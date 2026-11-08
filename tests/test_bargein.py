"""Unterbrechen muss die Antwort wirklich beenden.

Aus dem Firmenbesuch am 2026-08-05: "Sie lässt sich nicht einfach
unterbrechen." Der lokale Puffer wurde geleert, dem Server aber nichts
gesagt — der erzeugte weiter, die naechsten Pakete kamen an und die
Stimme setzte fort.
"""

import asyncio

import pytest
from test_realtime import FakeTransport

from kitelefon.realtime import RealtimeSession, SessionState
from kitelefon.roles import ROLES


def _session(transport, audio_out):
    return RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: audio_out.append(b),
        tool_handler=lambda name, args: {"ok": True},
    )


async def _bis_gespraech(transport, session):
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.01)
    transport.inject({"type": "session.created"})
    await asyncio.sleep(0.02)
    transport.inject({"type": "session.updated"})
    await asyncio.sleep(0.02)
    return task


@pytest.mark.asyncio
async def test_unterbrechen_bricht_die_antwort_beim_server_ab():
    transport = FakeTransport()
    audio_out: list[bytes] = []
    session = _session(transport, audio_out)
    task = await _bis_gespraech(transport, session)

    transport.inject({"type": "response.created"})
    await asyncio.sleep(0.02)
    assert session.state is SessionState.ASSISTANT_RESPONDING

    transport.inject({"type": "input_audio_buffer.speech_started"})
    await asyncio.sleep(0.02)

    assert any(m.get("type") == "response.cancel" for m in transport.sent), (
        "ohne response.cancel redet der Server weiter"
    )
    assert audio_out[-1] == b""  # lokaler Puffer geleert

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_kein_abbruch_wenn_gar_niemand_spricht():
    """response.cancel ohne laufende Antwort quittiert der Server mit einem
    Fehler — den provozieren wir nicht ohne Grund."""
    transport = FakeTransport()
    session = _session(transport, [])
    task = await _bis_gespraech(transport, session)

    transport.inject({"type": "input_audio_buffer.speech_started"})
    await asyncio.sleep(0.02)

    assert not any(m.get("type") == "response.cancel" for m in transport.sent)

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_verspaeteter_abbruch_beendet_das_gespraech_nicht():
    """Wettlauf: die Antwort war schon fertig, als unser Abbruch eintraf.
    Das ist kein Defekt — die Session deswegen in ERROR zu werfen wuerde
    mitten im Gespraech auflegen."""
    transport = FakeTransport()
    session = _session(transport, [])
    task = await _bis_gespraech(transport, session)

    transport.inject(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Cancellation failed: no active response found",
            },
        }
    )
    await asyncio.sleep(0.02)

    assert session.state is not SessionState.ERROR

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_echte_fehler_gelten_weiterhin_als_fehler():
    transport = FakeTransport()
    session = _session(transport, [])
    task = await _bis_gespraech(transport, session)

    transport.inject(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "unknown parameter 'foo'",
            },
        }
    )
    await asyncio.sleep(0.02)

    assert session.state is SessionState.ERROR

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_vad_werte_gehen_einstellbar_in_die_session():
    """Die Schwelle stand auf 0.85 — damit verstand das Telefon im Büro
    mehrere Kollegen schlecht, besonders tiefe Stimmen."""
    transport = FakeTransport()
    session = RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: None,
        tool_handler=lambda n, a: {},
        vad_threshold=0.45,
        vad_prefix_padding_ms=700,
        vad_silence_ms=600,
    )
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.01)
    transport.inject({"type": "session.created"})
    await asyncio.sleep(0.03)

    update = next(m for m in transport.sent if m["type"] == "session.update")
    vad = update["session"]["audio"]["input"]["turn_detection"]
    assert vad["threshold"] == 0.45
    assert vad["prefix_padding_ms"] == 700
    assert vad["silence_duration_ms"] == 600

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_vorgabe_ist_nicht_mehr_die_alte_hohe_schwelle():
    transport = FakeTransport()
    session = _session(transport, [])
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.01)
    transport.inject({"type": "session.created"})
    await asyncio.sleep(0.03)

    update = next(m for m in transport.sent if m["type"] == "session.update")
    vad = update["session"]["audio"]["input"]["turn_detection"]
    assert vad["threshold"] < 0.85

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_anweisungen_werden_im_langen_gespraech_aufgefrischt():
    """Jean-Lucs Akzent verschwand nach einiger Zeit: je laenger das
    Gespraech, desto weiter rutschen die Anweisungen im Kontext nach
    hinten. Alle N Antworten geht die Session-Konfiguration erneut raus."""
    transport = FakeTransport()
    session = RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: None,
        tool_handler=lambda n, a: {},
        reanchor_every=3,
    )
    task = await _bis_gespraech(transport, session)
    vorher = sum(1 for m in transport.sent if m["type"] == "session.update")

    for _ in range(3):
        transport.inject({"type": "response.done"})
        await asyncio.sleep(0.02)

    nachher = sum(1 for m in transport.sent if m["type"] == "session.update")
    assert nachher == vorher + 1, "nach der dritten Antwort muss aufgefrischt werden"

    await transport.close()
    await task


@pytest.mark.asyncio
async def test_auffrischen_laesst_sich_abschalten():
    transport = FakeTransport()
    session = RealtimeSession(
        transport=transport,
        role=ROLES[0],
        on_audio=lambda b: None,
        tool_handler=lambda n, a: {},
        reanchor_every=0,
    )
    task = await _bis_gespraech(transport, session)
    vorher = sum(1 for m in transport.sent if m["type"] == "session.update")

    for _ in range(6):
        transport.inject({"type": "response.done"})
        await asyncio.sleep(0.01)

    nachher = sum(1 for m in transport.sent if m["type"] == "session.update")
    assert nachher == vorher

    await transport.close()
    await task
