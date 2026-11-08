import asyncio
import logging
import time

from gpiozero import Button

MIN_PULSE_SEPARATION = 0.08
INTER_DIGIT_TIMEOUT = 0.6

log = logging.getLogger(__name__)


class GpioDialer:
    """Wählscheibe an GPIO 26 (BCM). 10 Impulse = 0."""

    im_gespraech_lesbar = True

    def __init__(self, pin: int = 26):
        self._button = Button(pin, pull_up=True)
        self._loop = asyncio.get_event_loop()
        self._digit_queue: asyncio.Queue[int] = asyncio.Queue()
        self._pulses = 0
        self._last_pulse = 0.0
        self._flush_handle: asyncio.TimerHandle | None = None
        self._button.when_pressed = self._on_pulse
        log.info("GpioDialer ready on GPIO %d", pin)

    def _on_pulse(self) -> None:
        now = time.monotonic()
        if now - self._last_pulse < MIN_PULSE_SEPARATION:
            return
        self._last_pulse = now
        self._pulses += 1
        self._loop.call_soon_threadsafe(self._reschedule_flush)

    def _reschedule_flush(self) -> None:
        if self._flush_handle:
            self._flush_handle.cancel()
        self._flush_handle = self._loop.call_later(INTER_DIGIT_TIMEOUT, self._flush)

    def _flush(self) -> None:
        if self._pulses == 0:
            return
        digit = self._pulses if self._pulses < 10 else 0
        self._pulses = 0
        self._digit_queue.put_nowait(digit)

    async def read_digit(self, timeout: float) -> int | None:
        try:
            return await asyncio.wait_for(self._digit_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    def leeren(self) -> None:
        """Ziffern verwerfen, die vor dem Gespraech noch in der Queue liegen.

        Die Impulse kommen auch waehrend des Ruftons an — wer nach der
        Rollenwahl noch an der Scheibe dreht, soll damit nicht gleich das
        Freisprechen einschalten (#38). Ein halb gezaehlter Ziffer-Impulszug
        (self._pulses) bleibt unberuehrt: er gehoert schon zum Gespraech.
        """
        verworfen = 0
        while not self._digit_queue.empty():
            self._digit_queue.get_nowait()
            verworfen += 1
        if verworfen:
            log.debug("Waehlscheibe: %d liegengebliebene Ziffer(n) verworfen", verworfen)
