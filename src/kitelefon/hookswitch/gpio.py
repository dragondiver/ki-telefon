import asyncio
import logging
from collections.abc import AsyncIterator

from gpiozero import Button

from kitelefon.hookswitch import HookEvent

log = logging.getLogger(__name__)


class GpioHookSwitch:
    """Gabelschalter an GPIO 5 (BCM). Pull-Up, Kontakt geschlossen = abgehoben."""

    def __init__(self, pin: int = 5, bounce_time: float = 0.2):
        self._button = Button(pin, pull_up=True, bounce_time=bounce_time)
        self._loop = asyncio.get_event_loop()
        self._queue: asyncio.Queue[HookEvent] = asyncio.Queue()
        self._button.when_pressed = self._on_pressed
        self._button.when_released = self._on_released
        log.info("GpioHookSwitch ready on GPIO %d, initial active=%s", pin, self._button.is_active)

    def _on_pressed(self) -> None:
        log.debug("hookswitch pressed -> LIFTED")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, HookEvent.LIFTED)

    def _on_released(self) -> None:
        log.debug("hookswitch released -> HUNG_UP")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, HookEvent.HUNG_UP)

    async def events(self) -> AsyncIterator[HookEvent]:
        while True:
            yield await self._queue.get()
