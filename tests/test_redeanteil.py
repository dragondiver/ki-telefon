import base64

import pytest
from test_realtime import FakeTransport

from kitelefon.realtime import RealtimeSession
from kitelefon.roles import ROLES

# tests/test_realtime.py exportiert keinen ROLE (Singular) — dort wird
# ROLES[0] aus kitelefon.roles verwendet. Wir uebernehmen dasselbe Muster,
# statt einen eigenen Fake anzulegen.
ROLE = ROLES[0]


@pytest.mark.asyncio
async def test_ki_sprechzeit_aus_audio_bytes():
    t = FakeTransport()
    s = RealtimeSession(
        transport=t, role=ROLE, on_audio=lambda b: None, tool_handler=lambda n, a: {}
    )
    pcm = b"\x00" * 48000  # exakt 1 Sekunde
    await s._handle_server_event(
        {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()}
    )
    anrufer, ki = s.sprechzeiten
    assert anrufer == 0.0
    assert ki == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_anrufer_sprechzeit_aus_vad_events(monkeypatch):
    t = FakeTransport()
    s = RealtimeSession(
        transport=t, role=ROLE, on_audio=lambda b: None, tool_handler=lambda n, a: {}
    )
    zeit = [100.0]
    monkeypatch.setattr(s, "_jetzt", lambda: zeit[0])
    await s._handle_server_event({"type": "input_audio_buffer.speech_started"})
    zeit[0] = 103.5
    await s._handle_server_event({"type": "input_audio_buffer.speech_stopped"})
    anrufer, _ = s.sprechzeiten
    assert anrufer == pytest.approx(3.5)


@pytest.mark.asyncio
async def test_doppeltes_stop_zaehlt_nicht_doppelt(monkeypatch):
    t = FakeTransport()
    s = RealtimeSession(
        transport=t, role=ROLE, on_audio=lambda b: None, tool_handler=lambda n, a: {}
    )
    zeit = [10.0]
    monkeypatch.setattr(s, "_jetzt", lambda: zeit[0])
    await s._handle_server_event({"type": "input_audio_buffer.speech_started"})
    zeit[0] = 12.0
    await s._handle_server_event({"type": "input_audio_buffer.speech_stopped"})
    await s._handle_server_event({"type": "input_audio_buffer.committed"})
    anrufer, _ = s.sprechzeiten
    assert anrufer == pytest.approx(2.0)
