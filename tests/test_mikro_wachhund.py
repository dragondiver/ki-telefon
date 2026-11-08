"""Der Wachhund erkennt den stillen Mikrofon-Tod und heilt ihn.

Sitzung 9 (19.08.): 89 s stumme Leitung, Prozess lief normal weiter,
Paula legte entnervt auf. Ein lebender Input-Stream liefert auch bei
Stille kontinuierlich Frames — bleibt der Nachschub laenger als
MIC_TOT_S aus, ist der Stream tot und wird neu geoeffnet, hoechstens
MIC_RESTART_VERSUCHE mal pro Anruf.

Sitzung 11 (05.09.) zeigte die zweite Todesart: Frames kamen im Takt,
aber es stand nichts drin — 62 s lang kein einziges Sprech-Ereignis,
waehrend der Wachhund nach damaliger Logik zu Recht schwieg. Seither
zaehlt auch der Inhalt (still_s, #41).
"""

import asyncio

import pytest

from kitelefon.call_manager import CallManager


class FakeAudio:
    def __init__(self):
        self.alter: float | None = 0.0  # None = noch nie ein Frame
        self.stille: float | None = 0.0  # Alter des letzten Frames MIT Signal
        self.restarts = 0
        self.gespielt: list[bytes] = []

    def sekunden_seit_mic_frame(self):
        return self.alter

    def sekunden_seit_signal(self):
        return self.stille

    def restart_input(self):
        self.restarts += 1
        self.alter = 0.0  # Neustart bringt den Nachschub zurueck
        self.stille = 0.0

    async def play(self, pcm: bytes) -> None:
        # Der Wachhund spielt die Ansage aus #20 ab, wenn er aufgibt.
        # Ohne diese Methode verschluckt das contextlib.suppress im
        # Aufgeben-Zweig einen AttributeError — die Tests waeren gruen,
        # obwohl Paula nichts hoert.
        self.gespielt.append(pcm)


@pytest.mark.asyncio
async def test_wachhund_startet_toten_stream_neu():
    audio = FakeAudio()
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05)
    )
    audio.alter = 0.3  # laenger tot als tot_s
    await asyncio.sleep(0.05)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert audio.restarts == 1


@pytest.mark.asyncio
async def test_wachhund_gibt_nach_zwei_versuchen_auf():
    audio = FakeAudio()

    def restart_ohne_wirkung():
        audio.restarts += 1  # Stream bleibt tot

    audio.restart_input = restart_ohne_wirkung
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05)
    )
    audio.alter = 999.0
    await asyncio.sleep(0.2)  # Zeit fuer viele Pruefzyklen
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert audio.restarts == 2  # MIC_RESTART_VERSUCHE, nicht mehr


@pytest.mark.asyncio
async def test_neustart_bekommt_sein_eigenes_zeitfenster():
    """Nach einem Neustart zaehlt das Alter ab dem Neustart (#25, Punkt 1).

    ``start_input()`` setzt den Frame-Zeitstempel auf None. Wuerde der
    None-Zweig weiter gegen den Wachhund-Start rechnen, waere das Alter
    sofort wieder groesser als tot_s und der zweite Versuch kaeme im
    naechsten Pruefzyklus — auf dem Pi verbrennt so bei traegem
    Device-Open das ganze Budget, ohne dem frischen Stream je eine
    Chance zu geben.

    Hier liefert das Mikro dauerhaft None. Der erste Versuch faellt nach
    tot_s; der zweite darf fruehestens tot_s spaeter kommen.
    """
    audio = FakeAudio()
    audio.alter = None  # noch nie ein Frame

    def restart_ohne_wirkung():
        audio.restarts += 1
        audio.alter = None  # wie start_input(): Zeitstempel weg

    audio.restart_input = restart_ohne_wirkung
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.1)
    )
    # 0,15 s: der erste Versuch (bei 0,1 s) ist gefallen, der zweite waere
    # mit dem alten Verhalten bei ~0,11 s gekommen, mit dem neuen bei 0,2 s.
    await asyncio.sleep(0.15)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert audio.restarts == 1


@pytest.mark.asyncio
async def test_wachhund_bleibt_ruhig_solange_frames_kommen():
    audio = FakeAudio()
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05)
    )
    audio.alter = 0.0
    await asyncio.sleep(0.1)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert audio.restarts == 0


@pytest.mark.asyncio
async def test_wachhund_wartet_auf_den_ersten_frame():
    # None = seit start_input() kam noch nie ein Frame — direkt nach dem
    # Abheben normal, kein Grund fuer einen Neustart in den ersten Sekunden.
    audio = FakeAudio()
    audio.alter = None
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05)
    )
    await asyncio.sleep(0.03)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert audio.restarts == 0


@pytest.mark.asyncio
async def test_frames_ohne_signal_gelten_als_tot():
    """Sitzung 11 (05.09.): Das Geraet lieferte im Takt Frames, in denen
    nichts stand. Fuer Paula ist das dasselbe wie ein toter Stream — sie
    redet ins Leere, und niemand merkt es."""
    audio = FakeAudio()
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05, still_s=0.05)
    )
    audio.alter = 0.0  # Puls einwandfrei
    audio.stille = 0.3  # aber seit 0,3 s kein Signal
    await asyncio.sleep(0.05)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert audio.restarts == 1


@pytest.mark.asyncio
async def test_leiser_raum_ist_kein_toter_stream():
    """Der Gegentest — und der Grund fuer die sehr niedrige Schwelle in
    audio.SIGNAL_SCHWELLE: Wer nachdenkt, statt zu reden, erzeugt trotzdem
    Raumgeraeusch. Solange Signal ankommt, hat der Wachhund zu schweigen,
    egal wie lange niemand spricht."""
    audio = FakeAudio()
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05, still_s=0.05)
    )
    audio.alter = 0.0
    audio.stille = 0.0  # Raumgeraeusch kommt weiter an
    await asyncio.sleep(0.15)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert audio.restarts == 0


@pytest.mark.asyncio
async def test_nie_signal_zaehlt_ab_anrufbeginn():
    """Wie beim fehlenden Puls: Kam seit dem Abheben ueberhaupt nie
    Signal, laeuft die Frist ab Wachhund-Start — sonst wartet er ewig auf
    einen Zeitstempel, den es nie geben wird."""
    audio = FakeAudio()
    audio.alter = 0.0
    audio.stille = None  # noch nie ein Frame mit Inhalt
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05, still_s=0.05)
    )
    await asyncio.sleep(0.12)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert audio.restarts >= 1
