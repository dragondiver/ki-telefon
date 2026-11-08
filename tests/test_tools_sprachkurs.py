import inspect

import pytest

from kitelefon.tools import ALL_TOOL_SCHEMAS, SPRACHKURS_TOOL_SCHEMAS, make_sprachkurs_tool_handler


def test_schemas_sind_nicht_global():
    namen_global = {s["name"] for s in ALL_TOOL_SCHEMAS}
    assert "note_error" not in namen_global
    assert {s["name"] for s in SPRACHKURS_TOOL_SCHEMAS} == {"note_error", "get_news_fr"}


@pytest.mark.asyncio
async def test_note_error_geht_an_den_call():
    """note_error() greift synchron auf Postgres zu — der Handler muss eine
    Awaitable liefern (asyncio.to_thread), damit realtime.py sie erwarten
    kann, statt den Event-Loop mit einem direkten DB-Zugriff zu blockieren
    (Abschluss-Review F1)."""
    aufrufe = []

    class FakeCall:
        def note_error(self, u, c, k):
            aufrufe.append((u, c, k))
            return {"ok": True}

    handler = make_sprachkurs_tool_handler(FakeCall(), fallback=None)
    out = handler(
        "note_error",
        {"learner_utterance": "à marché", "correction": "au marché", "category": "artikel"},
    )
    assert inspect.isawaitable(out)
    result = await out
    assert result == {"ok": True}
    assert aufrufe == [("à marché", "au marché", "artikel")]


def test_unbekanntes_tool_geht_an_den_fallback():
    def fallback(name, args):
        return {"von": "fallback", "name": name}

    handler = make_sprachkurs_tool_handler(object(), fallback=fallback)
    assert handler("get_weather", {})["von"] == "fallback"
