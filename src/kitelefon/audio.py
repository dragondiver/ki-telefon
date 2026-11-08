import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import numpy as np
import sounddevice as sd

log = logging.getLogger("kitelefon.audio")

# Ab dieser Spitzenamplitude (von 32768) gilt ein Frame als "hat Signal".
# Bewusst sehr niedrig: Unterschieden werden soll nicht "jemand spricht"
# von "jemand denkt nach", sondern ein lebendes Mikrofon von digitaler
# Stille. Messung am 06.09. auf kitelefon: ein stiller Raum liefert ueber
# den Hoerer Spitzen um 2400, ein totes Geraet exakt 0. Alles darueber
# waere eine Sprecherkennung — und wuerde eine nachdenkende Paula fuer
# defekt erklaeren.
SIGNAL_SCHWELLE = 30

RATE = 24000
CHANNELS = 1
FRAME_DURATION_S = 0.1  # 100 ms => 2400 samples @ 24 kHz
FRAME_SAMPLES = int(RATE * FRAME_DURATION_S)
DIAL_TONE_FREQ_HZ = 425
JITTER_BUFFER_BYTES = 12000  # ~250 ms at 24 kHz * 2 bytes

# Anlaesse fuer clear_playback() — landen im Journal (#113).
ANLASS_BARGEIN = "Barge-in (Mikrofon-VAD)"
ANLASS_WAEHLTON_ENDE = "Waehlton beendet"

# time.monotonic als Modulname: die Callbacks haben einen Parameter "time".
_jetzt = time.monotonic

BYTES_PRO_MS = RATE * 2 / 1000  # PCM16 mono: 48 Bytes je Millisekunde
STATUS_WARN_INTERVALL_S = 1.0  # hoechstens eine Statuswarnung je Sekunde
ENDE_PRUEF_INTERVALL_S = 0.25
ENDE_PRUEF_MAX_S = 120.0


@dataclass
class _AntwortMessung:
    """Zaehler einer Antwort, von "Antwort beginnt" bis "Wiedergabe zu Ende" (#113).

    gespielt/erstes_t/leerlauf werden im PortAudio-Thread unter dem
    Playback-Lock hochgezaehlt — nur Zahlen, kein Logging. Alles andere
    schreibt der Event-Loop-Thread, ebenfalls unter dem Lock.
    """

    nr: int
    t0: float
    puffer_beginn: int
    gesendet: int = 0
    gespielt: int = 0
    verworfen: int = 0
    erstes_t: float | None = None
    leerlauf: int = 0
    modell_fertig: bool = False
    modell_status: str = ""
    puffer_ende: int | None = None
    verworfen_anlaesse: list[str] = field(default_factory=list)
    bargein: bool = False
    status_ab: dict[str, int] = field(default_factory=dict)


def pcm16_dial_tone_frame(num_samples: int, phase: float) -> tuple[bytes, float]:
    """Generate one PCM16 chunk of 425-Hz sine. Returns (bytes, next_phase)."""
    step = 2 * np.pi * DIAL_TONE_FREQ_HZ / RATE
    t = phase + step * np.arange(num_samples, dtype=np.float64)
    samples = (0.1 * np.sin(t) * 32767).astype(np.int16)
    next_phase = (phase + num_samples * step) % (2 * np.pi)
    return samples.tobytes(), next_phase


def pcm16_ringback(tone_s: float, pause_s: float, repeats: int) -> bytes:
    """Build a complete PCM16 ringback signal: (tone + pause) * repeats."""
    n_tone = int(RATE * tone_s)
    n_pause = int(RATE * pause_s)
    t = np.arange(n_tone) / RATE
    tone = (0.1 * np.sin(2 * np.pi * DIAL_TONE_FREQ_HZ * t) * 32767).astype(np.int16)
    pause = np.zeros(n_pause, dtype=np.int16)
    return (np.concatenate([tone, pause] * repeats)).tobytes()


def pcm16_shutdown_tone() -> bytes:
    """Drei absteigende Töne — das Signal fürs Herunterfahren.

    Bewusst anders als Freizeichen (Dauerton) und Rufton (gleiche Tonhöhe im
    Takt): fallende Tonhöhe wird überall als „aus" verstanden, und man erkennt
    sie auch, wenn man den Hörer gerade nicht am Ohr hat.
    """
    teile = []
    for freq in (660, 550, 440):
        n = int(RATE * 0.25)
        t = np.arange(n) / RATE
        ton = (0.1 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
        # Kurzes Ausblenden, sonst knackt der Übergang.
        ton[-240:] = (ton[-240:] * np.linspace(1.0, 0.0, 240)).astype(np.int16)
        teile.extend([ton, np.zeros(int(RATE * 0.05), dtype=np.int16)])
    return np.concatenate(teile).tobytes()


class AudioIO:
    """sounddevice wrapper. Mic frames out, speaker buffer in.

    Lifecycle: start_output() opens the speaker stream. start_input() opens
    mic. stop() closes both. Multiple start/stop cycles per process must work.
    """

    def __init__(self):
        self._out_stream: sd.RawOutputStream | None = None
        self._in_stream: sd.RawInputStream | None = None
        self._playback_buffer = bytearray()
        self._playback_lock = threading.Lock()  # shared with PortAudio thread
        self._playback_started = False
        self._mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self._loop: asyncio.AbstractEventLoop | None = None
        # Status-Dedupe: Callbacks feuern zehnmal pro Sekunde — geloggt
        # wird nur der WECHSEL, sonst flutet ein dauerhafter Overflow das
        # Journal. Sitzungen 4/5 (17.08.) und 9 (19.08.): der Stream starb
        # ohne eine einzige Zeile, weil status nie gelesen wurde.
        self._letzter_in_status = ""
        self._letzter_out_status = ""
        # Monotoner Zeitpunkt des letzten Frames MIT Signal. Ein Geraet
        # kann im Takt weiter Frames liefern, in denen nur Nullen stehen —
        # Sitzung 11 (05.09.) lief genau so: 62 s ohne ein einziges
        # Sprech-Ereignis, waehrend der Wachhund zufrieden war, weil
        # Frames ankamen (#41). Deshalb wird zusaetzlich der Inhalt
        # gestempelt.
        self._letzter_signal_frame: float | None = None
        # Monotoner Zeitpunkt des letzten Mikrofon-Frames — die Grundlage
        # des Watchdogs: ein lebender Input-Stream liefert auch bei Stille
        # kontinuierlich Frames, "nichts seit Sekunden" heisst "Stream tot".
        self._letzter_mic_frame: float | None = None
        # Messung der Wiedergabekette (#113). Nur beobachten, nie steuern.
        self._antwort: _AntwortMessung | None = None
        self._antwort_nr = 0
        # Statusflags des Ausgabe-Callbacks: nur gezaehlt (ein Schreiber, der
        # PortAudio-Thread); geloggt wird im Event-Loop (_status_pruefen).
        self._out_status_zaehler: dict[str, int] = {}
        self._out_status_geloggt: dict[str, int] = {}
        self._letzte_status_warnung = -STATUS_WARN_INTERVALL_S
        self._pruef_handle: asyncio.TimerHandle | None = None

    def start_output(self) -> None:
        if self._out_stream is not None:
            return
        self._letzter_out_status = ""
        self._loop = asyncio.get_running_loop()
        self._out_stream = sd.RawOutputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=self._speaker_callback,
        )
        self._out_stream.start()

    def _oeffne_input_stream(self):
        """Naht zu PortAudio — die einzige Stelle, die einen Stream erzeugt.

        Herausgezogen, damit ``start_input``/``restart_input`` testbar sind,
        ohne dass ein echtes Audiogeraet vorhanden sein muss: Tests ersetzen
        diese Methode und pruefen den Schliessen-und-neu-Oeffnen-Pfad.
        """
        return sd.RawInputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=self._mic_callback,
        )

    def start_input(self) -> None:
        if self._in_stream is not None:
            return
        self._letzter_in_status = ""
        self._letzter_mic_frame = None
        self._letzter_signal_frame = None
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        self._in_stream = self._oeffne_input_stream()
        self._in_stream.start()

    def stop(self) -> None:
        for stream_attr in ("_in_stream", "_out_stream"):
            stream = getattr(self, stream_attr)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                finally:
                    setattr(self, stream_attr, None)
        if self._pruef_handle is not None:
            self._pruef_handle.cancel()
            self._pruef_handle = None
        with self._playback_lock:
            zeile = self._antwort_abschliessen("Stop")
            self._playback_buffer.clear()
            self._playback_started = False
        self._zeile_loggen(zeile)
        while not self._mic_queue.empty():
            try:
                self._mic_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def play(self, pcm: bytes) -> None:
        """Append PCM16 bytes to the playback buffer."""
        with self._playback_lock:
            self._playback_buffer.extend(pcm)
            if self._antwort is not None:
                self._antwort.gesendet += len(pcm)
        if self._out_status_zaehler:
            self._status_pruefen()

    async def clear_playback(self, anlass: str = "unbekannt") -> None:
        with self._playback_lock:
            verworfen = len(self._playback_buffer)
            self._playback_buffer.clear()
            self._playback_started = False
            rec = self._antwort
            zeile = None
            if rec is not None:
                rec.verworfen += verworfen
                rec.verworfen_anlaesse.append(anlass)
                rec.bargein = rec.bargein or anlass == ANLASS_BARGEIN
                zeile = self._antwort_abschliessen(anlass)
        # Geloggt wird erst nach dem Lock: der PortAudio-Thread soll nie auf
        # einen langsamen Log-Handler warten.
        log.info(
            "clear_playback: Anlass=%s, verworfen=%d B (%.0f ms)",
            anlass,
            verworfen,
            verworfen / BYTES_PRO_MS,
        )
        self._zeile_loggen(zeile)
        self._status_pruefen()

    # --- Messung der Wiedergabekette (#113) --------------------------------------
    def antwort_ereignis(self, ereignis: str, **info) -> None:
        """Antwortgrenzen vom Modell: "beginn" und "ende" (Event-Loop-Thread).

        Reines Beobachten — veraendert weder Puffer noch Timing.
        """
        with self._playback_lock:
            zeile = None
            if ereignis == "beginn":
                zeile = self._antwort_abschliessen("von naechster Antwort abgeloest")
                self._antwort_nr += 1
                self._antwort = _AntwortMessung(
                    nr=self._antwort_nr,
                    t0=_jetzt(),
                    puffer_beginn=len(self._playback_buffer),
                    status_ab=dict(self._out_status_zaehler),
                )
                beginn = self._antwort
            elif ereignis == "ende" and self._antwort is not None:
                rec = self._antwort
                rec.modell_fertig = True
                rec.modell_status = str(info.get("status", ""))
                rec.puffer_ende = len(self._playback_buffer)
                beginn = None
            else:
                return
        self._zeile_loggen(zeile)
        if beginn is not None:
            log.info("Antwort %d beginnt, Puffer %d B", beginn.nr, beginn.puffer_beginn)
        else:
            self._pruefe_ende()
        self._status_pruefen()

    def _ende_erreicht(self, rec: _AntwortMessung) -> bool:
        """Modell fertig UND nichts mehr unterwegs (oder Rest, der nie anspielt)."""
        if not rec.modell_fertig:
            return False
        n = len(self._playback_buffer)
        return n == 0 or (not self._playback_started and n < JITTER_BUFFER_BYTES)

    def _pruefe_ende(self) -> None:
        self._pruef_handle = None
        with self._playback_lock:
            rec = self._antwort
            if rec is None:
                return
            zeile = None
            if self._ende_erreicht(rec):
                zeile = self._antwort_abschliessen("Wiedergabe zu Ende")
            elif rec.modell_fertig and _jetzt() - rec.t0 > ENDE_PRUEF_MAX_S:
                zeile = self._antwort_abschliessen("Zeitlimit der Messung")
        if zeile is not None:
            self._zeile_loggen(zeile)
            return
        if rec.modell_fertig and self._loop is not None:
            try:
                self._pruef_handle = self._loop.call_later(
                    ENDE_PRUEF_INTERVALL_S, self._pruefe_ende
                )
            except RuntimeError:
                pass  # Loop schon zu (Herunterfahren)

    @staticmethod
    def _zeile_loggen(zeile: tuple | None) -> None:
        if zeile is not None:
            log.info(*zeile)

    def _antwort_abschliessen(self, ausgang: str) -> tuple | None:
        """Antwort beenden und ihre Zusammenfassung zurueckgeben (Aufrufer haelt den Lock).

        Rueckgabe sind die Log-Argumente; geloggt wird ausserhalb des Locks.
        """
        rec = self._antwort
        if rec is None:
            return None
        self._antwort = None
        rest = len(self._playback_buffer)
        erstes = "nie" if rec.erstes_t is None else f"{(rec.erstes_t - rec.t0) * 1000:.0f} ms"
        flags = {
            k: v - rec.status_ab.get(k, 0)
            for k, v in self._out_status_zaehler.items()
            if v - rec.status_ab.get(k, 0)
        }
        return (
            "Antwort %d beendet (%s%s): Modell %d B (%d Frames, %.0f ms), "
            "an Hardware %d B (%.0f ms), verworfen %d B%s, Rest im Puffer %d B, "
            "Puffer Beginn %d B / bei Modellende %s, erster Frame nach %s, "
            "Puffer-Leerlauf %dx, PortAudio-Flags %s, Modellstatus %s",
            rec.nr,
            ausgang,
            ", Barge-in" if rec.bargein else "",
            rec.gesendet,
            -(-rec.gesendet // (FRAME_SAMPLES * 2)),
            rec.gesendet / BYTES_PRO_MS,
            rec.gespielt,
            rec.gespielt / BYTES_PRO_MS,
            rec.verworfen,
            f" [{', '.join(rec.verworfen_anlaesse)}]" if rec.verworfen_anlaesse else "",
            rest,
            rec.puffer_beginn,
            "?" if rec.puffer_ende is None else f"{rec.puffer_ende} B",
            erstes,
            rec.leerlauf,
            flags or "keine",
            rec.modell_status or "offen",
        )

    def _status_pruefen(self) -> None:
        """Neue PortAudio-Ausgabeflags warnen — hoechstens einmal je Sekunde.

        Laeuft im Event-Loop (nie im PortAudio-Thread). Die Zaehler stehen
        in der Zusammenfassungszeile der Antwort, nichts geht verloren.
        """
        aktuell = dict(self._out_status_zaehler)
        neu = {
            k: v - self._out_status_geloggt.get(k, 0)
            for k, v in aktuell.items()
            if v != self._out_status_geloggt.get(k, 0)
        }
        if not neu:
            return
        jetzt = _jetzt()
        if jetzt - self._letzte_status_warnung < STATUS_WARN_INTERVALL_S:
            return
        self._letzte_status_warnung = jetzt
        self._out_status_geloggt = aktuell
        rec = self._antwort
        log.warning(
            "PortAudio Ausgabe-Status (Antwort %s): %s",
            rec.nr if rec is not None else "-",
            ", ".join(f"{k} x{v}" for k, v in neu.items()),
        )

    @property
    def is_playing(self) -> bool:
        """True while the speaker buffer has audio yet to be consumed."""
        with self._playback_lock:
            return self._playback_started and len(self._playback_buffer) > 0

    async def mic_frames(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._mic_queue.get()
            yield frame

    def sekunden_seit_signal(self) -> float | None:
        """Alter des letzten Frames mit Signal — None, wenn nie eines kam.

        Der Gegenspieler zu ``sekunden_seit_mic_frame``: der eine misst den
        Puls, dieser den Inhalt. Beides zusammen unterscheidet ein totes
        Geraet (kein Puls) von einem tauben (Puls ohne Inhalt, #41).
        """
        if self._letzter_signal_frame is None:
            return None
        return time.monotonic() - self._letzter_signal_frame

    def sekunden_seit_mic_frame(self) -> float | None:
        """Alter des letzten Mikrofon-Frames — None, wenn noch keiner kam."""
        if self._letzter_mic_frame is None:
            return None
        return time.monotonic() - self._letzter_mic_frame

    def restart_input(self) -> None:
        """Den Mikrofon-Stream schliessen und neu oeffnen.

        Selbstheilung fuer den stillen Stream-Tod (Sitzungen 4/5 am 17.08.,
        Sitzung 9 am 19.08.): PortAudio liefert dann keine Frames mehr,
        meldet aber auch nichts. Der Output-Stream bleibt unberuehrt —
        Camille soll weitersprechen koennen, waehrend das Ohr repariert wird.
        """
        stream = self._in_stream
        self._in_stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.warning("alter Mikrofon-Stream liess sich nicht schliessen", exc_info=True)
        self.start_input()

    @staticmethod
    def _status_melden(quelle: str, status, letzter: str) -> str:
        """Statuswechsel eines Streams loggen — dedupliziert, gibt den Merkwert zurueck.

        Callbacks feuern zehnmal pro Sekunde; geloggt wird nur der WECHSEL,
        sonst flutet ein dauerhafter Overflow das Journal. Laeuft im
        PortAudio-Thread: nur Logging, keine asyncio-APIs.
        """
        if not status:
            return letzter
        s = str(status)
        if s != letzter:
            log.warning("PortAudio-Status (%s): %s", quelle, s)
        return s

    def _speaker_callback(self, outdata, frames, time, status):  # noqa: ARG002
        # Runs in PortAudio thread; use threading.Lock, never asyncio APIs.
        self._letzter_out_status = self._status_melden(
            "Lautsprecher", status, self._letzter_out_status
        )
        if status:
            # Nur zaehlen (ein Schreiber); geloggt wird im Event-Loop (#113).
            k = str(status)
            self._out_status_zaehler[k] = self._out_status_zaehler.get(k, 0) + 1
        bytes_needed = frames * 2
        with self._playback_lock:
            if not self._playback_started:
                if len(self._playback_buffer) < JITTER_BUFFER_BYTES:
                    outdata[:] = b"\x00" * bytes_needed
                    return
                self._playback_started = True
            vorhanden = len(self._playback_buffer)
            if vorhanden >= bytes_needed:
                chunk = bytes(self._playback_buffer[:bytes_needed])
                del self._playback_buffer[:bytes_needed]
            else:
                chunk = bytes(self._playback_buffer) + b"\x00" * (bytes_needed - vorhanden)
                self._playback_buffer.clear()
                self._playback_started = False
            rec = self._antwort
            if rec is not None:
                echt = min(vorhanden, bytes_needed)
                rec.gespielt += echt
                if echt and rec.erstes_t is None:
                    rec.erstes_t = _jetzt()
                if echt < bytes_needed:
                    rec.leerlauf += 1
        outdata[:] = chunk

    def _einreihen(self, frame: bytes) -> None:
        """Frame in die Mikrofon-Warteschlange, im Event-Loop ausgefuehrt.

        Der Schutz gegen eine volle Warteschlange muss HIER stehen, nicht um
        call_soon_threadsafe herum: dort wird der Aufruf nur geplant,
        geworfen wird erst beim Ausfuehren. Vorher landete jeder Ueberlauf
        als "Exception in callback" mit vollem Traceback im Protokoll —
        zehnmal pro Sekunde, sobald niemand mehr mitliest, etwa nach dem
        Ende einer Realtime-Sitzung.
        """
        jetzt = time.monotonic()
        self._letzter_mic_frame = jetzt
        # numpy statt Python-Schleife: FRAME_SAMPLES Werte zehnmal pro
        # Sekunde sind in reinem Python spuerbar, als Vektor-Operation
        # nicht messbar.
        #
        # astype(int32) vor abs(): int16 hat fuer -32768 keine positive
        # Entsprechung, np.abs() liefert dort wieder -32768. Ein voll
        # ausgesteuerter Frame galte damit als stumm — ausgerechnet in der
        # Pruefung, die Stille erkennen soll.
        pegel = np.abs(np.frombuffer(frame, dtype=np.int16).astype(np.int32))
        if pegel.max(initial=0) > SIGNAL_SCHWELLE:
            self._letzter_signal_frame = jetzt
        try:
            self._mic_queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass  # drop on overrun; mic is best-effort

    def _mic_callback(self, indata, frames, time, status):  # noqa: ARG002
        self._letzter_in_status = self._status_melden("Mikrofon", status, self._letzter_in_status)
        if self._loop is None:
            return
        frame = bytes(indata)
        # call_soon_threadsafe is the only safe way to enqueue from a non-asyncio thread.
        try:
            self._loop.call_soon_threadsafe(self._einreihen, frame)
        except RuntimeError:
            pass  # loop closed during shutdown
