"""Hoerer-Wachhund (#86): in_call setzt sich zurueck, wenn der Hoerer aufliegt.

Am 13.08.2026 hing ein Rueckruf sieben Minuten in in_call, weil das Auflegen
die Hauptschleife nicht erreichte. Eine Obergrenze fuer die Gespraechsdauer
gibt es bewusst nicht (Paulas Sprachkurs darf lang werden) — nur der Hoerer
entscheidet.
"""

import asyncio

import pytest

from kitelefon.call_manager import CallManager, CallState
from kitelefon.hookswitch import HookEvent
from kitelefon.tasks import TaskQueue
from tests.test_call_manager import FakeAudio, FakeDialer, FakeSession


class SteuerbarerHoerer:
    """Hook-Events auf Zuruf des Tests."""

    def __init__(self):
        self._q: asyncio.Queue[HookEvent] = asyncio.Queue()

    def sende(self, ev: HookEvent) -> None:
        self._q.put_nowait(ev)

    async def events(self):
        while True:
            yield await self._q.get()


def _bauen(tmp_path, hoerer, **kw):
    audio = FakeAudio()
    sessions: list[FakeSession] = []

    def open_session(role, on_audio, tool_handler):
        s = FakeSession()
        sessions.append(s)
        return s

    cm = CallManager(
        hookswitch=hoerer,
        dialer=FakeDialer(digit=1),
        audio=audio,
        open_session=open_session,
        queue=TaskQueue(tmp_path / "tasks.db"),
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
        hoerer_frist_s=0.1,
        hoerer_pruef_s=0.01,
        **kw,
    )
    teardowns = []
    echt = cm._teardown

    async def gezaehlt():
        teardowns.append(1)
        await echt()

    cm._teardown = gezaehlt  # type: ignore[method-assign]
    return cm, audio, sessions, teardowns


async def _warte_auf(bedingung, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not bedingung():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_aufgelegt_aber_nicht_verarbeitet_beendet_gespraech(tmp_path):
    hoerer = SteuerbarerHoerer()
    cm, audio, sessions, teardowns = _bauen(tmp_path, hoerer)

    # Die Hauptschleife liest nach dem Abheben nichts mehr: das Auflegen
    # erreicht sie nie (Muster vom 13.08.).
    echt_next = cm._next_hook_event
    erstes = True

    async def haengt(hook_q, timeout):
        nonlocal erstes
        if erstes:
            erstes = False
            return await echt_next(hook_q, timeout)
        await asyncio.Event().wait()

    cm._next_hook_event = haengt  # type: ignore[method-assign]

    run = asyncio.create_task(cm.run())
    hoerer.sende(HookEvent.LIFTED)
    await _warte_auf(lambda: cm.state is CallState.IN_CALL)

    hoerer.sende(HookEvent.HUNG_UP)
    await _warte_auf(lambda: cm.state is CallState.IDLE)

    assert len(teardowns) == 1
    assert sessions[0].closed
    assert audio.stopped
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert len(teardowns) == 1


@pytest.mark.asyncio
async def test_aktives_gespraech_mit_abgehobenem_hoerer_wird_nie_beendet(tmp_path):
    hoerer = SteuerbarerHoerer()
    cm, _audio, sessions, teardowns = _bauen(tmp_path, hoerer)

    run = asyncio.create_task(cm.run())
    hoerer.sende(HookEvent.LIFTED)
    await _warte_auf(lambda: cm.state is CallState.IN_CALL)

    # Das Zehnfache der Frist, Hoerer bleibt abgehoben (auch mit Prellen).
    await asyncio.sleep(1.0)
    hoerer.sende(HookEvent.LIFTED)
    await asyncio.sleep(0.3)
    assert cm.state is CallState.IN_CALL
    assert not sessions[0].closed
    assert teardowns == []

    hoerer.sende(HookEvent.HUNG_UP)
    await _warte_auf(lambda: cm.state is CallState.IDLE)
    assert len(teardowns) == 1
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_normales_auflegen_bleibt_unveraendert(tmp_path):
    hoerer = SteuerbarerHoerer()
    # Frist sehr lang: nur der normale Weg kann das Gespraech beenden.
    cm, audio, sessions, teardowns = _bauen(tmp_path, hoerer)
    cm._hoerer_frist_s = 3600.0

    run = asyncio.create_task(cm.run())
    hoerer.sende(HookEvent.LIFTED)
    await _warte_auf(lambda: cm.state is CallState.IN_CALL)
    hoerer.sende(HookEvent.HUNG_UP)
    await _warte_auf(lambda: cm.state is CallState.IDLE, timeout=1.0)

    assert len(teardowns) == 1
    assert sessions[0].closed
    assert audio.stopped
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
