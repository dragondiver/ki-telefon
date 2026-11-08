"""Zwei Antworten gleichzeitig — die Figur redet über sich selbst.

Am 14.08. dreimal im Protokoll: nach einem Werkzeugergebnis ging
bedingungslos ein response.create raus, auch wenn der Server schon
antwortete. Die Folge war ein Fehler vom Server und, im Gespräch mit
Paula, vier Antworten in sieben Sekunden — jede mit "Alles klar" davor.
"""

import json

import pytest
from test_realtime import FakeTransport

from kitelefon.realtime import RealtimeSession, SessionState
from kitelefon.roles import ROLES


def _session(transport, werkzeug=None):
    return RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: None,
        tool_handler=werkzeug or (lambda name, args: {"ok": True}),
    )


def _typen(transport):
    return [m.get("type") for m in transport.sent]


@pytest.mark.asyncio
async def test_kein_zweiter_anstoss_waehrend_sie_spricht():
    transport = FakeTransport()
    session = _session(transport)
    session._state = SessionState.ASSISTANT_RESPONDING

    await session._antworten_lassen()

    assert "response.create" not in _typen(transport)


@pytest.mark.asyncio
async def test_anstoss_wenn_sie_zuhoert():
    transport = FakeTransport()
    session = _session(transport)
    session._state = SessionState.LISTENING

    await session._antworten_lassen()

    assert _typen(transport) == ["response.create"]


@pytest.mark.asyncio
async def test_werkzeugergebnis_stoesst_nicht_doppelt_an():
    """Der Fall aus dem Protokoll: das Werkzeug antwortet, waehrend sie redet."""
    transport = FakeTransport()
    session = _session(transport)
    session._state = SessionState.ASSISTANT_RESPONDING

    await session._dispatch_function_call(
        {
            "name": "delegate_task",
            "call_id": "call_1",
            "arguments": json.dumps({"description": "x", "deliver_via": "email"}),
        }
    )

    typen = _typen(transport)
    assert "conversation.item.create" in typen  # das Ergebnis geht raus
    assert "response.create" not in typen  # aber kein zweiter Anstoss


@pytest.mark.asyncio
async def test_nachfassen_schickt_hinweis_und_stoesst_an():
    transport = FakeTransport()
    session = _session(transport)
    session._state = SessionState.LISTENING

    await session.nachfassen("(Lies das Ergebnis jetzt vor.)")

    gesendet = transport.sent
    assert gesendet[0]["type"] == "conversation.item.create"
    assert gesendet[0]["item"]["role"] == "user"
    assert "Lies das Ergebnis" in gesendet[0]["item"]["content"][0]["text"]
    assert gesendet[1]["type"] == "response.create"
