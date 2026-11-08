"""Der Verlauf muss nach dem Auflegen bei der Nachbereitung ankommen.

Die Falle dabei: das Transkript muss geholt werden, SOLANGE die Session
noch lebt. Wer erst schliesst und dann fragt, bekommt nichts.
"""

import asyncio

import pytest
from test_call_manager import FakeAudio, FakeDialer, FakeHookSwitch

from kitelefon.call_manager import CallManager, CallState
from kitelefon.hookswitch import HookEvent
from kitelefon.tasks import TaskQueue


class SessionMitVerlauf:
    def __init__(self, verlauf):
        self._verlauf = verlauf
        self.closed = False

    @property
    def state(self):
        from kitelefon.realtime import SessionState

        return SessionState.LISTENING

    @property
    def verlauf(self):
        # Nach dem Schliessen ist nichts mehr zu holen — genau wie in echt.
        return [] if self.closed else self._verlauf

    def enable_mic(self):
        pass

    async def run(self):
        await asyncio.sleep(3600)

    async def close(self):
        self.closed = True

    async def send_mic_frame(self, pcm: bytes):
        pass


def _cm(tmp_path, on_call_ended, verlauf):
    return CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=1),
        audio=FakeAudio(),
        open_session=lambda role, on_audio, th: SessionMitVerlauf(verlauf),
        queue=TaskQueue(tmp_path / "tasks.db"),
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
        on_call_ended=on_call_ended,
    )


@pytest.mark.asyncio
async def test_verlauf_und_rolle_kommen_bei_der_nachbereitung_an(tmp_path):
    gesehen = []

    async def on_call_ended(role, verlauf):
        gesehen.append((role["name"], verlauf))

    verlauf = [("Anrufer", "Moin, hier Otto."), ("Hamburger", "Moin.")]
    await _cm(tmp_path, on_call_ended, verlauf).run()

    assert len(gesehen) == 1
    name, mitgeschrieben = gesehen[0]
    assert name == "Hamburger"  # Ziffer 1
    assert mitgeschrieben == verlauf


@pytest.mark.asyncio
async def test_ohne_verlauf_wird_nicht_nachbereitet(tmp_path):
    """Ein Abheben und sofortiges Auflegen ergibt keine Erinnerung — und
    soll keinen Claude-Aufruf kosten."""
    gerufen = []

    async def on_call_ended(role, verlauf):
        gerufen.append(role)

    await _cm(tmp_path, on_call_ended, []).run()

    assert gerufen == []


@pytest.mark.asyncio
async def test_fehler_in_der_nachbereitung_legt_das_telefon_nicht_lahm(tmp_path):
    """Das Gespraech ist vorbei — ein Fehler beim Aufraeumen darf das
    Telefon nicht in einem kaputten Zustand hinterlassen."""

    async def kaputt(role, verlauf):
        raise RuntimeError("Claude antwortet nicht")

    cm = _cm(tmp_path, kaputt, [("Anrufer", "Hallo.")])
    await cm.run()

    assert cm.state == CallState.IDLE
