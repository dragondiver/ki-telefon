"""NetzAudioIO gegen eine Fake-Bridge im Prozess (Freisprechen, #38 Bauabschnitt 2).

Die Bridge hier spricht den Vertrag aus rundfunk/src/rundfunk/verleih.py
(rundfunk#88): Token-Header, `verleihen` → `verliehen`/`besetzt`, Binaer-
frames in beide Richtungen, `leeren`, `zurueckgeben`, `wiedergabe`,
`geraet_weg`, 4401 bei falschem Token. Kein Geraet, kein Netz nach draussen,
kein Ton auf dem Mac — ein echter websockets-Server auf 127.0.0.1.
"""

import asyncio
import contextlib
import json

import pytest
import websockets

from kitelefon.netz_audio import GeraetBesetzt, NetzAudioIO, VerleihFehler

TOKEN = "test-token"
STILL = b"\x00" * 2880  # 60 ms Nullen
LAUT = b"\x00\x10" * 1440  # 60 ms mit Pegel 4096


class FakeBridge:
    """Ein Verleih-Endpunkt wie in rundfunk: nimmt genau einen Client an."""

    def __init__(self, *, besetzt: str | None = None):
        self.besetzt = besetzt
        self.empfangen: list[bytes | str] = []  # alles vom Client nach `verleihen`
        self.verliehen = asyncio.Event()
        self.geschlossen = asyncio.Event()
        self._ws = None
        self._server = None
        self.url = ""

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/verleih"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws):
        if ws.request.headers.get("X-Agent-Token") != TOKEN:
            await ws.close(4401, "Token")
            return
        erste = json.loads(await ws.recv())
        assert erste == {"type": "verleihen"}
        if self.besetzt is not None:
            await ws.send(json.dumps({"type": "besetzt", "grund": self.besetzt}))
            await ws.close()
            return
        self._ws = ws
        await ws.send(json.dumps({"type": "verliehen"}))
        self.verliehen.set()
        try:
            async for nachricht in ws:
                self.empfangen.append(nachricht)
                if nachricht == json.dumps({"type": "zurueckgeben"}):
                    await ws.close()
                    break
        except websockets.ConnectionClosed:
            pass
        finally:
            self.geschlossen.set()

    async def sende(self, daten: bytes | dict) -> None:
        if isinstance(daten, dict):
            daten = json.dumps(daten)
        await self._ws.send(daten)

    async def geraet_weg(self) -> None:
        await self.sende({"type": "geraet_weg"})
        await self._ws.close()

    def texte(self) -> list[dict]:
        return [json.loads(m) for m in self.empfangen if isinstance(m, str)]

    def audio(self) -> bytes:
        return b"".join(m for m in self.empfangen if isinstance(m, bytes))


async def _bis(bedingung, timeout=2.0):
    frist = asyncio.get_running_loop().time() + timeout
    while not bedingung():
        assert asyncio.get_running_loop().time() < frist, "Bedingung nicht erreicht"
        await asyncio.sleep(0.01)


async def _erstes_frame(audio: NetzAudioIO) -> bytes:
    it = audio.mic_frames()
    frame = await asyncio.wait_for(anext(it), 2.0)
    await it.aclose()
    return frame


@pytest.mark.asyncio
async def test_verliehen_frames_fliessen_in_beide_richtungen():
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN)
        await audio.start_output()
        audio.start_input()
        assert audio.verbunden

        await bridge.sende(LAUT)
        assert await _erstes_frame(audio) == LAUT

        pcm = bytes(range(256)) * 50  # 12800 Bytes -> 3 Stuecke (4800/4800/3200)
        await audio.play(pcm)
        await _bis(lambda: len(bridge.audio()) == len(pcm))
        stuecke = [m for m in bridge.empfangen if isinstance(m, bytes)]
        assert [len(s) for s in stuecke] == [4800, 4800, 3200]
        assert bridge.audio() == pcm

        await audio.stop()
        await asyncio.wait_for(bridge.geschlossen.wait(), 2.0)
        assert bridge.texte()[-1] == {"type": "zurueckgeben"}
        assert not audio.verbunden
        await audio.stop()  # idempotent


@pytest.mark.asyncio
async def test_besetzt_wirft_geraet_besetzt():
    async with FakeBridge(besetzt="Radio läuft") as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN)
        with pytest.raises(GeraetBesetzt) as info:
            await audio.start_output()
        assert info.value.grund == "Radio läuft"
        assert not audio.verbunden


@pytest.mark.asyncio
async def test_falsches_token_wirft_verleihfehler():
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, "falsch", verleih_timeout_s=1.0)
        with pytest.raises(VerleihFehler, match="4401"):
            await audio.start_output()
        assert not audio.verbunden


@pytest.mark.asyncio
async def test_ohne_bridge_wirft_verleihfehler():
    audio = NetzAudioIO("ws://127.0.0.1:1/verleih", TOKEN)
    with pytest.raises(VerleihFehler, match="keine Verbindung"):
        await audio.start_output()


@pytest.mark.asyncio
async def test_leeren_verwirft_die_warteschlange_und_meldet_der_bridge():
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN)
        await audio.start_output()
        # Sender noch nicht dran: play() reiht nur ein, clear_playback() wirft weg.
        await audio.play(b"\x01" * 48000)
        await audio.clear_playback()
        await _bis(lambda: bridge.texte() and bridge.texte()[-1] == {"type": "leeren"})
        assert bridge.audio() == b""
        await audio.stop()


@pytest.mark.asyncio
async def test_wiedergabe_steuert_is_playing():
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN)
        await audio.start_output()
        assert not audio.is_playing
        await bridge.sende({"type": "wiedergabe", "aktiv": True})
        await _bis(lambda: audio.is_playing)
        await bridge.sende({"type": "wiedergabe", "aktiv": False})
        await _bis(lambda: not audio.is_playing)
        await audio.stop()


@pytest.mark.asyncio
async def test_wachhund_helfer_sehen_wiedergabe_als_puls_und_signal():
    """30 s Wiedergabe ohne ein einziges Mikrofonframe darf nicht als Tod gelten."""
    uhr = [1000.0]
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN, uhr=lambda: uhr[0])
        await audio.start_output()
        assert audio.sekunden_seit_mic_frame() is None
        assert audio.sekunden_seit_signal() is None

        await bridge.sende(STILL)
        await _bis(lambda: audio.frames_vom_geraet == 1)
        uhr[0] += 3.0
        assert audio.sekunden_seit_mic_frame() == pytest.approx(3.0)
        assert audio.sekunden_seit_signal() is None  # Nullen sind kein Signal

        await bridge.sende({"type": "wiedergabe", "aktiv": True})
        await _bis(lambda: audio.is_playing)
        uhr[0] += 30.0  # das Geraet spielt, die Bridge schickt keine Frames
        assert audio.sekunden_seit_mic_frame() == 0.0
        assert audio.sekunden_seit_signal() == 0.0

        await bridge.sende({"type": "wiedergabe", "aktiv": False})
        await _bis(lambda: not audio.is_playing)
        uhr[0] += 4.0  # seit dem Ende der Wiedergabe: 4 s, unter MIC_TOT_S
        assert audio.sekunden_seit_mic_frame() == pytest.approx(4.0)
        assert audio.sekunden_seit_signal() == pytest.approx(4.0)

        await bridge.sende(LAUT)
        await _bis(lambda: audio.frames_vom_geraet == 2)
        uhr[0] += 1.0
        assert audio.sekunden_seit_signal() == pytest.approx(1.0)
        await audio.stop()


@pytest.mark.asyncio
async def test_geraet_weg_setzt_verloren_und_beendet_mic_frames():
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN)
        await audio.start_output()
        frames = []

        async def pumpe():
            async for f in audio.mic_frames():
                frames.append(f)

        task = asyncio.create_task(pumpe())
        await bridge.sende(LAUT)
        await _bis(lambda: len(frames) == 1)
        await bridge.geraet_weg()
        await asyncio.wait_for(audio.verloren.wait(), 2.0)
        await asyncio.wait_for(task, 2.0)  # der Iterator endet von selbst
        assert frames == [LAUT]
        await audio.stop()


@pytest.mark.asyncio
async def test_bridge_schliesst_setzt_verloren():
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN)
        await audio.start_output()
        await bridge._ws.close()
        await asyncio.wait_for(audio.verloren.wait(), 2.0)
        await audio.stop()


@pytest.mark.asyncio
async def test_restart_input_verbindet_neu():
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN)
        await audio.start_output()
        await asyncio.wait_for(bridge.verliehen.wait(), 2.0)
        bridge.verliehen.clear()
        # Wie der Wachhund: blockierend in einem Thread.
        await asyncio.to_thread(audio.restart_input)
        await asyncio.wait_for(bridge.verliehen.wait(), 2.0)
        assert audio.verbunden
        assert not audio.verloren.is_set()
        texte = bridge.texte()
        assert {"type": "zurueckgeben"} in texte  # die alte Verbindung wurde sauber beendet
        await bridge.sende(LAUT)
        assert await _erstes_frame(audio) == LAUT
        await audio.stop()


@pytest.mark.asyncio
async def test_restart_input_ohne_bridge_wirft_und_setzt_verloren():
    async with FakeBridge() as bridge:
        audio = NetzAudioIO(bridge.url, TOKEN)
        await audio.start_output()
    # Server ist zu: der Neuaufbau muss scheitern, die Leitung gilt als verloren.
    with pytest.raises(VerleihFehler):
        await asyncio.to_thread(audio.restart_input)
    assert audio.verloren.is_set()
    with contextlib.suppress(Exception):
        await audio.stop()
