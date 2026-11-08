import asyncio
import sys
from collections.abc import AsyncIterator

from kitelefon.hookswitch import HookEvent


async def _stdin_chars() -> AsyncIterator[str]:
    """Read single characters from stdin without blocking the event loop."""
    loop = asyncio.get_running_loop()
    while True:
        ch = await loop.run_in_executor(None, sys.stdin.read, 1)
        if ch == "":
            return
        yield ch


class KeyboardHookSwitch:
    """Space toggles lifted/hung_up. Other keys are ignored."""

    def __init__(self, input_source: AsyncIterator[str] | None = None):
        self._input = input_source if input_source is not None else _stdin_chars()
        self._lifted = False

    async def events(self) -> AsyncIterator[HookEvent]:
        async for ch in self._input:
            if ch != " ":
                continue
            self._lifted = not self._lifted
            yield HookEvent.LIFTED if self._lifted else HookEvent.HUNG_UP
