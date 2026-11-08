"""note_error notiert still — es darf keine Antwort ausloesen.

Sitzung 8 am 18.08., 20:51:04: Camilles Antwort endet, 0,3 s spaeter
startet ohne jedes VAD-Event eine zweite ("Très bien, et Filou a
apprécié la promenade ?") — die Antwort auf ein Werkzeugergebnis, das
nie eine Antwort wollte. Der Nachhol-Mechanismus aus PR #9 ist fuer
get_weather richtig (17.08.: "Camille geht nicht ran") und fuer
note_error falsch: Spec §4.3 nennt das Sammeln ausdruecklich still.
"""

import pytest
from test_realtime import FakeTransport

from kitelefon.realtime import STILLE_WERKZEUGE, RealtimeSession, SessionState
from kitelefon.roles import ROLES


def _session(t):
    return RealtimeSession(
        transport=t,
        role=ROLES[0],
        on_audio=lambda b: None,
        tool_handler=lambda n, a: {"ok": True},
    )


def _gesendete(t, typ):
    return [m for m in t.sent if m.get("type") == typ]


def test_note_error_ist_als_still_deklariert():
    assert "note_error" in STILLE_WERKZEUGE


async def test_note_error_waehrend_antwort_holt_nichts_nach():
    t = FakeTransport()
    s = _session(t)
    await s._handle_server_event({"type": "response.created"})
    await s._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "note_error",
            "call_id": "c1",
            "arguments": '{"original": "je vais visiter mes médecins"}',
        }
    )
    # Ergebnis geht zurueck, aber nichts wird vorgemerkt:
    assert len(_gesendete(t, "conversation.item.create")) == 1
    await s._handle_server_event({"type": "response.done"})
    assert _gesendete(t, "response.create") == []


async def test_note_error_im_leerlauf_stoesst_nichts_an():
    t = FakeTransport()
    s = _session(t)
    s._transition(SessionState.LISTENING)
    await s._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "note_error",
            "call_id": "c1",
            "arguments": "{}",
        }
    )
    assert len(_gesendete(t, "conversation.item.create")) == 1
    assert _gesendete(t, "response.create") == []


@pytest.mark.parametrize("werkzeug", ["get_weather", "delegate_task"])
async def test_sprechendes_werkzeug_bleibt_unveraendert_laut(werkzeug):
    # Der Nachhol-Mechanismus fuer sprechende Werkzeuge bleibt: ohne ihn
    # ging Camille am 17.08. nicht ran. delegate_task pinnt den Normalfall
    # der Berlinerin — landet es je in STILLE_WERKZEUGE, bleibt die Figur
    # nach jeder Delegation stumm.
    t = FakeTransport()
    s = _session(t)
    await s._handle_server_event({"type": "response.created"})
    await s._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": werkzeug,
            "call_id": "c2",
            "arguments": "{}",
        }
    )
    await s._handle_server_event({"type": "response.done"})
    assert len(_gesendete(t, "response.create")) == 1
