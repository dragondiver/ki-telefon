import asyncio
import logging

from kitelefon.bell import race_ring_against_answer

log = logging.getLogger(__name__)

SLAG_DURATION_S = 1.0
PAUSE_S = 1.5


class NullBell:
    """Klingel-Attrappe für die Mac-Entwicklung.

    Hält das Timing der echten Klingel ein (damit sich Abläufe wie der
    Auto-Anruf realistisch anfühlen), macht aber nur Konsolen-Ausgaben.
    """

    async def ring(self, rings: int = 1) -> None:
        for i in range(rings):
            print(f"\a🔔 Rrring! ({i + 1}/{rings})", flush=True)
            log.debug("NullBell strike %d/%d", i + 1, rings)
            await asyncio.sleep(SLAG_DURATION_S)
            if i < rings - 1:
                await asyncio.sleep(PAUSE_S)

    async def ring_until_answer(self, answered: asyncio.Event, max_rings: int = 5) -> bool:
        return await race_ring_against_answer(lambda: self.ring(max_rings), answered)

    def close(self) -> None:
        log.debug("NullBell closed")
