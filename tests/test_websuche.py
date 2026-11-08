"""Websuche im Gespraech: Haiku mit dem Server-Werkzeug web_search.

Kein Netz: der Anthropic-Client wird durch einen Fake ersetzt, der Antworten
im Format der echten API liefert (Textbloecke mit Zitaten, Suchergebnis-Bloecke).
"""

import asyncio
from datetime import date
from types import SimpleNamespace as NS

import anthropic
import httpx

from kitelefon.agents.websuche import Tageszaehler, handle_web_suche, web_suche


def text(inhalt, urls=()):
    return NS(type="text", text=inhalt, citations=[NS(url=u) for u in urls])


def suchergebnis(*urls):
    return NS(type="web_search_tool_result", content=[NS(url=u) for u in urls])


def antwort(*bloecke, stop_reason="end_turn", stop_details=None):
    return NS(content=list(bloecke), stop_reason=stop_reason, stop_details=stop_details)


class FakeClient:
    def __init__(self, *antworten, fehler=None, dauer=0.0):
        self._antworten = list(antworten)
        self._fehler = fehler
        self._dauer = dauer
        self.aufrufe = []
        self.messages = NS(create=self._create)

    async def _create(self, **kwargs):
        self.aufrufe.append(kwargs)
        if self._dauer:
            await asyncio.sleep(self._dauer)
        if self._fehler:
            raise self._fehler
        return self._antworten.pop(0)


async def test_antwort_und_quellen_kommen_vorlesbar_zurueck():
    client = FakeClient(
        antwort(
            text("Ich suche kurz."),
            suchergebnis("https://www.kicker.de/a", "https://www.sportschau.de/b"),
            text("Deutschland gewinnt 2:1.", ["https://www.kicker.de/a"]),
        )
    )

    ergebnis = await web_suche("Wie ging das Länderspiel aus?", client=client)

    assert ergebnis == {"ok": True, "antwort": "Deutschland gewinnt 2:1.", "quellen": ["kicker.de"]}
    aufruf = client.aufrufe[0]
    assert aufruf["model"] == "claude-haiku-5-5"
    assert aufruf["thinking"] == {"type": "disabled"}
    assert aufruf["tools"][0]["type"] == "web_search_20250305"
    assert aufruf["tools"][0]["max_uses"] == 3
    assert aufruf["messages"] == [{"role": "user", "content": "Wie ging das Länderspiel aus?"}]


async def test_ohne_zitate_nennt_es_die_ersten_treffer():
    client = FakeClient(
        antwort(
            suchergebnis("https://a.de/x", "https://www.b.de/y", "https://a.de/z", "https://c.de"),
            text("Ergebnis."),
        )
    )

    ergebnis = await web_suche("Frage", client=client)

    assert ergebnis["quellen"] == ["a.de", "b.de", "c.de"]


async def test_pause_turn_wird_fortgesetzt():
    client = FakeClient(
        antwort(suchergebnis("https://a.de"), stop_reason="pause_turn"),
        antwort(text("Fertig.", ["https://a.de"])),
    )

    ergebnis = await web_suche("Frage", client=client)

    assert ergebnis["antwort"] == "Fertig."
    assert len(client.aufrufe) == 2
    assert client.aufrufe[1]["messages"][-1]["role"] == "assistant"


async def test_leere_frage_wird_abgelehnt_ohne_aufruf():
    client = FakeClient()

    ergebnis = await web_suche("   ", client=client)

    assert ergebnis["ok"] is False
    assert client.aufrufe == []


async def test_zeitueberschreitung_liefert_vorlesbaren_fehler():
    client = FakeClient(antwort(text("zu spät")), dauer=1.0)

    ergebnis = await web_suche("Frage", client=client, timeout_s=0.05)

    assert ergebnis == {"ok": False, "error": "Die Suche hat zu lange gedauert."}


async def test_api_fehler_liefert_vorlesbaren_fehler():
    fehler = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x.test"))
    client = FakeClient(fehler=fehler)

    ergebnis = await web_suche("Frage", client=client)

    assert ergebnis == {"ok": False, "error": "Die Suche ist gerade nicht erreichbar."}


async def test_ablehnung_nimmt_den_weg_des_api_fehlers():
    client = FakeClient(
        antwort(
            text("Ich suche kurz."),
            stop_reason="refusal",
            stop_details=NS(type="refusal", category="general_harms", explanation=None),
        )
    )

    ergebnis = await web_suche("Frage", client=client)

    assert ergebnis == {"ok": False, "error": "Die Suche ist gerade nicht erreichbar."}


async def test_ablehnung_nach_pause_turn_verwirft_den_bisherigen_text():
    client = FakeClient(
        antwort(suchergebnis("https://a.de"), text("Halbe Antwort."), stop_reason="pause_turn"),
        antwort(stop_reason="refusal"),
    )

    ergebnis = await web_suche("Frage", client=client)

    assert ergebnis == {"ok": False, "error": "Die Suche ist gerade nicht erreichbar."}
    assert len(client.aufrufe) == 2
    assert all(a["thinking"] == {"type": "disabled"} for a in client.aufrufe)


async def test_antwort_ohne_text_ist_kein_erfolg():
    client = FakeClient(antwort(suchergebnis("https://a.de")))

    ergebnis = await web_suche("Frage", client=client)

    assert ergebnis["ok"] is False


def test_tageszaehler_zaehlt_bis_zum_limit_und_setzt_sich_um_mitternacht_zurueck():
    tag = [date(2026, 10, 4)]
    z = Tageszaehler(2, heute=lambda: tag[0])

    assert [z.versuche(), z.versuche(), z.versuche()] == [True, True, False]
    tag[0] = date(2026, 10, 5)
    assert z.versuche() is True


def test_tageszaehler_mit_limit_null_sperrt_alles():
    assert Tageszaehler(0).versuche() is False


async def test_handler_sagt_bei_erreichtem_tageslimit_ehrlich_nein():
    client = FakeClient(antwort(text("Antwort.")))
    zaehler = Tageszaehler(1)

    erste = await handle_web_suche({"frage": "Eins"}, zaehler=zaehler, client=client)
    zweite = await handle_web_suche({"frage": "Zwei"}, zaehler=zaehler, client=client)

    assert erste["ok"] is True
    assert zweite["ok"] is False
    assert "heute" in zweite["error"]
    assert len(client.aufrufe) == 1


async def test_handler_ohne_frage_verbraucht_kein_tageslimit():
    zaehler = Tageszaehler(1)

    ergebnis = await handle_web_suche({}, zaehler=zaehler, client=FakeClient())

    assert ergebnis["ok"] is False
    assert zaehler.versuche() is True


async def test_unerwarteter_fehler_im_client_wird_abgefangen():
    """Critical: RuntimeError in client.messages.create ist nicht APIError."""
    client = FakeClient(fehler=RuntimeError("Netzwerkstörung"))

    ergebnis = await web_suche("Frage", client=client)

    assert ergebnis == {"ok": False, "error": "Die Suche ist gerade nicht erreichbar."}


async def test_fehlender_api_schluessel_bei_get_client_wird_abgefangen(monkeypatch):
    """Critical: _get_client kann AnthropicError werfen, nicht nur APIError."""
    import kitelefon.agents.websuche as ws

    monkeypatch.setattr(
        ws, "_get_client", lambda: (_ for _ in ()).throw(anthropic.AnthropicError("kein Schlüssel"))
    )

    ergebnis = await web_suche("Frage")

    assert ergebnis == {"ok": False, "error": "Die Suche ist gerade nicht erreichbar."}


async def test_heutiges_datum_wird_im_prompt_eingefuegt():
    """Minor: SYSTEM-Prompt enthält heute Datum für Ankerfragen."""
    from datetime import date

    client = FakeClient(antwort(text("Antwort.")))
    heute_str = date.today().strftime("%d.%m.%Y")

    await web_suche("Frage", client=client)

    aufruf = client.aufrufe[0]
    system_prompt = aufruf["system"]
    assert heute_str in system_prompt, f"Datum {heute_str} nicht in Prompt: {system_prompt}"


def test_get_client_wiederholt_nicht_still(monkeypatch):
    """Kein Wiederholen im Werkzeug: der SDK-Standard (2 Wiederholungen) frisst das 22-s-Limit."""
    from kitelefon.agents import websuche as ws

    monkeypatch.setattr(ws, "_client", None, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    assert ws._get_client().max_retries == 0
