"""PortAudio-Status-Flags duerfen nicht stumm verschwinden.

Sitzungen 4/5 (17.08.) und 9 (19.08.): Der Mikrofon-Stream starb still,
ohne eine einzige Logzeile — beide Callbacks ignorierten ihren
status-Parameter. Jetzt wird jeder Statuswechsel geloggt (dedupliziert,
Callbacks feuern 10x pro Sekunde) und der Zeitpunkt des letzten
Mikrofon-Frames gefuehrt, damit ein Watchdog den Tod erkennen kann.
"""

import asyncio
import logging

from kitelefon.audio import FRAME_SAMPLES, AudioIO


def test_speaker_status_wird_einmal_geloggt(caplog):
    audio = AudioIO()
    out = bytearray(1920)
    with caplog.at_level(logging.WARNING, logger="kitelefon.audio"):
        audio._speaker_callback(out, 960, None, "output underflow")
        audio._speaker_callback(out, 960, None, "output underflow")
    treffer = [r for r in caplog.records if "output underflow" in r.message]
    assert len(treffer) == 1  # dedupliziert, nicht 10x pro Sekunde


def test_neuer_speaker_status_wird_wieder_geloggt(caplog):
    audio = AudioIO()
    out = bytearray(1920)
    with caplog.at_level(logging.WARNING, logger="kitelefon.audio"):
        audio._speaker_callback(out, 960, None, "output underflow")
        audio._speaker_callback(out, 960, None, "priming output")
    meldungen = "\n".join(r.message for r in caplog.records)
    assert "output underflow" in meldungen
    assert "priming output" in meldungen


def test_mic_status_wird_geloggt(caplog):
    audio = AudioIO()

    async def lauf():
        audio._loop = asyncio.get_running_loop()
        with caplog.at_level(logging.WARNING, logger="kitelefon.audio"):
            audio._mic_callback(b"\x00" * (FRAME_SAMPLES * 2), 960, None, "input overflow")
        return [r for r in caplog.records if "input overflow" in r.message]

    treffer = asyncio.run(lauf())
    assert len(treffer) == 1


def test_sekunden_seit_mic_frame():
    audio = AudioIO()
    assert audio.sekunden_seit_mic_frame() is None  # noch nie ein Frame

    async def lauf():
        audio._loop = asyncio.get_running_loop()
        audio._einreihen(b"\x00" * (FRAME_SAMPLES * 2))
        return audio.sekunden_seit_mic_frame()

    alter = asyncio.run(lauf())
    assert alter is not None and 0 <= alter < 1.0


def test_stille_frames_zaehlen_nicht_als_signal():
    """Kern von #41: Ein Geraet, das im Takt Nullen liefert, hat einen
    Puls, aber keinen Inhalt. Sitzung 11 (05.09.) lief 62 s genau so."""
    audio = AudioIO()
    assert audio.sekunden_seit_signal() is None  # noch nie Signal

    async def lauf():
        audio._loop = asyncio.get_running_loop()
        audio._einreihen(b"\x00" * (FRAME_SAMPLES * 2))  # digitale Stille
        return audio.sekunden_seit_mic_frame(), audio.sekunden_seit_signal()

    puls, signal = asyncio.run(lauf())
    assert puls is not None  # Frame ist angekommen …
    assert signal is None  # … aber ohne Inhalt


def test_raumgeraeusch_zaehlt_als_signal():
    """Der Gegentest zur Schwelle: Messung am 06.09. auf kitelefon ergab
    fuer einen stillen Raum Spitzen um 2400 — das muss deutlich als
    Signal durchgehen, sonst erklaert der Wachhund eine nachdenkende
    Paula fuer defekt."""
    import struct

    from kitelefon.audio import SIGNAL_SCHWELLE

    leise = struct.pack(f"<{FRAME_SAMPLES}h", *([2400, -2400] * (FRAME_SAMPLES // 2)))

    async def lauf():
        audio = AudioIO()
        audio._loop = asyncio.get_running_loop()
        audio._einreihen(leise)
        return audio.sekunden_seit_signal()

    assert SIGNAL_SCHWELLE < 2400, "Schwelle darf Raumgeraeusch nicht schlucken"
    alter = asyncio.run(lauf())
    assert alter is not None and alter < 1.0


def test_restart_input_setzt_den_signal_stempel_zurueck():
    """Ein Neustart ist ein Neuanfang — auch fuer den Inhalt. Sonst hielte
    der Wachhund das Signal von vor dem Neustart fuer noch gueltig und
    gaebe dem frischen Stream nie seine Frist.

    Nutzt dieselbe Naht wie tests/test_restart_input.py (Unterklasse statt
    Monkeypatch), damit der echte restart_input-Pfad laeuft.
    """
    from test_restart_input import FakeAudioIO

    async def lauf():
        audio = FakeAudioIO()
        audio._loop = asyncio.get_running_loop()
        audio.start_input()
        audio._einreihen(b"\x11\x11" * FRAME_SAMPLES)
        assert audio.sekunden_seit_signal() is not None
        audio.restart_input()
        return audio.sekunden_seit_signal(), audio.sekunden_seit_mic_frame()

    signal, puls = asyncio.run(lauf())
    assert signal is None
    assert puls is None


def test_vollausschlag_gilt_als_signal():
    """Review-Fund (06.09.): np.abs() liefert fuer int16 -32768 wieder
    -32768 — ein voll ausgesteuerter Frame haette als stumm gegolten,
    ausgerechnet in der Pruefung, die Stille erkennen soll."""
    import struct

    voll = struct.pack(f"<{FRAME_SAMPLES}h", *([-32768] * FRAME_SAMPLES))

    async def lauf():
        audio = AudioIO()
        audio._loop = asyncio.get_running_loop()
        audio._einreihen(voll)
        return audio.sekunden_seit_signal()

    assert asyncio.run(lauf()) is not None
