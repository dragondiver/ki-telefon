from collections.abc import AsyncIterator
from enum import Enum
from typing import Protocol


class HookEvent(str, Enum):
    LIFTED = "lifted"
    HUNG_UP = "hung_up"


class HookSwitch(Protocol):
    def events(self) -> AsyncIterator[HookEvent]: ...


def make_hookswitch(platform: str) -> HookSwitch:
    if platform == "mac":
        from kitelefon.hookswitch.keyboard import KeyboardHookSwitch

        return KeyboardHookSwitch()
    if platform == "pi":
        from kitelefon.hookswitch.gpio import GpioHookSwitch

        return GpioHookSwitch()
    raise ValueError(f"unknown platform: {platform!r}")
