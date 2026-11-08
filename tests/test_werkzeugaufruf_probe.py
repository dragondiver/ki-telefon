"""Tests der Werkzeugaufruf-Probe (#111) - ohne Netz, mit gefaelschtem Ereignisstrom."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PFAD = Path(__file__).resolve().parent.parent / "scripts" / "werkzeugaufruf_probe.py"
_spec = importlib.util.spec_from_file_location("werkzeugaufruf_probe", _PFAD)
probe = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)


def text(t):
    return {"type": "response.output_text.done", "text": t}


def aufruf(name, args, call_id="c1"):
    return {
        "type": "response.function_call_arguments.done",
        "name": name,
        "arguments": json.dumps(args),
        "call_id": call_id,
    }


def fertig(ein=100, aus=10):
    return {
        "type": "response.done",
        "response": {"usage": {"input_tokens": ein, "output_tokens": aus}},
    }


FALL_MERKE = probe.FAELLE[0]


def test_aufruf_vor_zusage():
    lauf = probe.lauf_aus_ereignissen(
        [
            aufruf("merke", {"thema": "Urlaub Italien", "notiz": "Sonnencreme"}),
            fertig(),
            text("Ist unter Urlaub Italien notiert."),
            fertig(),
        ]
    )
    assert lauf.aufrufe[0][0] == "merke"
    assert lauf.text_vor_aufruf == ""
    assert lauf.tokens_ein == 200 and lauf.tokens_aus == 20
    b = probe.bewerte(FALL_MERKE, lauf)
    assert b.richtig and not b.zusage_vor_aufruf and not b.zusage_ohne_aufruf


def test_zusage_vor_aufruf_wird_erkannt():
    lauf = probe.lauf_aus_ereignissen(
        [
            text("Alles klar, notiert!"),
            aufruf("merke", {"thema": "Italien", "notiz": "x"}),
            fertig(),
        ]
    )
    b = probe.bewerte(FALL_MERKE, lauf)
    assert b.werkzeug_gerufen and b.zusage_vor_aufruf and not b.zusage_ohne_aufruf


def test_zusage_ohne_aufruf():
    lauf = probe.lauf_aus_ereignissen([text("Ist notiert."), fertig()])
    b = probe.bewerte(FALL_MERKE, lauf)
    assert not b.richtig and not b.werkzeug_gerufen and b.zusage_ohne_aufruf


def test_falsches_thema_ist_nicht_richtig_aber_gerufen():
    lauf = probe.lauf_aus_ereignissen(
        [aufruf("merke", {"thema": "Garten", "notiz": "x"}), fertig()]
    )
    b = probe.bewerte(FALL_MERKE, lauf)
    assert b.werkzeug_gerufen and not b.richtig


def test_schreibweise_tolerant():
    assert probe.argumente_passen(FALL_MERKE, {"thema": "ITALIEN-Urlaub"})
    assert probe.norm("Länderspiel") == probe.norm("Laenderspiel")


def test_gegenprobe():
    gegen = probe.FAELLE[-1]
    assert gegen.erwartet is None
    assert probe.bewerte(
        gegen, probe.lauf_aus_ereignissen([text("Gut, und dir?"), fertig()])
    ).richtig
    b = probe.bewerte(gegen, probe.lauf_aus_ereignissen([aufruf("web_suche", {"frage": "x"})]))
    assert not b.richtig and b.falsches_werkzeug


def test_kaputte_argumente_stuerzen_nicht():
    ev = {"type": "response.function_call_arguments.done", "name": "merke", "arguments": "{kaputt"}
    assert probe.lauf_aus_ereignissen([ev]).aufrufe == [("merke", {})]


def test_faelle_sind_gueltig():
    namen = [f.name for f in probe.FAELLE]
    assert len(namen) == len(set(namen))
    schemas = {s["name"] for s in probe.ALL_TOOL_SCHEMAS}
    for f in probe.FAELLE:
        assert f.satz
        assert f.erwartet is None or f.erwartet in schemas
    assert sum(f.erwartet is None for f in probe.FAELLE) >= 1


def test_quoten():
    ergebnisse = [
        (FALL_MERKE, probe.lauf_aus_ereignissen([aufruf("merke", {"thema": "Italien"}), fertig()])),
        (FALL_MERKE, probe.lauf_aus_ereignissen([text("Notiert."), fertig()])),
        (FALL_MERKE, probe.Lauf(fehler="Timeout")),
    ]
    s = probe.statistik(ergebnisse)[0]
    assert (s.laeufe, s.richtig, s.zusage_ohne_aufruf, s.fehler) == (3, 1, 1, 1)
    assert s.quote == "1/2"
    assert "merke_italienurlaub" in probe.tabelle([s])


def test_prompt_enthaelt_themenliste_und_alle_werkzeuge():
    rolle, prompt, werkzeuge = probe.baue_prompt_und_werkzeuge()
    assert rolle["name"] == "Künstliche Intelligenz"
    assert "Urlaub Italien (2)" in prompt
    assert werkzeuge is probe.ALL_TOOL_SCHEMAS or "merke" in {w["name"] for w in werkzeuge}


def test_fake_ergebnisse():
    assert probe.fake_ergebnis("merke", {"thema": "Urlaub Italien"})["ack"].startswith(
        "Unter 'Urlaub Italien' notiert"
    )
    assert probe.fake_ergebnis("unbekanntes_werkzeug", {}) == {"ok": True}


def test_ohne_schluessel_wird_nichts_gesendet(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def verboten(*a, **k):
        raise AssertionError("darf nicht verbinden")

    monkeypatch.setattr(probe, "api_verbinder", verboten)
    assert probe.main([]) == 2
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err and "nichts gesendet" in err


def test_max_laeufe_schutz(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(probe, "api_verbinder", lambda k: (_ for _ in ()).throw(AssertionError()))
    assert probe.main(["--wiederholungen", "50", "--max-laeufe", "100"]) == 2
    assert "max-laeufe" in capsys.readouterr().err


class FakeTransport:
    """Spielt Server-Ereignisse ab; Antworten kommen erst nach response.create."""

    def __init__(self, antworten):
        self.antworten = list(antworten)  # je response.create eine Liste Ereignisse
        self.gesendet = []
        self._queue = [{"type": "session.created"}]
        self.zu = False

    async def send(self, msg):
        ev = json.loads(msg)
        self.gesendet.append(ev)
        if ev["type"] == "session.update":
            self._queue.append({"type": "session.updated"})
        elif ev["type"] == "response.create":
            self._queue.extend(self.antworten.pop(0))

    async def recv(self):
        if not self._queue:
            raise ConnectionError("Strom zu Ende")
        return json.dumps(self._queue.pop(0))

    async def close(self):
        self.zu = True


def test_lauf_beantwortet_aufruf_mit_fake_und_spricht_weiter():
    t = FakeTransport(
        [
            [aufruf("merke", {"thema": "Urlaub Italien", "notiz": "x"}, "abc"), fertig()],
            [text("Ist unter Urlaub Italien notiert."), fertig()],
        ]
    )
    lauf = asyncio.run(probe.fuehre_lauf(t, FALL_MERKE.satz, "m", "prompt", []))
    assert lauf.fehler is None
    assert lauf.aufrufe[0][0] == "merke" and "notiert" in lauf.text_gesamt
    update = next(e for e in t.gesendet if e["type"] == "session.update")
    assert update["session"]["output_modalities"] == ["text"]
    item = next(e for e in t.gesendet if e["type"] == "conversation.item.create")
    assert item["item"]["content"] == [{"type": "input_text", "text": FALL_MERKE.satz}]
    out = [e for e in t.gesendet if e.get("item", {}).get("type") == "function_call_output"]
    assert out[0]["item"]["call_id"] == "abc"
    assert "notiert" in json.loads(out[0]["item"]["output"])["ack"]
    assert t.zu


def test_api_fehler_wird_als_technischer_fehler_gezaehlt():
    t = FakeTransport([[{"type": "error", "error": {"message": "kaputt"}}]])
    lauf = asyncio.run(probe.fuehre_lauf(t, "Hallo", "m", "p", []))
    assert lauf.fehler and "kaputt" in lauf.fehler


def test_main_mit_gefaelschtem_verbinder(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def verbinder(modell):
        return FakeTransport([[text("Hallo!"), fertig()]])

    code = probe.main(
        ["--fall", "gegenprobe_smalltalk", "--wiederholungen", "2"], verbinder=verbinder
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "gegenprobe_smalltalk" in out and "2/2" in out
    assert "sk-test" not in out


def limit_antwort():
    return {
        "type": "response.done",
        "response": {
            "status": "failed",
            "status_details": {
                "type": "failed",
                "error": {"code": "rate_limit_exceeded", "message": "TPM Limit 200000"},
            },
        },
    }


def test_fehlgeschlagene_antwort_ist_technischer_fehler_keine_fehlquote():
    lauf = probe.lauf_aus_ereignissen([limit_antwort()])
    assert lauf.fehler and "rate_limit_exceeded" in lauf.fehler
    s = probe.statistik([(FALL_MERKE, lauf)])[0]
    assert s.fehler == 1 and s.richtig == 0 and s.zusage_ohne_aufruf == 0


def test_ratenlimit_wird_wiederholt(monkeypatch):
    pausen = []

    async def schlafe(s):
        pausen.append(s)

    monkeypatch.setattr(probe, "schlafe", schlafe)
    transporte = [
        FakeTransport([[limit_antwort()]]),
        FakeTransport([[text("Hallo!"), fertig(5, 1)]]),
    ]

    async def verbinder(modell):
        return transporte.pop(0)

    ergebnisse = asyncio.run(probe.probiere([probe.FAELLE[-1]], 1, verbinder))
    lauf = ergebnisse[0][1]
    assert lauf.fehler is None and lauf.text_gesamt == "Hallo!"
    assert len(pausen) == 1


def test_erinnere_hat_keine_zusagepruefung():
    fall = next(f for f in probe.FAELLE if f.erwartet == "erinnere")
    lauf = probe.lauf_aus_ereignissen([text("Das ist gespeichert."), fertig()])
    assert not probe.bewerte(fall, lauf).zusage_ohne_aufruf


def test_nicht_aussagekraeftiger_fall_ist_nicht_bewertet():
    fall = probe.Fall(
        "n_b_fall", "Irgendein Satz.", "delegate_task", aussagekraeftig=False, hinweis="Test"
    )
    ergebnisse = [
        (fall, probe.lauf_aus_ereignissen([text("Welcher Zahnarzt?"), fertig()])),
        (FALL_MERKE, probe.lauf_aus_ereignissen([aufruf("merke", {"thema": "Italien"}), fertig()])),
    ]
    stats = probe.statistik(ergebnisse)
    assert stats[0].quote == "n.b."
    tab = probe.tabelle(stats)
    assert "nicht bewertet (Test" in tab
    assert "Bewertete Läufe: 1" in tab
    assert "0/1" not in tab


# --- Mehrzug (#116) ---------------------------------------------------------

RUECKRUF = next(f for f in probe.FAELLE if f.name == "rueckruf_delegation")
VERGISS = next(f for f in probe.FAELLE if f.name == "vergiss_nach_ja")


def zug_lauf(*ereignisse):
    return probe.lauf_aus_ereignissen(list(ereignisse))


def mehrzug(*zuege):
    """Gesamtlauf aus je einer Ereignisliste pro Zug."""
    lauf = probe.Lauf()
    lauf.zuege = [probe.lauf_aus_ereignissen(z) for z in zuege]
    probe.fasse_zuege_zusammen(lauf)
    return lauf


def test_einzelsatz_string_bleibt_gueltig_und_wird_ein_zug():
    assert len(FALL_MERKE.zuege) == 1
    z = FALL_MERKE.zuege[0]
    assert z.satz == FALL_MERKE.satz and z.erwartet == "merke"
    assert z.argument_enthaelt == {"thema": "italien"}
    assert FALL_MERKE.erwartet_text == "merke"


def test_zugfolge_mit_erwartung_am_fall_ist_verboten():
    z = (probe.Zug("a", None),)
    with pytest.raises(ValueError):
        probe.Fall("x", z, "merke")
    with pytest.raises(ValueError):
        probe.Fall("x", ())


def test_mehrzug_faelle_sind_gueltig():
    schemas = {s["name"]: s for s in probe.ALL_TOOL_SCHEMAS}
    assert len(RUECKRUF.zuege) == 2 and len(VERGISS.zuege) == 2
    assert RUECKRUF.aussagekraeftig  # kein "n.b." mehr
    assert RUECKRUF.zuege[0].informativ and not RUECKRUF.zuege[1].informativ
    assert RUECKRUF.zuege[1].erwartet == "delegate_task"
    assert RUECKRUF.zuege[1].argument_enthaelt["deliver_via"] == "callback"
    assert VERGISS.zuege[0].erwartet is None and VERGISS.zuege[1].erwartet == "vergiss"
    for f in (RUECKRUF, VERGISS):
        for z in f.zuege:
            assert z.erwartet is None or z.erwartet in schemas
    laender = next(f for f in probe.FAELLE if f.name == "web_laenderspiel")
    assert len(laender.zuege) == 1


def test_fake_ergebnisse_delegate_und_vergiss():
    assert probe.fake_ergebnis("delegate_task", {})["status"] == "queued"
    assert probe.fake_ergebnis("vergiss", {"thema": "Urlaub Italien"})["ok"] is True


def test_auswertung_je_zug_rueckruf_ohne_aufruf_in_zug_1_ist_kein_fehler():
    lauf = mehrzug(
        [text("Soll ich dich auf dem Telefon zurückrufen?"), fertig()],
        [
            aufruf(
                "delegate_task",
                {"description": "Öffnungszeiten Zahnarzt", "deliver_via": "callback"},
            ),
            fertig(),
        ],
    )
    b1, b2 = probe.bewerte_zuege(RUECKRUF, lauf)
    assert b1.informativ and b2.richtig and not b2.falsches_werkzeug
    s = probe.statistik([(RUECKRUF, lauf)])[0]
    assert s.quote == "1/1"
    assert [z.quote for z in s.zuege] == ["inf.", "1/1"]
    assert s.zuege[0].ohne_aufruf == 1


def test_rueckruf_aufruf_schon_in_zug_1_ist_ebenfalls_kein_fehler():
    lauf = mehrzug(
        [aufruf("delegate_task", {"description": "Zahnarzt", "deliver_via": "callback"}), fertig()],
        [text("Ja, mache ich."), fertig()],
    )
    s = probe.statistik([(RUECKRUF, lauf)])[0]
    assert s.zuege[0].aufrufe["delegate_task"] == 1
    # Aufruf in Zug 1 genuegt: Zug 2 und Fall richtig
    assert s.zuege[1].quote == "1/1" and s.quote == "1/1"


def test_rueckruf_ohne_aufruf_in_beiden_zuegen_ist_falsch():
    lauf = mehrzug([text("Soll ich?"), fertig()], [text("Ja, mache ich."), fertig()])
    s = probe.statistik([(RUECKRUF, lauf)])[0]
    assert s.quote == "0/1"


def test_rueckruf_aufruf_in_zug_1_mit_falschem_deliver_via_genuegt_nicht():
    lauf = mehrzug(
        [aufruf("delegate_task", {"description": "Zahnarzt", "deliver_via": "email"}), fertig()],
        [text("Okay."), fertig()],
    )
    assert probe.statistik([(RUECKRUF, lauf)])[0].quote == "0/1"


def test_rueckruf_satz_ist_vollstaendiger_auftrag():
    assert "Dr. Schmidt" in RUECKRUF.zuege[0].satz and "Stuttgart" in RUECKRUF.zuege[0].satz


def test_rueckruf_falsches_deliver_via_ist_nicht_richtig():
    lauf = mehrzug(
        [text("Okay?"), fertig()],
        [aufruf("delegate_task", {"description": "Zahnarzt", "deliver_via": "email"}), fertig()],
    )
    b = probe.bewerte_zuege(RUECKRUF, lauf)[1]
    assert b.werkzeug_gerufen and not b.richtig


def test_vergiss_zug_1_ohne_aufruf_ist_richtig_zug_2_mit_aufruf():
    lauf = mehrzug(
        [text("Soll ich das Thema Urlaub Italien wirklich löschen?"), fertig()],
        [aufruf("vergiss", {"thema": "Urlaub Italien"}), fertig(), text("Erledigt."), fertig()],
    )
    b1, b2 = probe.bewerte_zuege(VERGISS, lauf)
    assert b1.richtig and not b1.falsches_werkzeug
    assert b2.richtig
    s = probe.statistik([(VERGISS, lauf)])[0]
    assert [z.quote for z in s.zuege] == ["1/1", "1/1"] and s.quote == "1/1"


def test_vergiss_schon_in_zug_1_ist_falsch_und_macht_den_fall_falsch():
    lauf = mehrzug(
        [aufruf("vergiss", {"thema": "Urlaub Italien"}), fertig()],
        [aufruf("vergiss", {"thema": "Urlaub Italien"}), fertig()],
    )
    s = probe.statistik([(VERGISS, lauf)])[0]
    assert [z.quote for z in s.zuege] == ["0/1", "1/1"]
    assert s.quote == "0/1" and s.falsches_werkzeug == 1


def test_mehrzug_tabelle_zeigt_fall_zug_und_legende():
    ok = mehrzug(
        [text("Soll ich?"), fertig()],
        [aufruf("delegate_task", {"description": "Zahnarzt", "deliver_via": "callback"}), fertig()],
    )
    tab = probe.tabelle(probe.statistik([(RUECKRUF, ok)]))
    assert "rueckruf_delegation" in tab and "2 Züge" in tab
    assert "  Zug 1" in tab and "  Zug 2" in tab and "inf." in tab
    assert "1× ohne Aufruf" in tab
    assert "Legende" in tab and "Bewertete Läufe: 1 (Züge: 2)" in tab


def test_technischer_fehler_im_mehrzug_zaehlt_nirgends_als_falsch():
    lauf = mehrzug(
        [text("Okay?"), fertig()],
        [limit_antwort()],
    )
    assert lauf.fehler and "rate_limit_exceeded" in lauf.fehler
    s = probe.statistik([(RUECKRUF, lauf)])[0]
    assert s.fehler == 1 and s.richtig == 0
    assert all(z.fehler == 1 and z.richtig == 0 for z in s.zuege)


def test_zug_2_wird_erst_nach_ende_von_zug_1_gesendet():
    """Zug 1 ruft ein Werkzeug (zwei Antwortrunden); Zug 2 darf erst danach kommen."""
    t = FakeTransport(
        [
            [aufruf("vergiss", {"thema": "Urlaub Italien"}, "v1"), fertig()],
            [text("Erledigt."), fertig()],
            [text("Zweiter Zug."), fertig()],
        ]
    )
    lauf = asyncio.run(probe.fuehre_lauf(t, ["Satz eins", "Satz zwei"], "m", "p", []))
    assert lauf.fehler is None and len(lauf.zuege) == 2
    arten = []
    for e in t.gesendet:
        if e["type"] == "conversation.item.create":
            item = e["item"]
            arten.append(
                item["content"][0]["text"]
                if item["type"] == "message"
                else "fake:" + item["call_id"]
            )
        elif e["type"] == "response.create":
            arten.append("response.create")
    assert arten == [
        "Satz eins",
        "response.create",
        "fake:v1",
        "response.create",
        "Satz zwei",
        "response.create",
    ]
    assert lauf.zuege[0].aufrufe == [("vergiss", {"thema": "Urlaub Italien"})]
    assert lauf.zuege[1].aufrufe == [] and lauf.zuege[1].text_gesamt == "Zweiter Zug."
    assert lauf.aufrufe == lauf.zuege[0].aufrufe  # Gesamtlauf = Summe
    assert lauf.tokens_ein == 300 and t.zu


def test_fehler_in_zug_1_verhindert_zug_2():
    t = FakeTransport([[limit_antwort()]])
    lauf = asyncio.run(probe.fuehre_lauf(t, ["eins", "zwei"], "m", "p", []))
    assert "rate_limit_exceeded" in lauf.fehler and len(lauf.zuege) == 1
    assert sum(e["type"] == "response.create" for e in t.gesendet) == 1


def test_einzelsatz_lauf_bleibt_kompatibel():
    t = FakeTransport([[text("Hallo!"), fertig()]])
    lauf = asyncio.run(probe.fuehre_lauf(t, "Hallo", "m", "p", []))
    assert lauf.text_gesamt == "Hallo!" and len(lauf.zug_laeufe()) == 1
    assert probe.zeile_lauf(probe.FAELLE[-1], lauf).startswith("[gegenprobe_smalltalk] Aufrufe:")


def test_max_laeufe_zaehlt_laeufe_nicht_zuege(monkeypatch, capsys):
    """5 Wiederholungen eines 2-Zug-Falls = 5 Laeufe (10 Zuege): passt unter --max-laeufe 5."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    verbunden = []

    async def verbinder(modell):
        verbunden.append(modell)
        return FakeTransport([[text("Okay?"), fertig()], [text("Gut."), fertig()]])

    code = probe.main(
        ["--fall", "vergiss_nach_ja", "--wiederholungen", "5", "--max-laeufe", "5"],
        verbinder=verbinder,
    )
    out = capsys.readouterr().out
    assert code == 0 and len(verbunden) == 5
    assert "Bewertete Läufe: 5 (Züge: 10)" in out
    assert (
        probe.main(
            ["--fall", "vergiss_nach_ja", "--wiederholungen", "6", "--max-laeufe", "5"],
            verbinder=verbinder,
        )
        == 2
    )


def test_ausfuehrlich_zeigt_zuege(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def verbinder(modell):
        return FakeTransport([[text("Okay?"), fertig()], [text("Gut."), fertig()]])

    probe.main(
        ["--fall", "vergiss_nach_ja", "--wiederholungen", "1", "--ausfuehrlich"],
        verbinder=verbinder,
    )
    out = capsys.readouterr().out
    assert "[vergiss_nach_ja Z1]" in out and "[vergiss_nach_ja Z2]" in out


@pytest.mark.parametrize(
    "satz",
    [
        "Ich habe Sonnencreme hinzugefügt.",
        "Ich habe es hinzugefuegt.",
        "Ich schreibe das für dich auf.",
        "Hab ich aufgeschrieben.",
    ],
)
def test_zusage_neue_muster(satz):
    assert probe.zusage(satz)


@pytest.mark.parametrize(
    "satz",
    [
        "Soll ich dich zurückrufen?",
        "Wenn du willst, rufe ich dich zurück?",
        "Möchtest du, dass ich das aufschreibe?",
        "Wie geht es dir?",
    ],
)
def test_keine_zusage(satz):
    assert not probe.zusage(satz)


def _fall(name):
    return next(f for f in probe.FAELLE if f.name == name)


def test_ausnahme_rueckfrage_oder_nicht_deutsche_suche_ist_richtig():
    fall = _fall("ausnahme_frauen")
    frage = probe.lauf_aus_ereignissen([text("Welche Frauen-Mannschaft?"), fertig()])
    assert probe.bewerte(fall, frage).richtig
    ok = probe.lauf_aus_ereignissen([aufruf("web_suche", {"frage": "Länderspiel der Frauen"})])
    assert probe.bewerte(fall, ok).richtig


def test_ausnahme_stille_deutschland_annahme_ist_falsch():
    fall = _fall("ausnahme_frauen")
    still = probe.lauf_aus_ereignissen(
        [aufruf("web_suche", {"frage": "Länderspiel der deutschen Frauen gestern"})]
    )
    assert not probe.bewerte(fall, still).richtig
    ohne = probe.lauf_aus_ereignissen([aufruf("web_suche", {"frage": "Fußball gestern"})])
    assert not probe.bewerte(fall, ohne).richtig


def test_genannter_gegner_verlangt_suche_ohne_rueckfrage():
    fall = _fall("web_gegen_frankreich")
    assert probe.bewerte(
        fall,
        probe.lauf_aus_ereignissen([aufruf("web_suche", {"frage": "Deutschland - Frankreich"})]),
    ).richtig
    assert not probe.bewerte(
        fall, probe.lauf_aus_ereignissen([text("Welche Mannschaften?"), fertig()])
    ).richtig


def test_informativer_einzelzug_zeigt_antwortzeile_statt_quote():
    fall = _fall("gegenprobe_spiel_gestern")
    laeufe = [
        (fall, probe.lauf_aus_ereignissen([text("Welches Spiel?"), fertig()])),
        (fall, probe.lauf_aus_ereignissen([aufruf("web_suche", {"frage": "x"})])),
    ]
    tab = probe.tabelle(probe.statistik(laeufe))
    assert "inf." in tab and "1× ohne Aufruf, Aufrufe: web_suche×1" in tab
    assert "2/2" not in tab


# --- Gedaechtnis nicht delegieren (#122) ------------------------------------

GEDAECHTNIS = next(f for f in probe.FAELLE if f.name == "gedaechtnis_uebernehmen")


def test_gedaechtnis_fall_ist_gueltig():
    assert [z.erwartet for z in GEDAECHTNIS.zuege] == ["erinnere", "vergiss"]
    assert GEDAECHTNIS.zuege[1].satz == "Kannst du das übernehmen?"
    assert GEDAECHTNIS.zuege[1].rueckfrage_ok
    notizen = GEDAECHTNIS.fake_ergebnisse["erinnere"]["notizen"]
    assert sum("Sonnencreme" in n for n in notizen) == 2


def _gedaechtnis_lauf(zug_2):
    return mehrzug(
        [
            aufruf("erinnere", {"thema": "Urlaub Italien"}),
            fertig(),
            text("Zwei Mal Sonnencreme."),
            fertig(),
        ],
        zug_2,
    )


def test_gedaechtnis_delegate_task_ist_falsch():
    lauf = _gedaechtnis_lauf(
        [aufruf("delegate_task", {"description": "Notizen aufräumen"}), fertig()]
    )
    s = probe.statistik([(GEDAECHTNIS, lauf)])[0]
    assert [z.quote for z in s.zuege] == ["1/1", "0/1"] and s.quote == "0/1"
    assert s.falsches_werkzeug == 1


def test_gedaechtnis_rueckfrage_oder_vergiss_ist_richtig():
    rueckfrage = _gedaechtnis_lauf(
        [text("Soll ich 'Sonnencreme kaufen.' löschen und die andere behalten?"), fertig()]
    )
    vergiss = _gedaechtnis_lauf(
        [aufruf("vergiss", {"thema": "Urlaub Italien", "notiz": "Sonnencreme kaufen"}), fertig()]
    )
    for lauf in (rueckfrage, vergiss):
        s = probe.statistik([(GEDAECHTNIS, lauf)])[0]
        assert s.quote == "1/1"


def test_fall_eigene_fakes_gehen_an_das_modell():
    t = FakeTransport(
        [
            [aufruf("erinnere", {"thema": "Urlaub Italien"}, "e1"), fertig()],
            [text("Sonnencreme steht doppelt."), fertig()],
            [text("Welche soll weg?"), fertig()],
        ]
    )
    lauf = asyncio.run(
        probe.fuehre_lauf(
            t,
            [z.satz for z in GEDAECHTNIS.zuege],
            "m",
            "p",
            [],
            fakes=GEDAECHTNIS.fake_ergebnisse,
        )
    )
    assert lauf.fehler is None
    out = next(e for e in t.gesendet if e.get("item", {}).get("type") == "function_call_output")
    assert json.loads(out["item"]["output"]) == GEDAECHTNIS.fake_ergebnisse["erinnere"]
    # Ohne eigene Fakes bleibt das allgemeine Ergebnis.
    assert "Sonnencreme kaufen." not in probe.fake_ergebnis("erinnere", {})["notizen"]
