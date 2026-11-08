import json
import os

import pytest

from kitelefon import sprachkurs_call as sc


class _Settings:
    sprachkurs_snapshot_path = None  # im Fixture gesetzt
    sprachkurs_spool_dir = None
    sprachkurs_enqueue = False
    sprachkurs_lernende = "Paula"  # KITELEFON_SPRACHKURS_LERNENDE (#149)


@pytest.fixture
def settings(tmp_path):
    s = _Settings()
    s.sprachkurs_snapshot_path = tmp_path / "kontext.txt"
    s.sprachkurs_spool_dir = tmp_path / "spool"
    return s


class _FakeConn:
    def close(self):
        pass


def _online(monkeypatch):
    # Nicht object(): die Implementierung schliesst jede Verbindung, also
    # braucht auch die Attrappe ein close().
    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())
    monkeypatch.setattr(sc, "_kontextblock", lambda conn, name: "Level: 4\n")
    monkeypatch.setattr(sc, "_learner_level", lambda conn, name: (7, 4))
    monkeypatch.setattr(sc, "_sitzung_anlegen", lambda conn, lid, level: 42)


def test_start_online_liefert_session_und_vad(settings, monkeypatch):
    _online(monkeypatch)
    call = sc.start_sprachkurs_call(settings)
    assert call.session_id == 42
    assert call.offline is False
    assert call.level == 4
    assert call.vad_silence_ms == 2500  # Level 4 -> 2500 (Spec 7.2)
    assert "Level: 4" in call.instructions
    # Schnappschuss wurde geschrieben
    assert "Level: 4" in settings.sprachkurs_snapshot_path.read_text()


def test_start_offline_nutzt_den_schnappschuss(settings, monkeypatch):
    settings.sprachkurs_snapshot_path.write_text("Level: 6\n(alt)")

    def kaputt():
        raise OSError("kein Netz")

    monkeypatch.setattr(sc, "_verbinden", kaputt)

    call = sc.start_sprachkurs_call(settings)
    assert call.offline is True
    assert call.session_id is None
    assert "Level: 6" in call.instructions
    assert call.vad_silence_ms == 3000  # konservativ bei offline


def test_start_query_fehlschlag_faellt_auf_schnappschuss_zurueck(settings, monkeypatch):
    """Nicht nur der Verbindungsaufbau, auch jede der drei Startabfragen
    muss auf den Schnappschuss degradieren (Abschluss-Review). Vorher fing
    start_sprachkurs_call() nur den Verbindungsversuch selbst ab — ein
    Fehler in _kontextblock() waere unbehandelt durchgeschlagen."""
    settings.sprachkurs_snapshot_path.write_text("Level: 5\n(alt)")
    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())

    def kaputt(conn, name):
        raise RuntimeError("kontextblock kaputt")

    monkeypatch.setattr(sc, "_kontextblock", kaputt)

    call = sc.start_sprachkurs_call(settings)
    assert call.offline is True
    assert call.session_id is None
    assert "Level: 5" in call.instructions


def test_start_query_fehlschlag_ohne_schnappschuss_wirft(settings, monkeypatch):
    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())

    def kaputt(conn, name):
        raise RuntimeError("kontextblock kaputt")

    monkeypatch.setattr(sc, "_kontextblock", kaputt)

    with pytest.raises(sc.SprachkursNichtVerfuegbar):
        sc.start_sprachkurs_call(settings)


def test_abschliessen_partial_fehler_notieren_spoolt_nur_ungeschriebenes(settings, monkeypatch):
    """Scheitert _fehler_notieren mittendrin, duerfen NUR die noch nicht
    geschriebenen Fehler in den Spool wandern — sonst legt ein spaeterer
    Replay die schon in der DB stehenden Fehler ein zweites Mal an."""
    _online(monkeypatch)
    call = sc.start_sprachkurs_call(settings)
    call._fehler_puffer = [
        {"learner_utterance": "a", "correction": "A", "category": None},
        {"learner_utterance": "b", "correction": "B", "category": None},
        {"learner_utterance": "c", "correction": "C", "category": None},
    ]
    monkeypatch.setattr(sc, "_sitzung_abschliessen", lambda conn, sid, **kw: True)
    aufrufe: list[str] = []

    def fehler_notieren(conn, sid, utterance, correction, category):
        aufrufe.append(utterance)
        if utterance == "b":
            raise RuntimeError("DB weg mitten drin")

    monkeypatch.setattr(sc, "_fehler_notieren", fehler_notieren)

    ergebnis = call.abschliessen(
        duration_s=100, speech_ratio_learner=0.5, ended_by="hangup", transcript="…"
    )
    assert ergebnis["gespoolt"] is True
    dateien = list(settings.sprachkurs_spool_dir.glob("*.json"))
    assert len(dateien) == 1
    inhalt = json.loads(dateien[0].read_text())
    assert [e["learner_utterance"] for e in inhalt["fehler"]] == ["b", "c"]
    assert inhalt["session_finalisiert"] is True


def test_start_offline_ohne_schnappschuss_wirft(settings, monkeypatch):
    def kaputt():
        raise OSError("kein Netz")

    monkeypatch.setattr(sc, "_verbinden", kaputt)
    with pytest.raises(sc.SprachkursNichtVerfuegbar):
        sc.start_sprachkurs_call(settings)  # -> Fehleransage (§7.6)


def test_note_error_offline_puffert_und_landet_im_spool(settings, monkeypatch):
    settings.sprachkurs_snapshot_path.write_text("Level: 3\n")
    monkeypatch.setattr(sc, "_verbinden", lambda: (_ for _ in ()).throw(OSError()))
    call = sc.start_sprachkurs_call(settings)

    out = call.note_error("je suis allé à marché", "au marché", "artikel")
    assert out == {"ok": True}

    call.abschliessen(duration_s=600, speech_ratio_learner=0.7, ended_by="hangup", transcript="…")
    dateien = list(settings.sprachkurs_spool_dir.glob("*.json"))
    assert len(dateien) == 1
    inhalt = json.loads(dateien[0].read_text())
    assert inhalt["fehler"][0]["correction"] == "au marché"
    assert inhalt["ended_by"] == "hangup"


def test_offene_fehler_text_formatiert_fuer_die_ansage(settings, monkeypatch):
    _online(monkeypatch)
    monkeypatch.setattr(
        sc,
        "_offene_fehler",
        lambda conn, sid: [
            {"learner_utterance": "à marché", "correction": "au marché"},
        ],
    )
    call = sc.start_sprachkurs_call(settings)
    text = call.offene_fehler_text()
    assert "à marché" in text and "au marché" in text


# --- Spool-Replay (spool_nachtragen) ---


def _write_spool_file(spool_dir, name="sitzung-1.json", **overrides):
    spool_dir.mkdir(parents=True, exist_ok=True)
    daten = {
        "session_id": None,
        "duration_s": 300,
        "speech_ratio_learner": 0.6,
        "ended_by": "hangup",
        "transcript": "…",
        "fehler": [
            {"learner_utterance": "a", "correction": "A", "category": None},
            {"learner_utterance": "b", "correction": "B", "category": None},
        ],
        "session_finalisiert": False,
        "level": 5,
        "started_at": "2026-08-01T12:00:00+00:00",
    }
    daten.update(overrides)
    pfad = spool_dir / name
    pfad.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")
    return pfad


class _FakeQueue:
    def __init__(self):
        self.calls: list[dict] = []

    def enqueue(self, **kw):
        self.calls.append(kw)
        return 1


def test_spool_nachtragen_offline_sitzung_wird_nachgetragen(settings, monkeypatch):
    """Fall (a): session_id null, session_finalisiert false, 2 Fehler,
    level 5 -> Sitzung wird bei Level 5 angelegt, abgeschlossen, beide
    Fehler nachgetragen, Datei wandert nach erledigt/, Enqueue bei Flag an."""
    pfad = _write_spool_file(settings.sprachkurs_spool_dir)
    settings.sprachkurs_enqueue = True

    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())
    monkeypatch.setattr(sc, "_learner_level", lambda conn, name: (7, 2))
    angelegt = {}

    def sitzung_anlegen(conn, learner_id, level):
        angelegt["learner_id"] = learner_id
        angelegt["level"] = level
        return 99

    monkeypatch.setattr(sc, "_sitzung_anlegen", sitzung_anlegen)

    abgeschlossen = {}

    def sitzung_abschliessen(conn, session_id, **felder):
        abgeschlossen["session_id"] = session_id
        abgeschlossen["felder"] = felder
        return True

    monkeypatch.setattr(sc, "_sitzung_abschliessen", sitzung_abschliessen)

    fehler_aufrufe: list[tuple] = []
    monkeypatch.setattr(
        sc, "_fehler_notieren", lambda conn, sid, u, c, cat: fehler_aufrufe.append((sid, u))
    )

    queue = _FakeQueue()
    ergebnis = sc.spool_nachtragen(settings, queue)

    assert ergebnis == {"nachgetragen": 1, "fehlgeschlagen": 0}
    assert angelegt == {"learner_id": 7, "level": 5}  # aus dem Spool-JSON
    assert abgeschlossen["session_id"] == 99
    assert [u for _, u in fehler_aufrufe] == ["a", "b"]
    assert not pfad.exists()
    erledigt = list((settings.sprachkurs_spool_dir / "erledigt").glob("*.json"))
    assert len(erledigt) == 1
    assert queue.calls == [
        {
            "agent_type": "sprachkurs_auswertung",
            "description": "Französisch-Sitzung auswerten",
            "deliver_via": "email",
            "deliver_to": "paula",
            "payload": {"session_id": 99},
            "konsument": "sprachkurs",
        }
    ]


def test_spool_nachtragen_teilerfolg_nur_fehler_nachtragen(settings, monkeypatch):
    """Fall (b): session_finalisiert true mit einem ungeschriebenen Fehler
    -> KEINE neue Sitzung, nur der Fehler wird zur vorhandenen session_id
    nachgetragen."""
    pfad = _write_spool_file(
        settings.sprachkurs_spool_dir,
        session_id=55,
        session_finalisiert=True,
        fehler=[{"learner_utterance": "c", "correction": "C", "category": None}],
    )
    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())

    def kein_neuanlegen(*a, **k):
        raise AssertionError("darf keine neue Sitzung anlegen")

    monkeypatch.setattr(sc, "_sitzung_anlegen", kein_neuanlegen)

    def kein_abschliessen(*a, **k):
        raise AssertionError("darf nicht erneut abgeschlossen werden")

    monkeypatch.setattr(sc, "_sitzung_abschliessen", kein_abschliessen)

    fehler_aufrufe: list[tuple] = []
    monkeypatch.setattr(
        sc, "_fehler_notieren", lambda conn, sid, u, c, cat: fehler_aufrufe.append((sid, u))
    )

    queue = _FakeQueue()
    ergebnis = sc.spool_nachtragen(settings, queue)

    assert ergebnis == {"nachgetragen": 1, "fehlgeschlagen": 0}
    assert fehler_aufrufe == [(55, "c")]
    assert not pfad.exists()


def test_spool_nachtragen_unlesbare_datei_bleibt_liegen(settings, monkeypatch):
    """Fall (c): unlesbare JSON-Datei zaehlt als fehlgeschlagen, bleibt
    liegen, Replay laeuft mit der naechsten Datei weiter."""
    settings.sprachkurs_spool_dir.mkdir(parents=True, exist_ok=True)
    kaputt = settings.sprachkurs_spool_dir / "sitzung-kaputt.json"
    kaputt.write_text("{nicht valides json", encoding="utf-8")
    gut = _write_spool_file(
        settings.sprachkurs_spool_dir,
        name="sitzung-gut.json",
        session_id=10,
        session_finalisiert=True,
        fehler=[],
    )

    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())
    monkeypatch.setattr(sc, "_fehler_notieren", lambda *a, **k: None)

    queue = _FakeQueue()
    ergebnis = sc.spool_nachtragen(settings, queue)

    assert ergebnis == {"nachgetragen": 1, "fehlgeschlagen": 1}
    assert kaputt.exists()  # bleibt liegen fuer den naechsten Versuch
    assert not gut.exists()


def test_spool_nachtragen_kein_enqueue_wenn_flag_aus(settings, monkeypatch):
    """Fall (d): Flag aus -> kein Enqueue, Rest identisch."""
    pfad = _write_spool_file(
        settings.sprachkurs_spool_dir, session_id=77, session_finalisiert=True, fehler=[]
    )
    settings.sprachkurs_enqueue = False
    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())
    monkeypatch.setattr(sc, "_fehler_notieren", lambda *a, **k: None)

    queue = _FakeQueue()
    ergebnis = sc.spool_nachtragen(settings, queue)

    assert ergebnis == {"nachgetragen": 1, "fehlgeschlagen": 0}
    assert not pfad.exists()
    assert queue.calls == []


def test_spool_nachtragen_ohne_spool_ordner_tut_nichts(settings):
    queue = _FakeQueue()
    ergebnis = sc.spool_nachtragen(settings, queue)
    assert ergebnis == {"nachgetragen": 0, "fehlgeschlagen": 0}


def test_spool_nachtragen_teilfehlschlag_verhindert_doppelanlage(settings, monkeypatch):
    """Review-Fund (Kritisch): scheitert _fehler_notieren nach erfolgreich
    angelegter UND abgeschlossener Sitzung, muss die Datei sofort die neue
    session_id + session_finalisiert=true tragen — sonst legt ein zweiter
    Replay-Lauf eine zweite Sitzung an und schreibt Fehler 1 doppelt."""
    pfad = _write_spool_file(
        settings.sprachkurs_spool_dir,
        fehler=[
            {"learner_utterance": "a", "correction": "A", "category": None},
            {"learner_utterance": "b", "correction": "B", "category": None},
        ],
    )
    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())
    monkeypatch.setattr(sc, "_learner_level", lambda conn, name: (7, 2))
    monkeypatch.setattr(sc, "_sitzung_anlegen", lambda conn, lid, level: 99)
    monkeypatch.setattr(sc, "_sitzung_abschliessen", lambda conn, sid, **kw: True)
    fehler_aufrufe: list[str] = []

    def fehler_notieren(conn, sid, u, c, cat):
        fehler_aufrufe.append(u)
        if u == "b":
            raise RuntimeError("DB weg mitten drin")

    monkeypatch.setattr(sc, "_fehler_notieren", fehler_notieren)

    queue = _FakeQueue()
    ergebnis = sc.spool_nachtragen(settings, queue)

    assert ergebnis == {"nachgetragen": 0, "fehlgeschlagen": 1}
    assert pfad.exists()
    assert not (settings.sprachkurs_spool_dir / "erledigt").exists() or not list(
        (settings.sprachkurs_spool_dir / "erledigt").glob("*.json")
    )
    inhalt = json.loads(pfad.read_text())
    assert inhalt["session_id"] == 99
    assert inhalt["session_finalisiert"] is True
    assert [e["learner_utterance"] for e in inhalt["fehler"]] == ["b"]

    # Zweiter Replay-Lauf mit "gesunden" Fakes: darf KEINE neue Sitzung
    # anlegen, nur den verbliebenen Fehler nachtragen, und die Datei
    # anschliessend verschieben.
    def keine_neuanlage(*a, **k):
        raise AssertionError("darf keine zweite Sitzung anlegen")

    monkeypatch.setattr(sc, "_sitzung_anlegen", keine_neuanlage)

    def kein_abschluss(*a, **k):
        raise AssertionError("darf nicht erneut abschliessen")

    monkeypatch.setattr(sc, "_sitzung_abschliessen", kein_abschluss)

    zweite_aufrufe: list[str] = []
    monkeypatch.setattr(
        sc, "_fehler_notieren", lambda conn, sid, u, c, cat: zweite_aufrufe.append((sid, u))
    )

    ergebnis2 = sc.spool_nachtragen(settings, queue)
    assert ergebnis2 == {"nachgetragen": 1, "fehlgeschlagen": 0}
    assert zweite_aufrufe == [(99, "b")]
    assert not pfad.exists()
    erledigt = list((settings.sprachkurs_spool_dir / "erledigt").glob("*.json"))
    assert len(erledigt) == 1


def test_spool_nachtragen_enqueue_fehlschlag_laesst_datei_liegen(settings, monkeypatch):
    """Review-Fund (Wichtig): scheitert queue.enqueue(), darf die Datei
    NICHT nach erledigt/ verschoben werden — sonst wird die Auswertung nie
    nachgeholt. Eine zweite, unabhaengige Datei muss trotzdem verarbeitet
    werden."""
    settings.sprachkurs_enqueue = True
    kaputt = _write_spool_file(
        settings.sprachkurs_spool_dir,
        name="sitzung-kaputt.json",
        session_id=1,
        session_finalisiert=True,
        fehler=[],
    )
    gut = _write_spool_file(
        settings.sprachkurs_spool_dir,
        name="sitzung-gut.json",
        session_id=2,
        session_finalisiert=True,
        fehler=[],
    )
    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())
    monkeypatch.setattr(sc, "_fehler_notieren", lambda *a, **k: None)

    class _KaputteQueue:
        def enqueue(self, **kw):
            if kw["payload"]["session_id"] == 1:
                raise RuntimeError("Queue nicht erreichbar")
            return 1

    ergebnis = sc.spool_nachtragen(settings, _KaputteQueue())

    assert ergebnis == {"nachgetragen": 1, "fehlgeschlagen": 1}
    assert kaputt.exists()
    assert not gut.exists()


def test_spool_nachtragen_rueckschreibung_scheitert_bleibt_beim_alten_stand(settings, monkeypatch):
    """Re-Review-Fund: die Rueckschreibung selbst kann scheitern (voller
    Datentraeger, Rechte). os.replace() sorgt fuer Atomaritaet — scheitert
    es, muss die Datei exakt den alten, VOLLSTAENDIGEN Zustand behalten
    (hier: session_id: null), nicht eine leere oder halbe Datei. Die Zeile
    zaehlt als fehlgeschlagen, wird nicht verschoben, und der naechste
    Spool-Eintrag wird trotzdem noch versucht (hier gibt es keinen
    zweiten, aber der Aufrufer darf hier nicht aus der Schleife fallen)."""
    pfad = _write_spool_file(settings.sprachkurs_spool_dir)
    ursprung = pfad.read_text()

    monkeypatch.setattr(sc, "_verbinden", lambda: _FakeConn())
    monkeypatch.setattr(sc, "_learner_level", lambda conn, name: (7, 2))
    monkeypatch.setattr(sc, "_sitzung_anlegen", lambda conn, lid, level: 99)
    monkeypatch.setattr(sc, "_sitzung_abschliessen", lambda conn, sid, **kw: True)
    monkeypatch.setattr(sc, "_fehler_notieren", lambda *a, **k: None)

    def kaputtes_replace(*a, **k):
        raise OSError("Platte voll")

    monkeypatch.setattr(os, "replace", kaputtes_replace)

    queue = _FakeQueue()
    ergebnis = sc.spool_nachtragen(settings, queue)

    assert ergebnis == {"nachgetragen": 0, "fehlgeschlagen": 1}
    assert pfad.exists()
    assert pfad.read_text() == ursprung  # exakt der alte, vollstaendige Stand
    inhalt = json.loads(pfad.read_text())
    assert inhalt["session_id"] is None
    assert inhalt["session_finalisiert"] is False
    assert not (settings.sprachkurs_spool_dir / "erledigt").exists() or not list(
        (settings.sprachkurs_spool_dir / "erledigt").glob("*.json")
    )
    # keine liegen gebliebene .tmp-Datei
    assert not list(settings.sprachkurs_spool_dir.glob("*.tmp"))
