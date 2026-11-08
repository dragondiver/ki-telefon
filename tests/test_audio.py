import numpy as np

from kitelefon.audio import (
    RATE,
    AudioIO,
    pcm16_dial_tone_frame,
    pcm16_ringback,
)


def test_dial_tone_frame_returns_bytes_of_expected_length():
    frame = pcm16_dial_tone_frame(num_samples=2400, phase=0.0)
    assert isinstance(frame[0], bytes)
    assert len(frame[0]) == 2400 * 2  # PCM16 = 2 bytes/sample
    # phase advanced
    assert frame[1] != 0.0


def test_ringback_total_length():
    pcm = pcm16_ringback(tone_s=1.0, pause_s=4.0, repeats=2)
    expected_bytes = int(RATE * (1.0 + 4.0 + 1.0 + 4.0)) * 2
    assert len(pcm) == expected_bytes


def test_ringback_first_and_silence_sections():
    pcm = pcm16_ringback(tone_s=0.1, pause_s=0.1, repeats=1)
    samples = np.frombuffer(pcm, dtype=np.int16)
    # First section is tone (non-zero), second silence
    n_tone = int(RATE * 0.1)
    assert np.abs(samples[:n_tone]).max() > 0
    assert np.abs(samples[n_tone:]).max() == 0


async def test_volle_mikrofon_warteschlange_wirft_nicht():
    """Sonst flutet jeder Ueberlauf das Protokoll mit Tracebacks.

    Passiert real, sobald niemand mehr mitliest — etwa nach dem Ende einer
    Realtime-Sitzung, waehrend die Audio-Eingabe noch laeuft.
    """
    io = AudioIO()
    while not io._mic_queue.full():
        io._mic_queue.put_nowait(b"x")
    voll_vorher = io._mic_queue.qsize()
    io._einreihen(b"noch einer")  # darf einfach verpuffen
    assert io._mic_queue.qsize() == voll_vorher
