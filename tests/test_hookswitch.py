# tests/test_hookswitch.py
import asyncio
from collections.abc import AsyncIterator

import pytest

from kitelefon.hookswitch import HookEvent
from kitelefon.hookswitch.keyboard import KeyboardHookSwitch


async def _feed(keys: list[str]) -> AsyncIterator[str]:
    for k in keys:
        yield k
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_space_toggles_lifted_then_hung_up():
    hook = KeyboardHookSwitch(input_source=_feed([" ", " "]))
    events: list[HookEvent] = []
    async for ev in hook.events():
        events.append(ev)
    assert events == [HookEvent.LIFTED, HookEvent.HUNG_UP]


@pytest.mark.asyncio
async def test_non_space_keys_ignored():
    hook = KeyboardHookSwitch(input_source=_feed(["x", "1", " ", "y", " "]))
    events: list[HookEvent] = []
    async for ev in hook.events():
        events.append(ev)
    assert events == [HookEvent.LIFTED, HookEvent.HUNG_UP]
