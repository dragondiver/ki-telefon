"""Die vier neuen Werkzeuge im Katalog und im Dispatch des CallManagers."""

from datetime import UTC, datetime

import pytest
from test_call_manager import FakeAudio, FakeDialer, FakeHookSwitch

from kitelefon.agents import websuche
from kitelefon.call_manager import CallManager
from kitelefon.haushalt import Haushalt
from kitelefon.tasks import TaskQueue
from kitelefon.tools import ALL_TOOL_SCHEMAS

NEUE = ("web_suche", "merke", "erinnere", "vergiss")


def _cm(tmp_path, **kw):
    return CallManager(
        hookswitch=FakeHookSwitch([], delay=0.0),
        dialer=FakeDialer(digit=1),
        audio=FakeAudio(),
        open_session=lambda role, on_audio, th: None,
        queue=TaskQueue(tmp_path / "tasks.db"),
        **kw,
    )


def _schema(name):
    return next(s for s in ALL_TOOL_SCHEMAS if s["name"] == name)


def test_alle_vier_werkzeuge_stehen_im_katalog():
    namen = [s["name"] for s in ALL_TOOL_SCHEMAS]
    for name in NEUE:
        assert namen.count(name) == 1


def test_pflichtfelder_der_schemas():
    assert _schema("web_suche")["parameters"]["required"] == ["frage"]
    assert _schema("merke")["parameters"]["required"] == ["thema", "notiz"]
    assert _schema("erinnere")["parameters"]["required"] == []
    assert _schema("vergiss")["parameters"]["required"] == ["thema"]


def test_beschreibungen_tragen_die_verhaltensregeln():
    assert "ausdrücklich" in _schema("merke")["description"]
    assert "vorher nach" in _schema("vergiss")["description"].lower()
    assert "delegate_task" in _schema("web_suche")["description"]


def test_web_suche_kuendigt_sich_mit_moment_an():
    """#128: Ohne Ansage blieben vor dem Ergebnis rund 3,5 s Stille auf der Leitung."""
    beschreibung = _schema("web_suche")["description"]
    assert "VOR dem Aufruf kurz 'Moment'" in beschreibung
    assert "eigenen Ton" in beschreibung
    assert "sofort auf" in beschreibung
    assert "Frage nicht vor" in beschreibung


async def test_merke_und_erinnere_ueber_den_werkzeug_weg(tmp_path):
    cm = _cm(tmp_path, haushalt=Haushalt(tmp_path / "tasks.db"))

    ack = await cm.werkzeug_ausfuehren(
        "merke", {"thema": "Urlaub Italien", "notiz": "Hotel in Rimini ist gebucht."}
    )
    antwort = await cm.werkzeug_ausfuehren("erinnere", {"thema": "Urlaub Italien"})

    assert ack["ok"] is True
    assert antwort["notizen"][0]["text"] == "Hotel in Rimini ist gebucht."
    gestrichen = await cm.werkzeug_ausfuehren("vergiss", {"thema": "Urlaub Italien"})
    assert gestrichen["ok"] is True


async def test_erinnere_ohne_argumente_listet_die_themen(tmp_path):
    haushalt = Haushalt(tmp_path / "tasks.db")
    haushalt.merke("Urlaub Italien", "Hotel gebucht.", datetime.now(UTC))
    cm = _cm(tmp_path, haushalt=haushalt)

    antwort = await cm.werkzeug_ausfuehren("erinnere", {})

    assert antwort["themen"] == [{"thema": "Urlaub Italien", "notizen": 1}]


async def test_ohne_haushalt_antwortet_es_ehrlich(tmp_path):
    cm = _cm(tmp_path)

    antwort = await cm.werkzeug_ausfuehren("merke", {"thema": "a", "notiz": "b"})

    assert antwort["ok"] is False
    assert "nicht eingerichtet" in antwort["error"]


async def test_defekte_datenbank_wirft_nicht_nach_aussen(tmp_path):
    class Kaputt:
        async def merke_ki(self, *a, **k):
            import sqlite3

            raise sqlite3.OperationalError("database is locked")

    cm = _cm(tmp_path, haushalt=Kaputt())

    antwort = await cm.werkzeug_ausfuehren("merke", {"thema": "a", "notiz": "b"})

    assert antwort["ok"] is False
    assert "Gedächtnis" in antwort["error"]


async def test_web_suche_geht_durch_den_dispatch_und_zaehlt_das_tageslimit(tmp_path, monkeypatch):
    gefragt = []

    async def fake(frage, *, client=None, timeout_s=websuche.TIMEOUT_S):
        gefragt.append(frage)
        return {"ok": True, "antwort": "Antwort.", "quellen": []}

    monkeypatch.setattr(websuche, "web_suche", fake)
    cm = _cm(tmp_path, websuche_max_pro_tag=1)

    erste = await cm.werkzeug_ausfuehren("web_suche", {"frage": "Eins"})
    zweite = await cm.werkzeug_ausfuehren("web_suche", {"frage": "Zwei"})

    assert erste["ok"] is True and gefragt == ["Eins"]
    assert zweite["ok"] is False and "heute" in zweite["error"]


async def test_fehlerhafte_argumente_werfen_nicht(tmp_path):
    cm = _cm(tmp_path, haushalt=Haushalt(tmp_path / "tasks.db"))

    assert (await cm.werkzeug_ausfuehren("merke", {}))["ok"] is False
    assert (await cm.werkzeug_ausfuehren("vergiss", {}))["ok"] is False


@pytest.mark.parametrize("name", NEUE)
def test_werkzeugdienst_reicht_die_neuen_werkzeuge_an_elektra(name):
    from kitelefon.werkzeugdienst import FERN_WERKZEUGE, NUR_AM_TELEFON

    assert name in FERN_WERKZEUGE
    assert name not in NUR_AM_TELEFON


async def test_merke_dispatch_meldet_aehnliche_und_neu_schreibt(tmp_path):
    haushalt = Haushalt(tmp_path / "tasks.db")
    haushalt.merke("Urlaub Italien", "Hotel gebucht.", datetime.now(UTC))
    cm = _cm(tmp_path, haushalt=haushalt)

    r = await cm.werkzeug_ausfuehren("merke", {"thema": "Italienurlaub", "notiz": "Reiseführer."})
    assert r["ok"] is False
    assert r["aehnliche_themen"] == [{"thema": "Urlaub Italien", "notizen": 1}]

    r = await cm.werkzeug_ausfuehren(
        "merke", {"thema": "Italienurlaub", "notiz": "Reiseführer.", "neu": True}
    )
    assert r["ok"] is True


def test_merke_schema_kennt_neu_optional():
    props = _schema("merke")["parameters"]
    assert props["properties"]["neu"]["type"] == "boolean"
    assert "neu" not in props["required"]
    assert "aehnliche_themen" in _schema("merke")["description"]


async def test_merke_dispatch_nutzt_das_haiku_urteil(tmp_path):
    from types import SimpleNamespace as NS

    async def create(**kwargs):
        text = '{"entscheidung": "vorhanden", "thema": "Urlaub Italien"}'
        return NS(content=[NS(type="text", text=text)])

    haushalt = Haushalt(tmp_path / "tasks.db", urteil_client=NS(messages=NS(create=create)))
    haushalt.merke("Urlaub Italien", "Hotel gebucht.", datetime.now(UTC))
    cm = _cm(tmp_path, haushalt=haushalt)

    r = await cm.werkzeug_ausfuehren("merke", {"thema": "Reise nach Rom", "notiz": "Zug buchen."})

    assert r["ok"] is True
    assert r["zugeordnet_zu"] == "Urlaub Italien"
    assert haushalt.erinnere(None)["themen"] == [{"thema": "Urlaub Italien", "notizen": 2}]
