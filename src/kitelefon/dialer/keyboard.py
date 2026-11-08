import asyncio
import logging
from collections.abc import AsyncIterator

from kitelefon.hookswitch.keyboard import _stdin_chars

log = logging.getLogger(__name__)

# Markiert in der Queue das Ende des Eingabestroms.
_STROM_ENDE = None


class KeyboardDialer:
    # Ein Hintergrund-Task pumpt den Eingabestrom in eine Queue, read_digit()
    # liest nur aus der Queue. Timeout und Abbruch von read_digit() lassen
    # den Strom darum unberuehrt (#74) — die Tastatur darf wie die GPIO-
    # Waehlscheibe auch im Gespraech gelesen werden (#38).
    im_gespraech_lesbar = True

    def __init__(self, input_source: AsyncIterator[str] | None = None):
        self._input = input_source if input_source is not None else _stdin_chars()
        self._queue: asyncio.Queue[int | None] = asyncio.Queue()
        # Erst beim ersten read_digit() gestartet: der Konstruktor kann
        # ausserhalb eines laufenden Event-Loops aufgerufen werden
        # (make_dialer).
        self._pumpe: asyncio.Task[None] | None = None
        self._strom_zu_ende = False

    async def _pumpen(self) -> None:
        """Eingabestrom einmal durchlesen, Ziffern in die Queue legen."""
        try:
            async for ch in self._input:
                if ch.isdigit():
                    self._queue.put_nowait(int(ch))
        finally:
            self._queue.put_nowait(_STROM_ENDE)

    async def read_digit(self, timeout: float) -> int | None:
        if self._pumpe is None:
            self._pumpe = asyncio.create_task(self._pumpen())
        if self._strom_zu_ende and self._queue.empty():
            # Wie bisher: nach Stromende sofort None (einmal an den Loop
            # abgeben, damit ein Aufrufer in einer Schleife nicht blockiert).
            await asyncio.sleep(0)
            return self._nach_stromende()

        try:
            digit = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            log.info("Waehlscheibe: keine Ziffer innerhalb %.0fs", timeout)
            return None
        if digit is _STROM_ENDE:
            self._strom_zu_ende = True
            return self._nach_stromende()
        log.info("Waehlscheibe: Ziffer %s erkannt", digit)
        return digit

    def _nach_stromende(self) -> None:
        # Eingabestrom zu Ende — im Betrieb ein Fehler, kein Timeout.
        log.warning("Waehlscheibe: Eingabestrom beendet")
        assert self._pumpe is not None
        if self._pumpe.done() and not self._pumpe.cancelled():
            exc = self._pumpe.exception()
            if exc is not None:
                raise exc
        return None

    def leeren(self) -> None:
        """Ziffern verwerfen, die vor dem Gespraech noch in der Queue liegen.

        Wie beim GpioDialer: wer nach der Rollenwahl noch Tasten drueckt,
        soll damit nicht gleich das Freisprechen umschalten (#38).
        """
        verworfen = 0
        while not self._queue.empty():
            if self._queue.get_nowait() is _STROM_ENDE:
                self._strom_zu_ende = True
            else:
                verworfen += 1
        if verworfen:
            log.debug("Waehlscheibe: %d liegengebliebene Ziffer(n) verworfen", verworfen)
