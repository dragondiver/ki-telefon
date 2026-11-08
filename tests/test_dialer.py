# tests/test_dialer.py
import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from kitelefon.dialer.keyboard import KeyboardDialer


async def _feed(chars: list[str]) -> AsyncIterator[str]:
    for c in chars:
        yield c
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_first_digit_returned():
    d = KeyboardDialer(input_source=_feed(["x", "y", "3", "4"]))
    digit = await d.read_digit(timeout=1.0)
    assert digit == 3


@pytest.mark.asyncio
async def test_timeout_returns_none():
    async def empty() -> AsyncIterator[str]:
        await asyncio.sleep(0.05)
        if False:
            yield ""

    d = KeyboardDialer(input_source=empty())
    digit = await d.read_digit(timeout=0.1)
    assert digit is None


@pytest.mark.asyncio
async def test_non_digit_ignored():
    d = KeyboardDialer(input_source=_feed(["a", "!", "0"]))
    digit = await d.read_digit(timeout=1.0)
    assert digit == 0


async def _queue_source(q: asyncio.Queue[str]) -> AsyncIterator[str]:
    """Wie __main__._queue_iter: endloser Strom aus einer Queue."""
    while True:
        yield await q.get()


@pytest.mark.asyncio
async def test_nach_timeout_kommt_die_naechste_ziffer_an():
    """#74: ein Timeout darf den Eingabestrom nicht beenden."""
    q: asyncio.Queue[str] = asyncio.Queue()
    d = KeyboardDialer(input_source=_queue_source(q))
    assert await d.read_digit(timeout=0.01) is None

    q.put_nowait("7")
    assert await d.read_digit(timeout=1.0) == 7


@pytest.mark.asyncio
async def test_nach_abbruch_kommt_die_naechste_ziffer_an():
    """#74: ein abgebrochener read_digit-Task darf den Strom nicht beenden."""
    q: asyncio.Queue[str] = asyncio.Queue()
    d = KeyboardDialer(input_source=_queue_source(q))
    task = asyncio.create_task(d.read_digit(timeout=10.0))
    await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    q.put_nowait("5")
    assert await d.read_digit(timeout=1.0) == 5


@pytest.mark.asyncio
async def test_abbruch_wird_durchgereicht():
    """Der Begleit-Task im Gespraech muss sich beim Auflegen beenden lassen:
    read_digit darf ein Cancel nicht als None verschlucken, sonst liefe die
    Leseschleife im CallManager einfach weiter."""
    q: asyncio.Queue[str] = asyncio.Queue()
    d = KeyboardDialer(input_source=_queue_source(q))
    task = asyncio.create_task(d.read_digit(timeout=10.0))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_leeren_verwirft_liegengebliebene_ziffern():
    q: asyncio.Queue[str] = asyncio.Queue()
    d = KeyboardDialer(input_source=_queue_source(q))
    assert await d.read_digit(timeout=0.01) is None  # Pumpe laeuft
    for ch in "12x3":
        q.put_nowait(ch)
    await asyncio.sleep(0.01)  # Pumpe holt die Zeichen ab

    d.leeren()
    assert await d.read_digit(timeout=0.01) is None

    q.put_nowait("4")
    assert await d.read_digit(timeout=1.0) == 4


@pytest.mark.asyncio
async def test_ziffern_vor_dem_stromende_kommen_noch_an():
    d = KeyboardDialer(input_source=_feed(["1", "2"]))
    assert await d.read_digit(timeout=1.0) == 1
    assert await d.read_digit(timeout=1.0) == 2
    # Danach ist der Strom zu Ende: sofort None, wie bisher.
    assert await d.read_digit(timeout=5.0) is None
    assert await d.read_digit(timeout=5.0) is None
