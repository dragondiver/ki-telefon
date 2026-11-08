"""Backend-Tausch im laufenden Gespraech (Freisprechen, #38 Bauabschnitt 2).

audio_umschalten() legt den Audioweg waehrend eines Anrufs auf ein anderes
Backend (Elektra ueber NetzAudioIO), audio_zurueck() holt ihn auf den Hoerer.
Die OpenAI-Sitzung merkt nichts: Mikrofonframes des neuen Backends erreichen
session.send_mic_frame, Sitzungsaudio landet im play() des neuen Backends.
Verliert das Netz-Backend das Geraet, faellt der CallManager von selbst auf
den Hoerer zurueck; Auflegen gibt das Geraet zurueck (Spec §10.3).

Die Fakes hier haben die volle AudioIO-Schnittstelle inklusive der
Wachhund-Helfer — anders als FakeAudio in test_call_manager, das nur den
Anruf-Rahmen abdeckt.
"""

import asyncio
from typing import Any

import pytest
from test_call_manager import FakeDialer, FakeSession
from test_netz_audio import TOKEN, FakeBridge

from kitelefon.call_manager import CallManager, CallState
from kitelefon.hookswitch import HookEvent
from kitelefon.netz_audio import GeraetBesetzt, NetzAudioIO
from kitelefon.tasks import TaskQueue


class GesteuerterHook:
    """Abheben sofort, Auflegen erst auf Zuruf."""

    def __init__(self):
        self._auflegen = asyncio.Event()

    def auflegen(self) -> None:
        self._auflegen.set()

    async def events(self):
        yield HookEvent.LIFTED
        await self._auflegen.wait()
        yield HookEvent.HUNG_UP


class FakeBackend:
    """AudioIO-Schnittstelle mit einer Mikrofon-Warteschlange fuer den Test.

    `synchron=True` verhaelt sich wie AudioIO (start/stop sind gewoehnliche
    Methoden), sonst wie NetzAudioIO (Coroutinen). None in der Warteschlange
    beendet mic_frames() — so meldet NetzAudioIO den Verlust des Geraets.
    """

    def __init__(self, *, synchron: bool = True):
        self.synchron = synchron
        self.frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.played: list[bytes] = []
        self.starts = 0
        self.stops = 0
        self.laeuft = False
        self.verloren = asyncio.Event()
        self.besetzt: str | None = None

    def _start(self) -> None:
        if self.besetzt is not None:
            raise GeraetBesetzt(self.besetzt)
        self.starts += 1
        self.laeuft = True

    def _stop(self) -> None:
        self.stops += 1
        self.laeuft = False

    def start_output(self) -> Any:
        if self.synchron:
            return self._start()
        return asyncio.sleep(0, result=self._start())

    def start_input(self) -> None:
        pass

    def stop(self) -> Any:
        if self.synchron:
            return self._stop()
        return asyncio.sleep(0, result=self._stop())

    async def play(self, pcm: bytes) -> None:
        self.played.append(pcm)

    async def clear_playback(self, anlass: str = "") -> None:
        self.played.append(b"")

    @property
    def is_playing(self) -> bool:
        return False

    async def mic_frames(self):
        while True:
            frame = await self.frames.get()
            if frame is None:
                return
            yield frame

    def sekunden_seit_mic_frame(self):
        return 0.0

    def sekunden_seit_signal(self):
        return 0.0

    def restart_input(self) -> None:
        pass


class SessionMitMikrofon(FakeSession):
    def __init__(self):
        super().__init__()
        self.frames: list[bytes] = []

    async def send_mic_frame(self, pcm: bytes) -> None:
        self.frames.append(pcm)


async def _bis(bedingung, timeout=3.0):
    frist = asyncio.get_running_loop().time() + timeout
    while not bedingung():
        assert asyncio.get_running_loop().time() < frist, "Bedingung nicht erreicht"
        await asyncio.sleep(0.01)


@pytest.fixture
def im_gespraech(tmp_path):
    """CallManager im Zustand IN_CALL mit Hoerer-Backend; raeumt beim Auflegen auf."""

    class Rahmen:
        pass

    r = Rahmen()
    r.hook = GesteuerterHook()
    r.hoerer = FakeBackend()
    r.sessions: list[SessionMitMikrofon] = []

    def open_session(role, on_audio, tool_handler):
        s = SessionMitMikrofon()
        r.sessions.append(s)
        return s

    r.cm = CallManager(
        hookswitch=r.hook,
        dialer=FakeDialer(digit=1),
        audio=r.hoerer,
        open_session=open_session,
        queue=TaskQueue(tmp_path / "tasks.db"),
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
        half_duplex=False,
    )
    return r


async def _anrufen(r) -> asyncio.Task:
    lauf = asyncio.create_task(r.cm.run())
    await _bis(lambda: r.cm.state is CallState.IN_CALL and r.cm._mikro_task is not None)
    return lauf


async def _auflegen(r, lauf: asyncio.Task) -> None:
    r.hook.auflegen()
    await asyncio.wait_for(lauf, 3.0)


@pytest.mark.asyncio
async def test_umschalten_haelt_die_sitzung_am_laufen(im_gespraech):
    r = im_gespraech
    lauf = await _anrufen(r)
    session = r.sessions[0]
    r.hoerer.frames.put_nowait(b"hoerer-1")
    await _bis(lambda: session.frames == [b"hoerer-1"])

    netz = FakeBackend(synchron=False)
    vorher = list(r.hoerer.played)  # Freizeichen und Rufton aus dem Anrufaufbau
    await r.cm.audio_umschalten(netz)

    assert netz.starts == 1
    assert r.hoerer.stops == 1, "das alte Backend wird gestoppt"
    assert r.cm.state is CallState.IN_CALL
    assert not session.closed

    # Mikrofon des neuen Backends erreicht die Sitzung ...
    netz.frames.put_nowait(b"netz-1")
    await _bis(lambda: session.frames == [b"hoerer-1", b"netz-1"])
    # ... und ein Frame aus dem alten kommt nicht mehr an.
    r.hoerer.frames.put_nowait(b"hoerer-tot")
    await asyncio.sleep(0.05)
    assert session.frames == [b"hoerer-1", b"netz-1"]

    # Sitzungsaudio landet im neuen Backend, Barge-in ebenfalls.
    r.cm._on_audio_from_session(b"ton")
    r.cm._on_audio_from_session(b"")
    await _bis(lambda: netz.played == [b"ton", b""])
    assert r.hoerer.played == vorher

    await _auflegen(r, lauf)
    assert netz.stops == 1, "Auflegen gibt das Geraet zurueck"
    assert session.closed


@pytest.mark.asyncio
async def test_zurueck_zum_hoerer(im_gespraech):
    r = im_gespraech
    lauf = await _anrufen(r)
    session = r.sessions[0]
    netz = FakeBackend(synchron=False)
    await r.cm.audio_umschalten(netz)
    hoerer_starts = r.hoerer.starts

    await r.cm.audio_zurueck()
    assert r.hoerer.starts == hoerer_starts + 1
    assert netz.stops == 1
    assert r.cm.state is CallState.IN_CALL

    r.hoerer.frames.put_nowait(b"wieder-da")
    await _bis(lambda: session.frames == [b"wieder-da"])
    r.cm._on_audio_from_session(b"ton")
    await _bis(lambda: r.hoerer.played[-1:] == [b"ton"])
    assert netz.played == []

    await r.cm.audio_zurueck()  # ohne Wirkung, wenn der Hoerer schon aktiv ist
    assert r.hoerer.starts == hoerer_starts + 1

    await _auflegen(r, lauf)
    assert netz.stops == 1
    assert r.hoerer.stops == 2


@pytest.mark.asyncio
async def test_verloren_faellt_von_selbst_auf_den_hoerer_zurueck(im_gespraech, caplog):
    r = im_gespraech
    lauf = await _anrufen(r)
    session = r.sessions[0]
    netz = FakeBackend(synchron=False)
    await r.cm.audio_umschalten(netz)

    # Wie NetzAudioIO bei geraet_weg: verloren setzen, mic_frames() beenden.
    netz.verloren.set()
    netz.frames.put_nowait(None)
    await _bis(lambda: r.cm._audio is r.hoerer)
    assert r.cm.state is CallState.IN_CALL
    assert not session.closed
    assert netz.stops == 1
    assert "zurueck zum Hoerer" in caplog.text

    r.hoerer.frames.put_nowait(b"nach-dem-verlust")
    await _bis(lambda: session.frames == [b"nach-dem-verlust"])

    await _auflegen(r, lauf)
    assert session.closed


@pytest.mark.asyncio
async def test_besetzt_laesst_alles_beim_alten(im_gespraech):
    r = im_gespraech
    lauf = await _anrufen(r)
    session = r.sessions[0]
    netz = FakeBackend(synchron=False)
    netz.besetzt = "Radio läuft"

    with pytest.raises(GeraetBesetzt, match="Radio"):
        await r.cm.audio_umschalten(netz)

    assert r.cm._audio is r.hoerer
    assert r.hoerer.stops == 0
    r.hoerer.frames.put_nowait(b"weiter")
    await _bis(lambda: session.frames == [b"weiter"])
    await _auflegen(r, lauf)


@pytest.mark.asyncio
async def test_umschalten_ausserhalb_eines_gespraechs_ist_ein_fehler(im_gespraech):
    r = im_gespraech
    with pytest.raises(RuntimeError):
        await r.cm.audio_umschalten(FakeBackend())


@pytest.mark.asyncio
async def test_auflegen_gibt_das_geraet_an_die_bridge_zurueck(im_gespraech):
    """Ende-zu-Ende mit dem echten NetzAudioIO: die Bridge sieht `zurueckgeben`."""
    r = im_gespraech
    async with FakeBridge() as bridge:
        lauf = await _anrufen(r)
        session = r.sessions[0]
        netz = NetzAudioIO(bridge.url, TOKEN)
        await r.cm.audio_umschalten(netz)
        await asyncio.wait_for(bridge.verliehen.wait(), 2.0)

        await bridge.sende(b"\x00\x10" * 1440)
        await _bis(lambda: len(session.frames) == 1)
        r.cm._on_audio_from_session(b"\x01" * 100)
        await _bis(lambda: bridge.audio() == b"\x01" * 100)

        await _auflegen(r, lauf)
        await asyncio.wait_for(bridge.geschlossen.wait(), 2.0)
        assert bridge.texte()[-1] == {"type": "zurueckgeben"}
        assert not netz.verbunden
        assert r.hoerer.stops == 2  # einmal beim Umschalten, einmal beim Auflegen
        assert r.cm._audio is r.hoerer
