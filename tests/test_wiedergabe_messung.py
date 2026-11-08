"""Messung der Wiedergabekette (#113): Antwort im Log, aber vom Anrufer nicht gehoert.

Nur Beobachten — die Tests pruefen die Zaehler UND dass der Pufferinhalt nach
play/clear/Callback unveraendert bleibt.
"""

import asyncio
import base64
import logging

import pytest

from kitelefon import audio as audio_modul
from kitelefon.audio import (
    ANLASS_BARGEIN,
    FRAME_SAMPLES,
    JITTER_BUFFER_BYTES,
    AudioIO,
)
from kitelefon.realtime import RealtimeSession
from kitelefon.roles import ROLES

FRAME_BYTES = FRAME_SAMPLES * 2


def _zusammenfassungen(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Antwort ") and "beendet" in r.getMessage()
    ]


def _hardware(audio: AudioIO, n: int) -> list[bytearray]:
    out = []
    for _ in range(n):
        puffer = bytearray(FRAME_BYTES)
        audio._speaker_callback(puffer, FRAME_SAMPLES, None, None)
        out.append(puffer)
    return out


@pytest.mark.asyncio
async def test_zaehler_gespielt_gesendet_und_zusammenfassung_genau_einmal(caplog):
    audio = AudioIO()
    pcm = b"\x01\x00" * (FRAME_SAMPLES * 4)  # 4 Frames = 400 ms
    with caplog.at_level(logging.INFO, logger="kitelefon.audio"):
        audio.antwort_ereignis("beginn")
        await audio.play(pcm)
        audio.antwort_ereignis("ende", status="completed")
        assert not _zusammenfassungen(caplog)  # noch nicht abgespielt
        bloecke = _hardware(audio, 4)
        audio._pruefe_ende()
        audio._pruefe_ende()  # zweiter Aufruf darf nicht doppelt loggen
    # Verhalten: exakt die gesendeten Bytes kommen an der Hardware an.
    assert b"".join(bytes(b) for b in bloecke) == pcm
    zeilen = _zusammenfassungen(caplog)
    assert len(zeilen) == 1
    z = zeilen[0]
    assert f"Modell {len(pcm)} B (4 Frames, 400 ms)" in z
    assert f"an Hardware {len(pcm)} B" in z
    assert "verworfen 0 B" in z
    assert "Rest im Puffer 0 B" in z
    assert "Modellstatus completed" in z


@pytest.mark.asyncio
async def test_bargein_zaehlt_verworfene_bytes_und_nennt_anlass(caplog):
    audio = AudioIO()
    pcm = b"\x01\x00" * (FRAME_SAMPLES * 4)
    with caplog.at_level(logging.INFO, logger="kitelefon.audio"):
        audio.antwort_ereignis("beginn")
        await audio.play(pcm)
        _hardware(audio, 1)  # ein Frame gespielt, drei im Puffer
        await audio.clear_playback(anlass=ANLASS_BARGEIN)
    assert len(audio._playback_buffer) == 0  # Verhalten wie zuvor
    assert audio.is_playing is False
    clear = [r.getMessage() for r in caplog.records if r.getMessage().startswith("clear_playback")]
    assert len(clear) == 1
    assert ANLASS_BARGEIN in clear[0]
    assert f"verworfen={3 * FRAME_BYTES} B" in clear[0]
    zeilen = _zusammenfassungen(caplog)
    assert len(zeilen) == 1
    assert f"verworfen {3 * FRAME_BYTES} B" in zeilen[0]
    assert "Barge-in" in zeilen[0]
    assert f"an Hardware {FRAME_BYTES} B" in zeilen[0]
    # Eine spaet eintreffende "ende"-Meldung darf nicht ein zweites Mal zusammenfassen.
    audio.antwort_ereignis("ende", status="cancelled")
    assert len(_zusammenfassungen(caplog)) == 1


@pytest.mark.asyncio
async def test_clear_playback_ohne_antwort_loggt_nur_anlass(caplog):
    audio = AudioIO()
    await audio.play(b"\x00" * 100)
    with caplog.at_level(logging.INFO, logger="kitelefon.audio"):
        await audio.clear_playback(anlass="Waehlton beendet")
    assert any(
        "Waehlton beendet" in r.getMessage() and "verworfen=100 B" in r.getMessage()
        for r in caplog.records
    )
    assert not _zusammenfassungen(caplog)


@pytest.mark.asyncio
async def test_rest_unter_jitter_schwelle_wird_sichtbar(caplog):
    """Kurze Antwort (< 250 ms) spielt nie an: genau das soll das Log zeigen."""
    audio = AudioIO()
    pcm = b"\x01\x00" * 1000  # 2000 B < JITTER_BUFFER_BYTES
    assert len(pcm) < JITTER_BUFFER_BYTES
    with caplog.at_level(logging.INFO, logger="kitelefon.audio"):
        audio.antwort_ereignis("beginn")
        await audio.play(pcm)
        _hardware(audio, 2)
        audio.antwort_ereignis("ende", status="completed")
    zeilen = _zusammenfassungen(caplog)
    assert len(zeilen) == 1
    assert "an Hardware 0 B" in zeilen[0]
    assert f"Rest im Puffer {len(pcm)} B" in zeilen[0]
    assert "erster Frame nach nie" in zeilen[0]
    assert bytes(audio._playback_buffer) == pcm  # unveraendert


@pytest.mark.asyncio
async def test_pufferstand_bei_beginn_und_ende(caplog):
    audio = AudioIO()
    await audio.play(b"\x00" * 500)
    with caplog.at_level(logging.INFO, logger="kitelefon.audio"):
        audio.antwort_ereignis("beginn")
        await audio.play(b"\x00" * 3000)
        audio.antwort_ereignis("ende", status="completed")
        audio.stop()
    beginn = [r.getMessage() for r in caplog.records if "beginnt" in r.getMessage()]
    assert beginn and "Puffer 500 B" in beginn[0]
    zeilen = _zusammenfassungen(caplog)
    assert len(zeilen) == 1
    assert "Puffer Beginn 500 B / bei Modellende 3500 B" in zeilen[0]


@pytest.mark.asyncio
async def test_status_flags_im_callback_nur_gezaehlt_und_warnung_ratenbegrenzt(caplog, monkeypatch):
    audio = AudioIO()
    uhr = {"t": 100.0}
    monkeypatch.setattr(audio_modul, "_jetzt", lambda: uhr["t"])
    out = bytearray(FRAME_BYTES)
    with caplog.at_level(logging.INFO, logger="kitelefon.audio"):
        audio.antwort_ereignis("beginn")
        for _ in range(5):
            audio._speaker_callback(out, FRAME_SAMPLES, None, "output underflow")
        # Der Callback selbst loggt hoechstens die bestehende Wechselzeile
        # ("PortAudio-Status (Lautsprecher)"), nie die Ratenbegrenzte.
        assert not [r for r in caplog.records if "Ausgabe-Status" in r.getMessage()]
        await audio.play(b"\x00" * 10)  # Event-Loop-Seite: meldet
        await audio.play(b"\x00" * 10)  # innerhalb derselben Sekunde: still
        for _ in range(3):
            audio._speaker_callback(out, FRAME_SAMPLES, None, "output underflow")
        await audio.play(b"\x00" * 10)
        uhr["t"] += 1.5
        await audio.play(b"\x00" * 10)  # Sekunde um: zweite Warnung
        audio.stop()
    warn = [
        r
        for r in caplog.records
        if "Ausgabe-Status" in r.getMessage() and r.levelno == logging.WARNING
    ]
    assert len(warn) == 2
    assert "output underflow x5" in warn[0].getMessage()
    assert "output underflow x3" in warn[1].getMessage()
    zeilen = _zusammenfassungen(caplog)
    assert len(zeilen) == 1
    assert "'output underflow': 8" in zeilen[0]


@pytest.mark.asyncio
async def test_pufferverhalten_unveraendert_gegenueber_ohne_messung():
    """Mit und ohne Messung liefert der Callback dieselben Bytes."""
    pcm = bytes(range(256)) * 80  # 20480 B, nicht Frame-ausgerichtet

    async def lauf(messen: bool):
        audio = AudioIO()
        if messen:
            audio.antwort_ereignis("beginn")
        await audio.play(pcm)
        ausgabe = b"".join(bytes(b) for b in _hardware(audio, 6))
        return ausgabe, bytes(audio._playback_buffer), audio._playback_started

    assert await lauf(True) == await lauf(False)


@pytest.mark.asyncio
async def test_ende_wird_nach_dem_ausspielen_per_timer_gemeldet(caplog):
    audio = AudioIO()
    audio._loop = asyncio.get_running_loop()
    pcm = b"\x01\x00" * (FRAME_SAMPLES * 3)
    with caplog.at_level(logging.INFO, logger="kitelefon.audio"):
        audio.antwort_ereignis("beginn")
        await audio.play(pcm)
        audio.antwort_ereignis("ende", status="completed")
        assert not _zusammenfassungen(caplog)
        _hardware(audio, 4)  # Puffer leer gespielt
        await asyncio.sleep(audio_modul.ENDE_PRUEF_INTERVALL_S + 0.2)
    assert len(_zusammenfassungen(caplog)) == 1


@pytest.mark.asyncio
async def test_realtime_meldet_beginn_und_ende_an_on_audio_antwort():
    from tests.test_realtime import FakeTransport

    ereignisse = []

    class Senke:
        def __call__(self, pcm):
            ereignisse.append(("pcm", len(pcm)))

        def antwort(self, ereignis, **info):
            ereignisse.append((ereignis, info.get("status")))

    transport = FakeTransport()
    session = RealtimeSession(
        transport=transport, role=ROLES[0], on_audio=Senke(), tool_handler=lambda n, a: {}
    )
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.01)
    transport.inject({"type": "response.created"})
    transport.inject(
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02").decode()}
    )
    transport.inject({"type": "response.done", "response": {"status": "completed"}})
    await asyncio.sleep(0.05)
    assert ereignisse == [("beginn", None), ("pcm", 2), ("ende", "completed")]
    await transport.close()
    await task
