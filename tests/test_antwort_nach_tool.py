"""Ein Werkzeugergebnis mitten in einer laufenden Antwort darf die
Folge-Antwort nicht verschlucken.

Pi-Log vom 17.08., Sitzungen 2 und 3: Camilles Begruessung rief zuerst
get_weather auf, das response.create nach dem Ergebnis wurde unterdrueckt
("es laeuft schon eine Antwort"), und gpt-realtime-mini setzt — anders als
gpt-realtime-2.1 — nach einem Werkzeugergebnis nicht von selbst fort.
Ergebnis: Camille ging nie ran.
"""

from test_realtime import FakeTransport

from kitelefon.realtime import RealtimeSession, SessionState
from kitelefon.roles import ROLES

ROLE = ROLES[0]


def _session(t):
    return RealtimeSession(
        transport=t,
        role=ROLE,
        on_audio=lambda b: None,
        tool_handler=lambda n, a: {"ok": True},
    )


def _gesendete(t, typ):
    return [m for m in t.sent if m.get("type") == typ]


async def test_tool_ergebnis_waehrend_antwort_holt_response_create_nach():
    t = FakeTransport()
    s = _session(t)
    # Antwort laeuft (die Begruessung), darin kommt der Tool-Aufruf an:
    await s._handle_server_event({"type": "response.created"})
    await s._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "get_weather",
            "call_id": "c1",
            "arguments": "{}",
        }
    )
    # Waehrend der laufenden Antwort: unterdrueckt, nichts gesendet.
    assert _gesendete(t, "response.create") == []

    # Die Antwort endet — jetzt MUSS das Nachholen kommen.
    await s._handle_server_event({"type": "response.done"})
    assert len(_gesendete(t, "response.create")) == 1

    # Und nur einmal: ein weiteres response.done legt nicht nach.
    await s._handle_server_event({"type": "response.done"})
    assert len(_gesendete(t, "response.create")) == 1


async def test_tool_ergebnis_im_leerlauf_stoesst_sofort_an():
    t = FakeTransport()
    s = _session(t)
    s._transition(SessionState.LISTENING)
    await s._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "get_weather",
            "call_id": "c1",
            "arguments": "{}",
        }
    )
    assert len(_gesendete(t, "response.create")) == 1


async def test_sprechbeginn_verwirft_die_vormerkung():
    t = FakeTransport()
    s = _session(t)
    await s._handle_server_event({"type": "response.created"})
    await s._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "get_weather",
            "call_id": "c1",
            "arguments": "{}",
        }
    )
    # Paula faengt an zu sprechen (Barge-in) — die Vormerkung muss weg,
    # sonst faehrt ihr das nachgeholte response.create ins Wort.
    await s._handle_server_event({"type": "input_audio_buffer.speech_started"})
    await s._handle_server_event({"type": "response.done"})
    assert _gesendete(t, "response.create") == []
