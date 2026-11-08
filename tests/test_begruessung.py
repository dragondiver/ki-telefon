"""Die Begruessung gehoert in die Leitung, nicht ins Freizeichen.

Sitzung 8 am 18.08.: session.updated um 20:50:17 loeste sofort die
Begruessung aus, verbunden war der Anruf erst 20:50:25. Die Audio staute
sich hinter dem Freizeichen; Paulas erstes "Hallo?" raeumte sie per
Barge-in ab — sie hoerte nie ein Bonjour. Deshalb: kein automatisches
response.create mehr bei session.updated, sondern begruessen() aus
_in_call(), wenn die Leitung wirklich steht.
"""

from test_realtime import FakeTransport

from kitelefon.realtime import RealtimeSession, SessionState
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


async def test_session_updated_begruesst_nicht_mehr_von_selbst():
    t = FakeTransport()
    s = _session(t)
    await s._handle_server_event({"type": "session.created"})
    await s._handle_server_event({"type": "session.updated"})
    assert _gesendete(t, "response.create") == []


async def test_begruessen_nach_session_updated_stoesst_sofort_an():
    t = FakeTransport()
    s = _session(t)
    await s._handle_server_event({"type": "session.created"})
    await s._handle_server_event({"type": "session.updated"})
    await s.begruessen()
    assert len(_gesendete(t, "response.create")) == 1


async def test_begruessen_vor_session_updated_wird_nachgeholt():
    # Der eingehende Anruf: _in_call kann vor session.updated dran sein
    # (die Session entsteht dort erst nach dem Abheben).
    t = FakeTransport()
    s = _session(t)
    await s.begruessen()  # Leitung steht, Session noch nicht
    assert _gesendete(t, "response.create") == []
    await s._handle_server_event({"type": "session.created"})
    await s._handle_server_event({"type": "session.updated"})
    assert len(_gesendete(t, "response.create")) == 1


async def test_begruessen_wird_nicht_doppelt_nachgeholt():
    t = FakeTransport()
    s = _session(t)
    await s.begruessen()
    await s._handle_server_event({"type": "session.created"})
    await s._handle_server_event({"type": "session.updated"})
    await s._handle_server_event({"type": "session.updated"})
    assert len(_gesendete(t, "response.create")) == 1


async def test_begruessen_waehrend_paula_spricht_stoesst_sofort_an():
    # Der echte Pfad ruft begruessen() nur einmal frueh auf — die Zustaende
    # USER_SPEAKING/ASSISTANT_RESPONDING sind trotzdem als "Leitung steht"
    # gemeint und duerfen die Eroeffnung nicht liegen lassen.
    t = FakeTransport()
    s = _session(t)
    await s._handle_server_event({"type": "session.created"})
    await s._handle_server_event({"type": "session.updated"})
    await s._handle_server_event({"type": "input_audio_buffer.speech_started"})
    assert s.state is SessionState.USER_SPEAKING
    await s.begruessen()
    assert len(_gesendete(t, "response.create")) == 1
    assert s._begruessung_ausstehend is False


async def test_begruessen_waehrend_antwort_wird_bei_response_done_nachgeholt():
    t = FakeTransport()
    s = _session(t)
    await s._handle_server_event({"type": "session.created"})
    await s._handle_server_event({"type": "session.updated"})
    await s._handle_server_event({"type": "response.created"})
    assert s.state is SessionState.ASSISTANT_RESPONDING
    await s.begruessen()
    # Nicht sofort — das liefe der laufenden Antwort in die Quere …
    assert _gesendete(t, "response.create") == []
    assert s._begruessung_ausstehend is False
    # … sondern vorgemerkt und beim naechsten freien Turn nachgeholt.
    await s._handle_server_event({"type": "response.done"})
    assert len(_gesendete(t, "response.create")) == 1
