import asyncio
from typing import Any

import pytest

from kitelefon.call_manager import CallManager, CallState
from kitelefon.hookswitch import HookEvent
from kitelefon.tasks import TaskQueue


class FakeHookSwitch:
    """Hook-Events der Reihe nach.

    `delay` ist die Pause **vor** jedem Event ab dem zweiten. Ohne sie folgt
    das Auflegen dem Abheben in derselben Event-Loop-Runde — am echten
    Telefon unmöglich, und seit das Auflegen gegen die Wählscheibe rennt
    entscheidet die Reihenfolge über den Ausgang.
    """

    def __init__(self, events: list[HookEvent], delay: float = 0.0):
        self._events = events
        self._delay = delay

    async def events(self):
        for i, e in enumerate(self._events):
            if i and self._delay:
                await asyncio.sleep(self._delay)
            yield e
            await asyncio.sleep(0)


class FakeDialer:
    def __init__(self, digit: int | None):
        self._digit = digit

    async def read_digit(self, timeout: float) -> int | None:
        await asyncio.sleep(0)
        return self._digit


class FakeAudio:
    def __init__(self):
        self.output_started = False
        self.input_started = False
        self.played: list[bytes] = []
        self.stopped = False
        self.cleared = False

    def start_output(self):
        self.output_started = True

    def start_input(self):
        self.input_started = True

    def stop(self):
        self.stopped = True

    async def play(self, pcm: bytes):
        self.played.append(pcm)

    async def clear_playback(self, anlass: str = ""):
        self.cleared = True

    async def mic_frames(self):
        # never yields; manager will be cancelled when hook goes down
        await asyncio.sleep(3600)
        if False:
            yield b""


class FakeSession:
    def __init__(self):
        self.started_mic = False
        self.closed = False
        self.tool_calls: list[tuple[str, dict]] = []

    @property
    def state(self):
        from kitelefon.realtime import SessionState

        return SessionState.LISTENING

    def enable_mic(self):
        self.started_mic = True

    async def run(self):
        await asyncio.sleep(3600)

    async def close(self):
        self.closed = True

    async def send_mic_frame(self, pcm: bytes):
        pass


class SessionMitBegruessung(FakeSession):
    """FakeSession mit begruessen()-Zaehler.

    Finalreview 19.08.: _Lazy (src/kitelefon/__main__.py) hatte kein
    begruessen() und keine __getattr__-Weiterleitung — der getattr-Schutz
    in CallManager._in_call griff bei jedem produktiven Anruf und schluckte
    die Begruessung still. tests/test_begruessung.py war trotzdem gruen,
    weil dort nur RealtimeSession.begruessen() geprueft wird, nie die
    Verdrahtung ueber _in_call. Diese Klasse deckt genau das ab.
    """

    def __init__(self):
        super().__init__()
        self.begruesst = 0

    async def begruessen(self):
        self.begruesst += 1


@pytest.mark.asyncio
async def test_full_call_flow_writes_no_task_without_tool_call(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    audio = FakeAudio()
    sessions: list[FakeSession] = []

    def open_session(role, on_audio, tool_handler):
        s = FakeSession()
        sessions.append(s)
        return s

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=1),
        audio=audio,
        open_session=open_session,
        queue=queue,
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
    )
    await cm.run()
    assert audio.output_started
    assert audio.stopped
    assert len(sessions) == 1
    assert sessions[0].closed
    assert queue.list_pending() == []
    assert cm.state == CallState.IDLE


@pytest.mark.asyncio
async def test_in_call_begruesst_die_sitzung_genau_einmal(tmp_path):
    """Finalreview 19.08.: _Lazy schluckte die Begruessung — produktiv ging
    beim gewaehlten wie beim eingehenden Anruf kein Gruss mehr raus, obwohl
    tests/test_begruessung.py komplett gruen war. Dieser Test deckt die
    Verdrahtung CallManager._in_call -> session.begruessen() fuer den
    gewaehlten Anruf ab (siehe test_autocall.py fuer den eingehenden)."""
    queue = TaskQueue(tmp_path / "tasks.db")
    audio = FakeAudio()
    sessions: list[SessionMitBegruessung] = []

    def open_session(role, on_audio, tool_handler):
        s = SessionMitBegruessung()
        sessions.append(s)
        return s

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=1),
        audio=audio,
        open_session=open_session,
        queue=queue,
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
    )
    await cm.run()
    assert len(sessions) == 1
    assert sessions[0].begruesst == 1


@pytest.mark.asyncio
async def test_tool_handler_writes_to_queue(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    audio = FakeAudio()
    captured_handlers: list[Any] = []

    def open_session(role, on_audio, tool_handler):
        captured_handlers.append(tool_handler)
        return FakeSession()

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=2),
        audio=audio,
        open_session=open_session,
        queue=queue,
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
    )
    task = asyncio.create_task(cm.run())
    # wait for handler to be installed
    for _ in range(50):
        if captured_handlers:
            break
        await asyncio.sleep(0.01)
    handler = captured_handlers[0]
    result = await handler(
        "delegate_task",
        {
            "agent_type": "reminder",
            "description": "Buy bread",
            "deliver_via": "callback",
        },
    )
    assert result["ok"] is True
    await task
    rows = queue.list_pending()
    assert len(rows) == 1
    assert rows[0]["agent_type"] == "reminder"


@pytest.mark.asyncio
async def test_sprachkurs_rolle_finalisiert_auch_bei_leerem_transkript(tmp_path):
    """Rolle 3: ein sofortiger Abbruch darf die Sitzung nicht offen lassen.

    Bei den Plauderrollen ist ein leeres Transkript nichts zu destillieren
    — der fruehe Ausstieg in _nachbereiten() ist da richtig. Fuer Rolle 3
    waere er falsch: ein Transport-Fehler direkt nach dem Aufbau wuerde
    sonst die DB-Sitzung fuer immer offen lassen.
    """
    queue = TaskQueue(tmp_path / "tasks.db")
    audio = FakeAudio()
    calls: list[tuple[Any, list]] = []

    async def on_call_ended(role, verlauf):
        calls.append((role, verlauf))

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=3),
        audio=audio,
        open_session=lambda role, on_audio, th: FakeSession(),
        queue=queue,
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
        on_call_ended=on_call_ended,
    )
    await cm.run()

    assert len(calls) == 1
    role, verlauf = calls[0]
    assert role.get("sprachkurs") is True
    assert verlauf == []


@pytest.mark.asyncio
async def test_plauderrolle_ohne_transkript_ruft_on_call_ended_nicht(tmp_path):
    """Gegenprobe: fuer die Plauderrollen bleibt der fruehe Ausstieg."""
    queue = TaskQueue(tmp_path / "tasks.db")
    audio = FakeAudio()
    calls: list[tuple[Any, list]] = []

    async def on_call_ended(role, verlauf):
        calls.append((role, verlauf))

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=1),
        audio=audio,
        open_session=lambda role, on_audio, th: FakeSession(),
        queue=queue,
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
        on_call_ended=on_call_ended,
    )
    await cm.run()

    assert calls == []


@pytest.mark.asyncio
async def test_sprachkurs_nicht_verfuegbar_spielt_fehlerton_statt_stille(tmp_path):
    """B2 (Abschluss-Review): scheitert der Sprachkurs-Aufbau komplett (kein
    Netz, kein Schnappschuss), soll die Leitung nicht einfach stumm bleiben
    — es muss ein Ton gespielt werden, bevor aufgeraeumt wird."""
    from kitelefon.sprachkurs_call import SprachkursNichtVerfuegbar

    queue = TaskQueue(tmp_path / "tasks.db")
    audio = FakeAudio()

    class KaputteSession(FakeSession):
        async def run(self):
            raise SprachkursNichtVerfuegbar("weder Datenbank noch Schnappschuss")

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=3),
        audio=audio,
        open_session=lambda role, on_audio, th: KaputteSession(),
        queue=queue,
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
    )
    await cm.run()

    assert len(audio.played) >= 1, "es haette ein Fehlerton gespielt werden muessen"


@pytest.mark.asyncio
async def test_hangup_during_dialing_returns_to_idle(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    audio = FakeAudio()

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP]),
        dialer=FakeDialer(digit=None),  # times out
        audio=audio,
        open_session=lambda *a, **kw: FakeSession(),
        queue=queue,
        dial_timeout_s=0.05,
    )
    await cm.run()
    assert cm.state == CallState.IDLE
    assert audio.stopped


class BlockingDialer:
    """Wählscheibe, an der niemand dreht — blockiert bis zum Timeout.

    Der echte GPIO-Dialer verhält sich so; `FakeDialer` kehrt dagegen sofort
    zurück und lässt den Auflegen-Fall in der Wählphase ungetestet.
    """

    def __init__(self):
        self.cancelled = False

    async def read_digit(self, timeout: float) -> int | None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return None


@pytest.mark.asyncio
async def test_hangup_stops_dial_tone_immediately(tmp_path):
    """Auflegen im Freizeichen muss sofort abbrechen, nicht erst nach dem
    Wähl-Timeout. Vorher lief das Freizeichen bis zu 30 s weiter."""
    queue = TaskQueue(tmp_path / "tasks.db")
    audio = FakeAudio()
    dialer = BlockingDialer()

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP]),
        dialer=dialer,
        audio=audio,
        open_session=lambda *a, **kw: FakeSession(),
        queue=queue,
        dial_timeout_s=30.0,  # realistischer Wert
    )

    await asyncio.wait_for(cm.run(), timeout=2.0)

    assert cm.state == CallState.IDLE
    assert audio.stopped, "Audio muss beim Auflegen gestoppt werden"
    assert dialer.cancelled, "das Warten auf die Ziffer muss abgebrochen werden"
