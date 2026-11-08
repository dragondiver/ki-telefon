"""Gemeinsames Gedaechtnis des Haushalts, nach Themen getrennt.

Vorgabe von Florian (2026-10-04): ein Gedaechtnis fuer Telefon und Elektra,
Themen leben so lange, wie sie benutzt werden (90 Tage ohne Zugriff).
"""

from datetime import UTC, datetime, timedelta

import pytest

from kitelefon.haushalt import (
    MAX_NOTIZ_ZEICHEN,
    MAX_NOTIZEN,
    MAX_THEMA_ZEICHEN,
    MAX_THEMEN,
    VERFALL,
    Haushalt,
)

JETZT = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)


@pytest.fixture
def hh(tmp_path):
    return Haushalt(tmp_path / "tasks.db")


def test_merke_legt_thema_an_und_erinnere_liefert_die_notiz(hh):
    ack = hh.merke("Urlaub Italien", "Hotel in Rimini ist gebucht.", JETZT)

    assert ack["ok"] is True
    assert "Urlaub Italien" in ack["ack"]
    antwort = hh.erinnere("Urlaub Italien", JETZT)
    assert antwort["ok"] is True
    assert antwort["thema"] == "Urlaub Italien"
    assert antwort["notizen"] == [{"text": "Hotel in Rimini ist gebucht.", "am": "2026-10-04"}]


def test_andere_schreibweise_trifft_dasselbe_thema(hh):
    hh.merke("Urlaub Italien", "Hotel gebucht.", JETZT)
    hh.merke("urlaub  ITALIEN", "Flug im Mai.", JETZT)

    antwort = hh.erinnere("URLAUB italien", JETZT)
    assert [n["text"] for n in antwort["notizen"]] == ["Hotel gebucht.", "Flug im Mai."]
    # Die Anzeige bleibt so, wie das Thema zuerst genannt wurde.
    assert antwort["thema"] == "Urlaub Italien"
    assert len(hh.erinnere(None, JETZT)["themen"]) == 1


def test_gleiche_notiz_zweimal_ergibt_keine_dublette(hh):
    hh.merke("Urlaub Italien", "Hotel gebucht.", JETZT)
    hh.merke("Urlaub Italien", "  hotel   GEBUCHT. ", JETZT)

    assert len(hh.erinnere("Urlaub Italien", JETZT)["notizen"]) == 1


def test_erinnere_ohne_thema_listet_die_themen_mit_anzahl(hh):
    hh.merke("Urlaub Italien", "Hotel gebucht.", JETZT)
    hh.merke("Urlaub Italien", "Flug im Mai.", JETZT)
    hh.merke("Geburtstag Paula", "Wünscht sich ein Buch.", JETZT)

    themen = hh.erinnere(None, JETZT)["themen"]

    assert {"thema": "Urlaub Italien", "notizen": 2} in themen
    assert {"thema": "Geburtstag Paula", "notizen": 1} in themen


def test_erinnere_ohne_themen_sagt_das_ehrlich(hh):
    antwort = hh.erinnere(None, JETZT)

    assert antwort["ok"] is True
    assert antwort["themen"] == []
    assert "noch keine" in antwort["hinweis"].lower()


def test_unbekanntes_thema_nennt_die_vorhandenen_namen(hh):
    hh.merke("Urlaub Italien", "Hotel gebucht.", JETZT)

    antwort = hh.erinnere("Italien", JETZT)

    assert antwort["ok"] is False
    assert antwort["vorhandene_themen"] == ["Urlaub Italien"]


def test_zu_lange_notiz_wird_abgelehnt_nicht_gekuerzt(hh):
    antwort = hh.merke("Urlaub Italien", "x" * (MAX_NOTIZ_ZEICHEN + 1), JETZT)

    assert antwort["ok"] is False
    assert str(MAX_NOTIZ_ZEICHEN) in antwort["error"]
    assert hh.erinnere(None, JETZT)["themen"] == []


def test_das_einunddreissigste_thema_wird_abgelehnt(hh):
    for i in range(MAX_THEMEN):
        assert hh.merke(f"Thema {i}", "etwas", JETZT, neu=True)["ok"] is True

    antwort = hh.merke("Thema zu viel", "etwas", JETZT, neu=True)

    assert antwort["ok"] is False
    assert "vergessen" in antwort["error"]
    assert len(hh.erinnere(None, JETZT)["themen"]) == MAX_THEMEN


def test_die_einundvierzigste_notiz_wird_abgelehnt_aber_ergaenzen_geht_weiter(hh):
    for i in range(MAX_NOTIZEN):
        assert hh.merke("Urlaub", f"Notiz {i}", JETZT)["ok"] is True

    assert hh.merke("Urlaub", "Notiz zu viel", JETZT)["ok"] is False
    # Eine schon vorhandene Notiz zu wiederholen bleibt harmlos.
    assert hh.merke("Urlaub", "Notiz 0", JETZT)["ok"] is True
    assert len(hh.erinnere("Urlaub", JETZT)["notizen"]) == MAX_NOTIZEN


def test_leeres_thema_oder_leere_notiz_wird_abgelehnt(hh):
    assert hh.merke("  ", "etwas", JETZT)["ok"] is False
    assert hh.merke("Urlaub", "   ", JETZT)["ok"] is False


def test_sonderzeichen_im_namen_sind_harmlos(hh):
    boese = "Rimini'; DROP TABLE haushalt_themen;--"
    assert hh.merke(boese, "Umlaute: äöüß", JETZT)["ok"] is True

    antwort = hh.erinnere(boese, JETZT)
    assert antwort["ok"] is True
    assert antwort["notizen"][0]["text"] == "Umlaute: äöüß"
    assert hh.merke("Zweites", "da", JETZT)["ok"] is True  # Tabelle lebt noch


def test_themen_verfallen_nach_90_tagen_ohne_nutzung(hh):
    hh.merke("Altes Thema", "etwas", JETZT)

    assert hh.aufraeumen(JETZT + VERFALL - timedelta(minutes=1)) == 0
    assert hh.aufraeumen(JETZT + VERFALL + timedelta(minutes=1)) == 1
    assert hh.erinnere(None, JETZT + VERFALL + timedelta(minutes=2))["themen"] == []


def test_lesen_verlaengert_das_leben_eines_themas(hh):
    hh.merke("Urlaub Italien", "Hotel gebucht.", JETZT)
    spaeter = JETZT + timedelta(days=80)
    assert hh.erinnere("Urlaub Italien", spaeter)["ok"] is True

    # 100 Tage nach dem Anlegen, aber nur 20 nach dem Lesen: noch da.
    assert hh.erinnere("Urlaub Italien", JETZT + timedelta(days=100))["ok"] is True


def test_vergiss_ohne_notiz_loescht_das_ganze_thema(hh):
    hh.merke("Urlaub Italien", "Hotel gebucht.", JETZT)

    assert hh.vergiss("urlaub italien", None, JETZT)["ok"] is True

    assert hh.erinnere("Urlaub Italien", JETZT)["ok"] is False


def test_vergiss_mit_notiz_loescht_nur_die_passende(hh):
    hh.merke("Urlaub Italien", "Hotel in Rimini ist gebucht.", JETZT)
    hh.merke("Urlaub Italien", "Flug geht im Mai.", JETZT)

    antwort = hh.vergiss("Urlaub Italien", "Hotel", JETZT)

    assert antwort["ok"] is True
    rest = hh.erinnere("Urlaub Italien", JETZT)["notizen"]
    assert [n["text"] for n in rest] == ["Flug geht im Mai."]


def test_vergiss_der_letzten_notiz_loescht_das_thema(hh):
    hh.merke("Urlaub Italien", "Hotel gebucht.", JETZT)

    hh.vergiss("Urlaub Italien", "Hotel", JETZT)

    assert hh.erinnere(None, JETZT)["themen"] == []


def test_vergiss_mit_unpassender_notiz_loescht_nichts(hh):
    hh.merke("Urlaub Italien", "Hotel in Rimini ist gebucht.", JETZT)

    antwort = hh.vergiss("Urlaub Italien", "Zahnarzttermin um drei", JETZT)

    assert antwort["ok"] is False
    assert antwort["notizen"] == ["Hotel in Rimini ist gebucht."]
    assert len(hh.erinnere("Urlaub Italien", JETZT)["notizen"]) == 1


def test_vergiss_unbekanntes_thema_nennt_die_vorhandenen_namen(hh):
    hh.merke("Urlaub Italien", "Hotel gebucht.", JETZT)

    antwort = hh.vergiss("Garten", None, JETZT)

    assert antwort["ok"] is False
    assert antwort["vorhandene_themen"] == ["Urlaub Italien"]


def test_themenzeile_nennt_nur_namen_und_anzahl(hh):
    assert hh.themenzeile(JETZT) == ""
    hh.merke("Urlaub Italien", "Hotel in Rimini ist gebucht.", JETZT)
    hh.merke("Urlaub Italien", "Flug im Mai.", JETZT)

    zeile = hh.themenzeile(JETZT)

    assert "Urlaub Italien (2)" in zeile
    assert "erinnere" in zeile
    assert "Rimini" not in zeile  # nie Inhalte im Prompt


def test_themenzeile_raeumt_verfallene_themen_vorher_auf(hh):
    hh.merke("Altes Thema", "etwas", JETZT)

    assert hh.themenzeile(JETZT + VERFALL + timedelta(days=1)) == ""


def test_vergiss_mit_mehreren_treffern_loescht_nichts_und_nennt_kandidaten(hh):
    hh.merke("Urlaub", "Hotel in Rimini ist gebucht.", JETZT)
    hh.merke("Urlaub", "Hotel Rechnung bezahlt.", JETZT)

    antwort = hh.vergiss("Urlaub", "Hotel", JETZT)

    assert antwort["ok"] is False
    assert len(antwort["notizen"]) == 2
    assert "mehrere" in antwort["error"].lower() or "passen" in antwort["error"].lower()
    assert len(hh.erinnere("Urlaub", JETZT)["notizen"]) == 2


def test_vergiss_mit_exaktem_treffer_trotz_mehrerer_teiltreffern(hh):
    hh.merke("Urlaub", "Hotel in Rimini ist gebucht.", JETZT)
    hh.merke("Urlaub", "Hotel", JETZT)

    antwort = hh.vergiss("Urlaub", "Hotel", JETZT)

    assert antwort["ok"] is True
    rest = hh.erinnere("Urlaub", JETZT)["notizen"]
    assert [n["text"] for n in rest] == ["Hotel in Rimini ist gebucht."]


def test_themen_ueberleben_einen_neustart(tmp_path):
    Haushalt(tmp_path / "tasks.db").merke("Urlaub Italien", "Hotel gebucht.", JETZT)

    neu = Haushalt(tmp_path / "tasks.db")

    assert neu.erinnere("Urlaub Italien", JETZT)["ok"] is True


def test_zu_langer_themenname_wird_abgelehnt(hh):
    r = hh.merke("x" * (MAX_THEMA_ZEICHEN + 1), "Notiz", JETZT)
    assert r["ok"] is False
    assert "61" in r["error"] and "60" in r["error"]
    assert hh.themenzeile(JETZT) == ""


def test_themenname_mit_hoechstlaenge_wird_angenommen(hh):
    assert hh.merke("x" * MAX_THEMA_ZEICHEN, "Notiz", JETZT)["ok"] is True


# --- Aehnlichkeitspruefung in merke (#103) ---------------------------------


def _mit_italien(tmp_path):
    h = Haushalt(tmp_path / "tasks.db")
    h.merke("Urlaub Italien", "Hotel gebucht.", datetime.now(UTC))
    return h


def _themen(h):
    return h.erinnere(None)["themen"]


@pytest.mark.parametrize("name", ["Italienurlaub", "Italien", "Urlaub", "Italien-Urlaub"])
def test_aehnliches_thema_wird_gemeldet_und_nicht_geschrieben(tmp_path, name):
    h = _mit_italien(tmp_path)

    r = h.merke(name, "Wir brauchen einen Reiseführer.")

    assert r["ok"] is False
    assert r["aehnliche_themen"] == [{"thema": "Urlaub Italien", "notizen": 1}]
    assert "neu=true" in r["hinweis"]
    assert _themen(h) == [{"thema": "Urlaub Italien", "notizen": 1}]


def test_anderes_thema_schreibt_normal(tmp_path):
    h = _mit_italien(tmp_path)

    r = h.merke("Geburtstag Paula", "Torte bestellen.")

    assert r["ok"] is True
    assert {t["thema"] for t in _themen(h)} == {"Urlaub Italien", "Geburtstag Paula"}


def test_neu_true_schreibt_trotz_aehnlichkeit(tmp_path):
    h = _mit_italien(tmp_path)

    r = h.merke("Italien", "Pizza-Rezepte.", neu=True)

    assert r["ok"] is True
    assert {t["thema"] for t in _themen(h)} == {"Urlaub Italien", "Italien"}


def test_exakter_name_in_anderer_schreibweise_schreibt_ins_vorhandene(tmp_path):
    h = _mit_italien(tmp_path)

    r = h.merke("urlaub  ITALIEN", "Reiseführer kaufen.")

    assert r["ok"] is True
    assert _themen(h) == [{"thema": "Urlaub Italien", "notizen": 2}]


def test_ohne_vorhandene_themen_schreibt_merke(tmp_path):
    h = Haushalt(tmp_path / "tasks.db")

    assert h.merke("Italienurlaub", "Reiseführer.")["ok"] is True


# --- KI-Urteil (Haiku) ueber die Themen-Aehnlichkeit, #103 -------------------

import asyncio  # noqa: E402
import json as _json  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402


class FakeHaiku:
    """Gefaelschter Anthropic-Client: liefert Text, wirft oder haengt."""

    def __init__(self, antwort=None, *, fehler=None, dauer=0.0, stop_reason="end_turn"):
        self._antwort = antwort
        self._stop_reason = stop_reason
        self._fehler = fehler
        self._dauer = dauer
        self.aufrufe = []
        self.messages = _NS(create=self._create)

    async def _create(self, **kwargs):
        self.aufrufe.append(kwargs)
        if self._dauer:
            await asyncio.sleep(self._dauer)
        if self._fehler:
            raise self._fehler
        text = self._antwort if isinstance(self._antwort, str) else _json.dumps(self._antwort)
        return _NS(content=[_NS(type="text", text=text)], stop_reason=self._stop_reason)


def _ki(tmp_path, client, *namen):
    h = Haushalt(tmp_path / "tasks.db", urteil_client=client, urteil_timeout_s=0.05)
    for n in namen:
        h.merke(n, "Notiz.", JETZT, neu=True)
    return h


def _namen(h):
    return {t["thema"] for t in h.erinnere(None)["themen"]}


def _run(h, name, **kw):
    return asyncio.run(h.merke_ki(name, "Reiseführer kaufen.", JETZT, **kw))


def test_ki_vorhanden_schreibt_ins_vorhandene_thema(tmp_path):
    fake = FakeHaiku({"entscheidung": "vorhanden", "thema": "Urlaub Italien"})
    h = _ki(tmp_path, fake, "Urlaub Italien", "Geburtstag Oma")

    r = _run(h, "Reise nach Rom")

    assert r["ok"] is True
    assert r["zugeordnet_zu"] == "Urlaub Italien"
    assert _namen(h) == {"Urlaub Italien", "Geburtstag Oma"}
    assert h.erinnere("Urlaub Italien")["notizen"][-1]["text"] == "Reiseführer kaufen."
    assert len(fake.aufrufe) == 1
    assert "Urlaub Italien" in fake.aufrufe[0]["messages"][0]["content"]
    assert fake.aufrufe[0]["model"] == "claude-haiku-5-5"
    assert fake.aufrufe[0]["thinking"] == {"type": "disabled"}


def test_ki_neu_legt_neues_thema_an(tmp_path):
    h = _ki(tmp_path, FakeHaiku({"entscheidung": "neu", "thema": ""}), "Urlaub Italien")

    r = _run(h, "Urlaub Spanien")

    assert r["ok"] is True
    assert "zugeordnet_zu" not in r
    assert _namen(h) == {"Urlaub Italien", "Urlaub Spanien"}


def test_ki_unklar_schreibt_nichts_und_nennt_kandidaten(tmp_path):
    fake = FakeHaiku({"entscheidung": "unklar", "thema": ""})
    h = _ki(tmp_path, fake, "Urlaub Italien", "Geburtstag Oma")

    r = _run(h, "Gartenfest")

    assert r["ok"] is False
    assert {k["thema"] for k in r["aehnliche_themen"]} == {"Urlaub Italien", "Geburtstag Oma"}
    assert "neu=true" in r["hinweis"]
    assert _namen(h) == {"Urlaub Italien", "Geburtstag Oma"}


def test_ki_vorhanden_mit_erfundenem_namen_faellt_auf_wortregel_zurueck(tmp_path):
    fake = FakeHaiku({"entscheidung": "vorhanden", "thema": "Erfundenes Thema"})
    h = _ki(tmp_path, fake, "Urlaub Italien")

    r = _run(h, "Italienurlaub")  # Wortregel: aehnlich -> melden, nicht schreiben

    assert len(fake.aufrufe) == 1
    assert r["ok"] is False
    assert r["aehnliche_themen"] == [{"thema": "Urlaub Italien", "notizen": 1}]
    assert _namen(h) == {"Urlaub Italien"}  # nichts unter erfundenem Namen


@pytest.mark.parametrize(
    "fake",
    [
        FakeHaiku(fehler=RuntimeError("API weg")),
        FakeHaiku("kein json"),
        FakeHaiku({"entscheidung": "vielleicht", "thema": ""}),
        FakeHaiku({"entscheidung": "neu", "thema": ""}, dauer=1.0),
        # Text trotz Ablehnung: darf nicht als Urteil durchgehen
        FakeHaiku({"entscheidung": "vorhanden", "thema": "Urlaub Italien"}, stop_reason="refusal"),
    ],
    ids=["exception", "kein-json", "unbekannte-entscheidung", "timeout", "refusal"],
)
def test_ki_fehler_faellt_auf_wortregel_zurueck(tmp_path, fake):
    h = _ki(tmp_path, fake, "Urlaub Italien")

    aehnlich = _run(h, "Italienurlaub")  # Wortregel meldet
    anders = _run(h, "Geburtstag Paula")  # Wortregel schreibt

    assert len(fake.aufrufe) == 2
    assert aehnlich["ok"] is False and "aehnliche_themen" in aehnlich
    assert anders["ok"] is True
    assert _namen(h) == {"Urlaub Italien", "Geburtstag Paula"}


def test_ki_ohne_client_nutzt_wortregel(tmp_path):
    h = _ki(tmp_path, None, "Urlaub Italien")

    assert _run(h, "Italienurlaub")["ok"] is False
    assert _run(h, "Geburtstag Paula")["ok"] is True


def test_ki_exakter_name_ruft_haiku_nicht_auf(tmp_path):
    fake = FakeHaiku({"entscheidung": "neu", "thema": ""})
    h = _ki(tmp_path, fake, "Urlaub Italien")

    r = _run(h, "urlaub  ITALIEN")

    assert r["ok"] is True
    assert fake.aufrufe == []
    assert h.erinnere("Urlaub Italien")["notizen"][-1]["text"] == "Reiseführer kaufen."


def test_ki_neu_true_ruft_haiku_nicht_auf(tmp_path):
    fake = FakeHaiku({"entscheidung": "vorhanden", "thema": "Urlaub Italien"})
    h = _ki(tmp_path, fake, "Urlaub Italien")

    r = _run(h, "Italien", neu=True)

    assert r["ok"] is True
    assert fake.aufrufe == []
    assert _namen(h) == {"Urlaub Italien", "Italien"}


def test_ki_leerer_haushalt_ruft_haiku_nicht_auf(tmp_path):
    fake = FakeHaiku({"entscheidung": "unklar", "thema": ""})
    h = _ki(tmp_path, fake)

    assert _run(h, "Urlaub Italien")["ok"] is True
    assert fake.aufrufe == []


def test_ki_ungueltige_eingabe_ruft_haiku_nicht_auf(tmp_path):
    fake = FakeHaiku({"entscheidung": "neu", "thema": ""})
    h = _ki(tmp_path, fake, "Urlaub Italien")

    assert asyncio.run(h.merke_ki("Garten", "   ", JETZT))["ok"] is False
    assert fake.aufrufe == []


def test_ki_neu_bei_vollem_haushalt_liefert_limit_fehler(tmp_path):
    fake = FakeHaiku({"entscheidung": "neu", "thema": ""})
    h = _ki(tmp_path, fake, *[f"Thema {i}" for i in range(MAX_THEMEN)])

    r = _run(h, "Etwas ganz anderes")

    assert r["ok"] is False
    assert "30" in r["error"]
    assert len(_namen(h)) == MAX_THEMEN


def test_ki_antwort_in_codezaun_wird_gelesen(tmp_path):
    fake = FakeHaiku('```json\n{"entscheidung": "vorhanden", "thema": "urlaub italien"}\n```')
    h = _ki(tmp_path, fake, "Urlaub Italien")

    r = _run(h, "Italienurlaub")

    assert r["zugeordnet_zu"] == "Urlaub Italien"
