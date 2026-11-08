import asyncio
import base64
import json
from typing import Any

import pytest

from kitelefon.realtime import RealtimeSession, SessionState
from kitelefon.roles import ROLES


class FakeTransport:
    """In-memory bidirectional channel for testing RealtimeSession."""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, msg: str) -> None:
        self.sent.append(json.loads(msg))

    async def recv(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise ConnectionError("closed")
        return item

    async def close(self) -> None:
        self.closed = True
        await self._incoming.put(None)

    def inject(self, event: dict[str, Any]) -> None:
        self._incoming.put_nowait(json.dumps(event))


@pytest.mark.asyncio
async def test_session_sends_session_update_on_start():
    transport = FakeTransport()
    audio_out: list[bytes] = []
    session = RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: audio_out.append(b),
        tool_handler=lambda name, args: {"ok": True},
    )

    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.01)
    # Server sends session.created first; client responds with session.update
    transport.inject({"type": "session.created"})
    await asyncio.sleep(0.05)
    assert any(m.get("type") == "session.update" for m in transport.sent)
    update = next(m for m in transport.sent if m["type"] == "session.update")
    assert update["session"]["type"] == "realtime"
    assert update["session"]["output_modalities"] == ["audio"]
    assert update["session"]["audio"]["input"]["format"]["rate"] == 24000
    # speed stand jahrelang nur in roles.py und wurde nie gesendet
    assert update["session"]["audio"]["output"]["speed"] == ROLES[0]["speed"]
    # tool is registered
    tools = update["session"]["tools"]
    assert any(t["name"] == "delegate_task" for t in tools)

    # Whisper-1 halluzinierte am 19.08. "Thank you for watching" in Paulas
    # Transkript (Sitzung 9) — das neuere Modell halluziniert deutlich
    # seltener. Die Figuren sprechen Deutsch: Sprache festgelegt (3.10.).
    transcription = update["session"]["audio"]["input"]["transcription"]
    assert transcription["model"] == "gpt-transcribe"
    assert transcription["language"] == "de"

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_session_decodes_audio_delta_to_on_audio_callback():
    transport = FakeTransport()
    audio_out: list[bytes] = []
    session = RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: audio_out.append(b),
        tool_handler=lambda name, args: {"ok": True},
    )

    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.01)
    payload = b"\x01\x02\x03\x04"
    transport.inject(
        {
            "type": "response.output_audio.delta",
            "delta": base64.b64encode(payload).decode("ascii"),
        }
    )
    await asyncio.sleep(0.05)
    assert audio_out == [payload]

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_session_dispatches_function_call():
    transport = FakeTransport()
    captured: dict[str, Any] = {}

    def handler(name: str, args: dict[str, Any]) -> dict[str, Any]:
        captured["name"] = name
        captured["args"] = args
        return {"ok": True, "task_id": 7}

    session = RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: None,
        tool_handler=handler,
    )

    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.01)
    transport.inject(
        {
            "type": "response.function_call_arguments.done",
            "name": "delegate_task",
            "call_id": "call_abc",
            "arguments": json.dumps(
                {
                    "agent_type": "research",
                    "description": "Find a hotel",
                    "deliver_via": "email",
                    "deliver_to": "me@example.com",
                }
            ),
        }
    )
    await asyncio.sleep(0.05)
    assert captured["name"] == "delegate_task"
    assert captured["args"]["agent_type"] == "research"
    # And the session sent back a function_call_output
    sent_types = [m.get("type") for m in transport.sent]
    assert "conversation.item.create" in sent_types
    item_create = next(m for m in transport.sent if m["type"] == "conversation.item.create")
    assert item_create["item"]["type"] == "function_call_output"
    assert item_create["item"]["call_id"] == "call_abc"
    assert "response.create" in sent_types

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_state_transitions_on_session_updated():
    transport = FakeTransport()
    session = RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: None,
        tool_handler=lambda n, a: {"ok": True},
    )
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.01)
    assert session.state == SessionState.CONNECTING or session.state == SessionState.CONNECTED
    transport.inject({"type": "session.created"})
    transport.inject({"type": "session.updated"})
    await asyncio.sleep(0.05)
    assert session.state == SessionState.LISTENING

    # Seit 19.08.: session.updated begruesst nicht mehr von selbst —
    # das macht der CallManager bei in_call (siehe test_begruessung.py).
    assert all(m.get("type") != "response.create" for m in transport.sent)

    await transport.close()
    await task


def test_transkriptionssprache_ist_deutsch_ausser_im_kurs_und_auf_englisch():
    """3.10.2026: Ohne Sprachangabe erschienen im Protokoll chinesische,
    russische und portugiesische 'Aeusserungen' zu deutschem Gespraech.
    Camille (Franzoesisch/Deutsch gemischt) und der englische Entwickler
    bleiben ohne Pin."""
    from kitelefon.realtime import transkriptions_einstellung

    assert transkriptions_einstellung(ROLES[0])["language"] == "de"
    assert "language" not in transkriptions_einstellung({"sprachkurs": True})
    assert "language" not in transkriptions_einstellung({"sprache": "ENGLISCH"})
