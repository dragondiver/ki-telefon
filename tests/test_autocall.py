"""Auto-Anruf: die KI klingelt von selbst, beim Abheben startet eine Rolle."""

import asyncio

from test_call_manager import FakeAudio, FakeDialer, FakeSession, SessionMitBegruessung

from kitelefon.call_manager import CallManager, CallState
from kitelefon.hookswitch import HookEvent
from kitelefon.tasks import TaskQueue


class SlowHookSwitch:
    """Liefert Events erst nach einer Verzögerung — löst so den Autocall aus."""

    def __init__(self, events: list[tuple[float, HookEvent]]):
        self._events = events

    async def events(self):
        for delay, ev in self._events:
            await asyncio.sleep(delay)
            yield ev
        await asyncio.sleep(3600)


class FakeBell:
    def __init__(self):
        self.rings: list[int] = []
        self.until_answer_calls = 0

    async def ring(self, rings: int = 1) -> None:
        self.rings.append(rings)
        await asyncio.sleep(0)

    async def ring_until_answer(self, answered: asyncio.Event, max_rings: int = 5) -> bool:
        self.until_answer_calls += 1
        try:
            await asyncio.wait_for(answered.wait(), timeout=0.2)
            return True
        except TimeoutError:
            return False

    def close(self) -> None:
        pass


def _make(cm_kwargs, tmp_path, hook, bell, sessions):
    def open_session(role, on_audio, tool_handler):
        s = FakeSession()
        sessions.append(s)
        return s

    return CallManager(
        hookswitch=hook,
        dialer=FakeDialer(None),
        audio=FakeAudio(),
        open_session=open_session,
        queue=TaskQueue(tmp_path / "tasks.db"),
        bell=bell,
        **cm_kwargs,
    )


async def test_startup_ring_fires_once(tmp_path):
    bell = FakeBell()
    hook = SlowHookSwitch([])
    cm = _make({"startup_ring": True}, tmp_path, hook, bell, [])

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.05)
    task.cancel()

    assert bell.rings == [1], "beim Start soll genau einmal geklingelt werden"


async def test_no_startup_ring_when_disabled(tmp_path):
    bell = FakeBell()
    cm = _make({"startup_ring": False}, tmp_path, SlowHookSwitch([]), bell, [])

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.05)
    task.cancel()

    assert bell.rings == []


async def test_autocall_disabled_by_default(tmp_path):
    """autocall_delay_s = 0 heißt: nie von selbst klingeln."""
    bell = FakeBell()
    cm = _make({"startup_ring": False}, tmp_path, SlowHookSwitch([]), bell, [])

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.1)
    task.cancel()

    assert bell.until_answer_calls == 0


async def test_autocall_rings_after_silence(tmp_path):
    bell = FakeBell()
    cm = _make(
        {"startup_ring": False, "autocall_delay_s": 0.02},
        tmp_path,
        SlowHookSwitch([]),
        bell,
        [],
    )

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.3)
    task.cancel()

    assert bell.until_answer_calls >= 1, "nach Stille soll die KI anrufen"


async def test_autocall_connects_when_lifted(tmp_path):
    """Abheben während des Klingelns startet das Gespräch ohne Wählen."""
    bell = FakeBell()
    sessions: list[FakeSession] = []
    # Hörer wird kurz nach dem Autocall-Timeout abgehoben.
    hook = SlowHookSwitch([(0.05, HookEvent.LIFTED)])
    cm = _make(
        {"startup_ring": False, "autocall_delay_s": 0.02},
        tmp_path,
        hook,
        bell,
        sessions,
    )

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.3)

    assert sessions, "es muss eine Session geöffnet worden sein"
    assert sessions[0].started_mic, "Mikrofon muss nach dem Abheben frei sein"
    assert cm.state is CallState.IN_CALL
    task.cancel()


async def test_autocall_begruesst_beim_eingehenden_anruf(tmp_path):
    """Der eingehende Pfad (autocall_connects_when_lifted) muss _in_call
    genauso begruessen wie der gewaehlte Anruf.

    Finalreview 19.08.: _Lazy in src/kitelefon/__main__.py schluckte
    begruessen() ueber BEIDE Pfade — der getattr-Schutz in
    CallManager._in_call griff still, ohne Logzeile. Siehe
    test_call_manager.py::test_in_call_begruesst_die_sitzung_genau_einmal
    fuer den gewaehlten Anruf."""
    bell = FakeBell()
    sessions: list[SessionMitBegruessung] = []

    def open_session(role, on_audio, tool_handler):
        s = SessionMitBegruessung()
        sessions.append(s)
        return s

    hook = SlowHookSwitch([(0.05, HookEvent.LIFTED)])
    cm = CallManager(
        hookswitch=hook,
        dialer=FakeDialer(None),
        audio=FakeAudio(),
        open_session=open_session,
        queue=TaskQueue(tmp_path / "tasks.db"),
        bell=bell,
        startup_ring=False,
        autocall_delay_s=0.02,
    )

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.3)

    assert sessions, "es muss eine Session geöffnet worden sein"
    assert sessions[0].begruesst == 1
    task.cancel()


async def test_autocall_gives_up_and_closes_session(tmp_path):
    """Niemand hebt ab: gar keine Session, zurück nach IDLE.

    Bis 2026-08-13 wurde die Sitzung schon beim Klingeln vorgewärmt. Das
    kostete nicht nur eine ungenutzte Realtime-Sitzung, wenn niemand ranging
    — das Modell fing auch an zu sprechen, während es noch klingelte.
    """
    bell = FakeBell()
    sessions: list[FakeSession] = []
    # Delay so gewählt, dass wir im ruhigen Fenster nach dem ersten
    # Klingeln messen — sonst läuft schon der nächste Auto-Anruf.
    cm = _make(
        {"startup_ring": False, "autocall_delay_s": 0.15},
        tmp_path,
        SlowHookSwitch([]),
        bell,
        sessions,
    )

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.40)  # 0.15 Ruhe + 0.20 klingeln + Puffer
    assert cm.state is CallState.IDLE
    task.cancel()

    assert sessions == [], "ohne Abheben soll keine Sitzung entstehen"
    assert bell.until_answer_calls == 1


async def test_normal_call_still_works_with_bell_present(tmp_path):
    """Der ausgehende Anruf darf durch die Klingel nicht gestört werden."""
    bell = FakeBell()
    sessions: list[FakeSession] = []
    hook = SlowHookSwitch([(0.01, HookEvent.LIFTED)])

    def open_session(role, on_audio, tool_handler):
        s = FakeSession()
        sessions.append(s)
        return s

    cm = CallManager(
        hookswitch=hook,
        dialer=FakeDialer(3),
        audio=FakeAudio(),
        open_session=open_session,
        queue=TaskQueue(tmp_path / "tasks.db"),
        bell=bell,
        startup_ring=False,
        ringback_tone_s=0.0,
        ringback_pause_s=0.0,
        ringback_repeats=1,
    )

    task = asyncio.create_task(cm.run())
    await asyncio.sleep(0.2)

    assert sessions, "Wählen muss weiterhin eine Session öffnen"
    assert bell.until_answer_calls == 0, "beim Abheben darf nicht geklingelt werden"
    task.cancel()


async def test_autocall_waehlt_nie_die_sprachkursrolle(tmp_path):
    """Camille (Rolle 3) darf nie von selbst anrufen.

    tool_schemas kommt nur ueber get_role() zustande (beim Waehlen per
    Ziffer) — ein Auto-Anruf ginge direkt ueber random.choice(ROLES) und
    haette ohne den Ausschluss die falschen Werkzeuge (ALL_TOOL_SCHEMAS:
    Spotify, Rezepte) in einer Franzoesischstunde.
    """
    bell = FakeBell()
    cm = _make({"startup_ring": False}, tmp_path, SlowHookSwitch([]), bell, [])

    gewaehlte: list[str] = []

    async def fake_eingehender_anruf(hook_q, role):
        gewaehlte.append(role["name"])

    cm._eingehender_anruf = fake_eingehender_anruf
    for _ in range(200):
        await cm._autocall(None)

    assert gewaehlte, "es sollten Rollen gewaehlt worden sein"
    assert all(name != "Camille" for name in gewaehlte)
