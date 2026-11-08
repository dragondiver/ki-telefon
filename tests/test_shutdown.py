"""Ziffer 0 fährt den Pi herunter — Auflegen bricht ab.

Wenn das Telefon im Gehäuse sitzt, gibt es keine Tastatur mehr. Die 0 ist auf
der Wählscheibe der längste Zug, trifft man also kaum versehentlich; die
Gnadenfrist mit Abbruch durch Auflegen ist die zweite Sicherung.
"""

import asyncio

from test_call_manager import FakeAudio, FakeDialer, FakeHookSwitch, FakeSession

from kitelefon.call_manager import CallManager, CallState
from kitelefon.hookswitch import HookEvent
from kitelefon.tasks import TaskQueue


class SpyShutdown:
    def __init__(self):
        self.aufgerufen = 0

    async def __call__(self) -> None:
        self.aufgerufen += 1


def _make(tmp_path, hook, shutdown, **kw):
    return CallManager(
        hookswitch=hook,
        dialer=FakeDialer(digit=0),
        audio=FakeAudio(),
        open_session=lambda *a, **k: FakeSession(),
        queue=TaskQueue(tmp_path / "tasks.db"),
        shutdown=shutdown,
        shutdown_grace_s=0.1,
        **kw,
    )


async def test_null_faehrt_herunter(tmp_path):
    shutdown = SpyShutdown()
    cm = _make(tmp_path, FakeHookSwitch([HookEvent.LIFTED]), shutdown)

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.3)
    task.cancel()

    assert shutdown.aufgerufen == 1


async def test_auflegen_bricht_das_herunterfahren_ab(tmp_path):
    """Die Gnadenfrist ist der Sinn der Sache: auflegen = doch nicht."""
    shutdown = SpyShutdown()
    # Auflegen kommt nach 50 ms, die Gnadenfrist läuft 100 ms.
    hook = FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05)
    cm = _make(tmp_path, hook, shutdown)

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.4)
    task.cancel()

    assert shutdown.aufgerufen == 0, "Auflegen muss das Herunterfahren verhindern"


async def test_null_kuendigt_hoerbar_an(tmp_path):
    """Ohne Ansage wüsste niemand, dass gerade abgebrochen werden kann."""
    shutdown = SpyShutdown()
    cm = _make(tmp_path, FakeHookSwitch([HookEvent.LIFTED]), shutdown)
    audio = cm._audio  # type: ignore[attr-defined]

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.3)
    task.cancel()

    assert audio.played, "vor dem Herunterfahren muss ein Ton kommen"


async def test_ohne_konfiguriertes_shutdown_passiert_nichts(tmp_path):
    """Default ist aus — die 0 darf dann nicht den Anruf sprengen."""
    cm = _make(tmp_path, FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05), None)

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.3)
    task.cancel()

    assert cm.state in (CallState.IDLE, CallState.TEARDOWN)


async def test_normale_ziffer_faehrt_nicht_herunter(tmp_path):
    shutdown = SpyShutdown()
    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=3),
        audio=FakeAudio(),
        open_session=lambda *a, **k: FakeSession(),
        queue=TaskQueue(tmp_path / "tasks.db"),
        shutdown=shutdown,
        shutdown_grace_s=0.1,
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
    )

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.3)
    task.cancel()

    assert shutdown.aufgerufen == 0
