"""NetzAudioIO — das Sprachgerät im Wohnzimmer (Elektra) als Audiogerät des Telefons.

Freisprechen, Bauabschnitt 2 (telefon-ai#38). Die OpenAI-Sitzung bleibt beim
Telefon; nur der Audioweg wechselt: statt PyAudio (`AudioIO`, Hörer) laufen
PCM16-Frames über eine WebSocket-Verbindung zur rundfunk-Bridge, die das
Gerät verleiht. Vertrag: `rundfunk/src/rundfunk/verleih.py` (rundfunk#88),
Kurzfassung:

    ws://<bridge>:8000/verleih, Header X-Agent-Token
    Client → Bridge   {"type": "verleihen"} (erste Nachricht, binnen 10 s),
                      <binär> PCM16 mono 24 kHz LE, {"type": "leeren"},
                      {"type": "zurueckgeben"}
    Bridge → Client   {"type": "verliehen"} | {"type": "besetzt", "grund": …},
                      <binär> Mikrofon (nur solange das Gerät zuhört),
                      {"type": "wiedergabe", "aktiv": true|false},
                      {"type": "geraet_weg"}
    Falsches Token: Schließen mit 4401.

Die Klasse hat dieselbe Schnittstelle wie `AudioIO` (duck-typed), damit der
CallManager sie im laufenden Gespräch austauschen kann — mit zwei
Abweichungen, die der CallManager über `inspect.isawaitable` abfängt:
`start_output()` und `stop()` sind Coroutinen, weil sie über das Netz reden.

Wachhund (call_manager.MIC_TOT_S / MIC_STILL_S): Während das Gerät spielt,
schickt die Bridge *absichtlich* keine Mikrofonframes (Halbduplex in der
Firmware). Damit der Mikro-Wachhund das nicht als toten Stream deutet,
zählen `wiedergabe`-Nachrichten und die laufende Wiedergabe als Puls UND
Signal: `sekunden_seit_*` liefern das Alter des jüngsten von
(letzter Frame [mit Signal], letzte wiedergabe-Nachricht, jetzt-falls-spielt).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import numpy as np

from kitelefon.audio import SIGNAL_SCHWELLE

log = logging.getLogger("kitelefon.netz_audio")

# Größte Binärnachricht ans Gerät: 100 ms bei 24 kHz PCM16. Kleinere Stücke,
# damit ein `leeren` (Barge-in) dazwischenkommt, statt hinter einem
# sekundenlangen Block zu warten.
STUECK_BYTES = 4800
# So lange gilt die Wiedergabe als laufend, nachdem das letzte Stück raus
# ist und bevor die Bridge `wiedergabe aktiv:true` gemeldet hat.
SENDE_NACHLAUF_S = 0.1
# Antwort auf `verleihen` — die Bridge schließt nach 10 s ohne Nachricht.
VERLEIH_TIMEOUT_S = 10.0
VERBINDUNGS_TIMEOUT_S = 5.0
ZURUECKGEBEN_TIMEOUT_S = 2.0
MIKRO_VORRAT = 64

_LEEREN = object()

Verbinder = Callable[[str, dict[str, str]], Awaitable[Any]]


class VerleihFehler(RuntimeError):
    """Die Bridge hat das Gerät nicht verliehen (Token, Netz, Protokoll)."""


class GeraetBesetzt(VerleihFehler):
    """Die Bridge hat mit `besetzt` geantwortet — das Gerät gehört jemand anderem."""

    def __init__(self, grund: str):
        super().__init__(grund)
        self.grund = grund


async def _standard_verbinden(url: str, headers: dict[str, str]) -> Any:
    import websockets

    return await websockets.connect(
        url,
        additional_headers=headers,
        max_size=None,
        open_timeout=VERBINDUNGS_TIMEOUT_S,
    )


class NetzAudioIO:
    """Gerät der Bridge als Audiogerät: PCM16 24 kHz über WebSocket, beide Richtungen.

    Lebenszyklus: `await start_output()` verbindet und leiht das Gerät;
    `start_input()` tut danach nichts mehr (eine Verbindung trägt beides);
    `await stop()` gibt zurück und schließt — idempotent. Verliert die
    Bridge das Gerät oder die Verbindung, ist `verloren` gesetzt und
    `mic_frames()` endet; der CallManager schaltet dann auf den Hörer zurück.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verbinden: Verbinder | None = None,
        verleih_timeout_s: float = VERLEIH_TIMEOUT_S,
        uhr: Callable[[], float] = time.monotonic,
    ):
        self._url = url
        self._token = token
        self._verbinden = verbinden or _standard_verbinden
        self._verleih_timeout_s = verleih_timeout_s
        # Eigene Uhr statt time.monotonic direkt: Tests stellen sie vor, ohne
        # den Event-Loop einzufrieren (der haengt an derselben Funktion).
        self._uhr = uhr
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._leser: asyncio.Task | None = None
        self._sender: asyncio.Task | None = None
        self._ausgabe: asyncio.Queue[Any] = asyncio.Queue()
        self._mic_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=MIKRO_VORRAT)
        self.verloren = asyncio.Event()
        self._geraet_spielt = False
        self._letzter_mic_frame: float | None = None
        self._letzter_signal_frame: float | None = None
        self._letzte_wiedergabe: float | None = None
        self._letztes_stueck: float | None = None
        self.frames_vom_geraet = 0
        self.frames_ans_geraet = 0

    # --- Lebenszyklus -------------------------------------------------------------
    @property
    def verbunden(self) -> bool:
        return self._ws is not None

    async def start_output(self) -> None:
        """Verbinden und das Gerät leihen. Wirft GeraetBesetzt oder VerleihFehler."""
        if self._ws is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._ws = await self._leihen()
        self.verloren.clear()
        self._geraet_spielt = False
        self._leser = self._loop.create_task(self._lesen(self._ws))
        self._sender = self._loop.create_task(self._senden(self._ws))

    def start_input(self) -> None:
        """Kein eigener Schritt: die Verbindung aus start_output() trägt auch das Mikrofon."""
        if self._ws is None:
            raise VerleihFehler("start_input() vor start_output(): nicht verbunden")

    async def _leihen(self) -> Any:
        try:
            ws = await self._verbinden(self._url, {"X-Agent-Token": self._token})
        except Exception as exc:  # noqa: BLE001 — Netz, DNS, Handshake: alles ein Fehler
            raise VerleihFehler(f"keine Verbindung zu {self._url}: {exc}") from exc
        try:
            await ws.send(json.dumps({"type": "verleihen"}))
            antwort = await asyncio.wait_for(ws.recv(), self._verleih_timeout_s)
        except TimeoutError as exc:
            await self._schliessen(ws)
            raise VerleihFehler("Bridge antwortet nicht auf verleihen") from exc
        except Exception as exc:  # noqa: BLE001
            await self._schliessen(ws)
            code = getattr(getattr(exc, "rcvd", None), "code", None)
            if code == 4401:
                raise VerleihFehler("Bridge lehnt das Token ab (4401)") from exc
            raise VerleihFehler(f"Verbindung beim Verleihen verloren: {exc}") from exc
        if isinstance(antwort, bytes):
            await self._schliessen(ws)
            raise VerleihFehler("Bridge schickt Audio statt einer Antwort auf verleihen")
        try:
            nachricht = json.loads(antwort)
        except ValueError as exc:
            await self._schliessen(ws)
            raise VerleihFehler(f"unlesbare Antwort der Bridge: {antwort!r}") from exc
        typ = nachricht.get("type")
        if typ == "verliehen":
            log.info("Gerät verliehen: %s", self._url)
            return ws
        await self._schliessen(ws)
        if typ == "besetzt":
            raise GeraetBesetzt(str(nachricht.get("grund", "")))
        raise VerleihFehler(f"unerwartete Antwort der Bridge: {typ!r}")

    async def stop(self) -> None:
        """Gerät zurückgeben und die Verbindung schließen. Mehrfach aufrufbar."""
        ws = self._ws
        self._ws = None
        await self._tasks_beenden()
        self._puffer_leeren()
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                ws.send(json.dumps({"type": "zurueckgeben"})), ZURUECKGEBEN_TIMEOUT_S
            )
        await self._schliessen(ws)
        self._geraet_spielt = False
        log.info("Gerät zurückgegeben")

    async def neu_verbinden(self) -> None:
        """Verbindung trennen und das Gerät neu leihen (Selbstheilung des Wachhunds).

        Bricht der Neuaufbau ab, ist `verloren` gesetzt, damit der
        CallManager nicht auf einer toten Leitung sitzen bleibt.
        """
        await self.stop()
        try:
            await self.start_output()
        except VerleihFehler:
            self.verloren.set()
            self._mikro_ende()
            raise

    def restart_input(self) -> None:
        """Wachhund-Haken — läuft in `asyncio.to_thread`, deshalb blockierend.

        Vom Event-Loop-Thread aus stattdessen `await neu_verbinden()`.
        """
        loop = self._loop
        if loop is None:
            raise VerleihFehler("restart_input() vor start_output()")
        try:
            im_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            im_loop = False
        if im_loop:
            raise VerleihFehler("restart_input() im Event-Loop — neu_verbinden() awaiten")
        fut = asyncio.run_coroutine_threadsafe(self.neu_verbinden(), loop)
        fut.result(timeout=VERBINDUNGS_TIMEOUT_S + self._verleih_timeout_s + 1.0)

    async def _tasks_beenden(self) -> None:
        for name in ("_leser", "_sender"):
            task: asyncio.Task | None = getattr(self, name)
            setattr(self, name, None)
            if task is None or task is asyncio.current_task():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _puffer_leeren(self) -> None:
        while not self._ausgabe.empty():
            self._ausgabe.get_nowait()

    @staticmethod
    async def _schliessen(ws: Any) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), ZURUECKGEBEN_TIMEOUT_S)

    # --- Client → Bridge ----------------------------------------------------------
    async def play(self, pcm: bytes) -> None:
        """PCM16 ans Gerät — in Stücken von höchstens STUECK_BYTES, der Reihe nach."""
        if self._ws is None:
            return
        for i in range(0, len(pcm), STUECK_BYTES):
            self._ausgabe.put_nowait(bytes(pcm[i : i + STUECK_BYTES]))

    async def clear_playback(self, anlass: str = "unbekannt") -> None:
        """Barge-in: lokale Warteschlange verwerfen und der Bridge `leeren` schicken."""
        verworfen = self._ausgabe.qsize()
        log.info("clear_playback (Netz): Anlass=%s, verworfene Stuecke=%d", anlass, verworfen)
        self._puffer_leeren()
        if self._ws is not None:
            self._ausgabe.put_nowait(_LEEREN)

    async def _senden(self, ws: Any) -> None:
        try:
            while True:
                posten = await self._ausgabe.get()
                if posten is _LEEREN:
                    await ws.send(json.dumps({"type": "leeren"}))
                    continue
                await ws.send(posten)
                self.frames_ans_geraet += 1
                self._letztes_stueck = self._uhr()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — Verbindung weg: der Leser meldet es
            log.debug("Senden ans Gerät beendet: %s", exc)

    @property
    def is_playing(self) -> bool:
        """Das Gerät spielt (laut Bridge) — oder wir haben gerade etwas geschickt.

        Der Nachlauf von SENDE_NACHLAUF_S überbrückt die Zeit, bis die
        Bridge das erste Stück angenommen und `wiedergabe aktiv:true`
        gemeldet hat.
        """
        if self._geraet_spielt or not self._ausgabe.empty():
            return True
        return (
            self._letztes_stueck is not None
            and self._uhr() - self._letztes_stueck < SENDE_NACHLAUF_S
        )

    # --- Bridge → Client ----------------------------------------------------------
    async def _lesen(self, ws: Any) -> None:
        grund = "Bridge hat geschlossen"
        try:
            async for nachricht in ws:
                if isinstance(nachricht, bytes):
                    self._mikro_frame(nachricht)
                    continue
                if self._textnachricht(nachricht):
                    grund = "Gerät weg"
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            grund = f"Verbindung verloren: {exc}"
        if self._ws is ws:
            log.warning("Verleih beendet: %s", grund)
            self.verloren.set()
            self._geraet_spielt = False
            self._mikro_ende()

    def _textnachricht(self, roh: str) -> bool:
        """Eine JSON-Nachricht der Bridge verarbeiten; True = Gerät ist weg."""
        try:
            nachricht = json.loads(roh)
        except ValueError:
            log.warning("unlesbare Nachricht der Bridge: %r", roh[:80])
            return False
        typ = nachricht.get("type")
        if typ == "wiedergabe":
            self._geraet_spielt = bool(nachricht.get("aktiv"))
            self._letzte_wiedergabe = self._uhr()
            return False
        if typ == "geraet_weg":
            return True
        log.debug("Nachricht der Bridge ignoriert: %s", typ)
        return False

    def _mikro_frame(self, frame: bytes) -> None:
        jetzt = self._uhr()
        self._letzter_mic_frame = jetzt
        self.frames_vom_geraet += 1
        # Wie AudioIO._einreihen: int32 vor abs(), sonst kippt -32768.
        pegel = np.abs(np.frombuffer(frame, dtype=np.int16).astype(np.int32))
        if pegel.max(initial=0) > SIGNAL_SCHWELLE:
            self._letzter_signal_frame = jetzt
        if self._mic_queue.full():
            self._mic_queue.get_nowait()  # der Pumper hinkt: das Älteste fällt weg
        self._mic_queue.put_nowait(frame)

    def _mikro_ende(self) -> None:
        if self._mic_queue.full():
            self._mic_queue.get_nowait()
        self._mic_queue.put_nowait(None)

    async def mic_frames(self) -> AsyncIterator[bytes]:
        """Mikrofonframes vom Gerät; endet, wenn das Gerät oder die Verbindung weg ist."""
        while True:
            frame = await self._mic_queue.get()
            if frame is None:
                return
            yield frame

    # --- Wachhund -------------------------------------------------------------------
    def _juengster(self, *zeitpunkte: float | None) -> float | None:
        if self.is_playing:
            return 0.0
        bekannte = [t for t in (*zeitpunkte, self._letzte_wiedergabe) if t is not None]
        if not bekannte:
            return None
        return self._uhr() - max(bekannte)

    def sekunden_seit_mic_frame(self) -> float | None:
        """Alter des letzten Pulses — Mikrofonframe oder Wiedergabe (siehe Modul-Doku)."""
        return self._juengster(self._letzter_mic_frame)

    def sekunden_seit_signal(self) -> float | None:
        """Alter des letzten Inhalts — Frame mit Signal oder Wiedergabe (siehe Modul-Doku)."""
        return self._juengster(self._letzter_signal_frame)
