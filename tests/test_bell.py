import asyncio

import pytest

from kitelefon.bell import make_bell, race_ring_against_answer
from kitelefon.bell.null import NullBell


def test_make_bell_mac_returns_null_bell():
    assert isinstance(make_bell("mac"), NullBell)


def test_make_bell_rejects_unknown_platform():
    with pytest.raises(ValueError, match="unknown platform"):
        make_bell("commodore")


async def test_ring_until_answer_returns_false_when_nobody_picks_up():
    bell = NullBell()
    answered = asyncio.Event()
    # NullBell hält echtes Timing ein, deshalb Schläge kurz halten.
    bell.ring = _fast_ring()

    assert await bell.ring_until_answer(answered, max_rings=2) is False


async def test_ring_until_answer_returns_true_when_lifted_mid_ring():
    bell = NullBell()
    answered = asyncio.Event()
    bell.ring = _fast_ring()

    async def lift_soon():
        await asyncio.sleep(0.02)
        answered.set()

    asyncio.create_task(lift_soon())
    assert await bell.ring_until_answer(answered, max_rings=50) is True


async def test_ring_until_answer_short_circuits_if_already_lifted():
    """Hörer schon abgehoben: gar nicht erst klingeln."""
    rang = False

    async def ring() -> None:
        nonlocal rang
        rang = True

    answered = asyncio.Event()
    answered.set()

    assert await race_ring_against_answer(ring, answered) is True
    assert rang is False


async def test_ring_is_cancelled_and_cleaned_up_on_answer():
    """Das finally der Klingel-Routine muss laufen, bevor wir zurückkehren."""
    cleaned = False

    async def ring() -> None:
        nonlocal cleaned
        try:
            await asyncio.sleep(10)
        finally:
            cleaned = True

    answered = asyncio.Event()

    async def lift_soon():
        await asyncio.sleep(0.02)
        answered.set()

    asyncio.create_task(lift_soon())
    assert await race_ring_against_answer(ring, answered) is True
    assert cleaned is True, "Spule wäre bestromt geblieben"


def _fast_ring():
    """Ersetzt die 1-s-Schläge durch 10-ms-Schläge, damit Tests flott sind."""

    async def ring(rings: int = 1) -> None:
        for _ in range(rings):
            await asyncio.sleep(0.01)

    return ring
