import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Protocol

log = logging.getLogger(__name__)


async def race_ring_against_answer(
    ring: Callable[[], Coroutine[object, object, None]], answered: asyncio.Event
) -> bool:
    """Lässt `ring()` laufen, bricht sofort ab sobald `answered` gesetzt ist.

    Gemeinsam für alle Bell-Implementierungen. Gibt True zurück, wenn
    abgehoben wurde, False wenn durchgeklingelt wurde.
    """
    if answered.is_set():
        return True

    ring_task = asyncio.create_task(ring())
    answer_task = asyncio.create_task(answered.wait())
    try:
        await asyncio.wait({ring_task, answer_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (ring_task, answer_task):
            task.cancel()
        # Auf die Cancellation warten, damit das finally in der Klingel-
        # Routine sicher durchläuft (Spule stromlos), bevor wir zurückkehren.
        await asyncio.gather(ring_task, answer_task, return_exceptions=True)

    picked_up = answered.is_set()
    log.info("ring_until_answer: %s", "abgehoben" if picked_up else "niemand da")
    return picked_up


class Bell(Protocol):
    async def ring(self, rings: int = 1) -> None:
        """Schlägt `rings` Mal an. Kehrt zurück wenn fertig."""
        ...

    async def ring_until_answer(self, answered: asyncio.Event, max_rings: int = 5) -> bool:
        """Klingelt bis abgehoben wird oder `max_rings` erreicht sind.

        Gibt True zurück, wenn abgehoben wurde.
        """
        ...

    def close(self) -> None:
        """Gibt die Hardware frei. Muss idempotent sein."""
        ...


def make_bell(platform: str) -> Bell:
    if platform == "mac":
        from kitelefon.bell.null import NullBell

        return NullBell()
    if platform == "pi":
        from kitelefon.bell.gpio import GpioBell

        return GpioBell()
    raise ValueError(f"unknown platform: {platform!r}")
