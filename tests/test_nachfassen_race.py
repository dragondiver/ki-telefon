"""nachfassen()-Race: Paula faengt genau im Fenster zwischen dem Einspeisen
des Hinweistexts und dem eigenen Antwort-Anstoss an zu sprechen.

Finalreview 19.08.: Sitzungsuhr (Stille-Nachfrage, Phasen-Events) ruft
session.nachfassen() ueber mehrere awaits. Beginnt Paula GENAU in diesem
Moment zu sprechen, meldet server_vad waehrenddessen
input_audio_buffer.speech_started, der Zustand springt auf USER_SPEAKING —
aber nachfassen() schickte bis dahin sein eigenes response.create trotzdem
los. Die serverseitige VAD-Antwort auf Paulas Aeusserung lief dem ins Wort:
conversation_already_has_active_response. Der bestehende Error-Zweig
whitelistete nur die response.cancel-Race-Variante ("cancel" im Text) —
dieser Fehler enthaelt kein "cancel", also landete die Session in ERROR,
und send_mic_frame() gatet ERROR aus: das Mikro blieb fuer den Rest des
Anrufs taub.

Zwei Bausteine werden hier geprueft:
(a) nachfassen() ueberspringt den eigenen Antwort-Anstoss, wenn der Zustand
    nach dem Einspeisen bereits auf USER_SPEAKING gesprungen ist.
(b) Kommt es trotzdem zum Wettlauf, behandelt der Error-Zweig
    conversation_already_has_active_response als gutartig statt die Session
    in ERROR zu werfen (wie die response.cancel-Race es schon lange tut,
    siehe tests/test_bargein.py::test_verspaeteter_abbruch_beendet_das_
    gespraech_nicht).
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


class RaceTransport(FakeTransport):
    """Wie FakeTransport, loest aber synchron nach dem ERSTEN send() (dem
    conversation.item.create aus nachfassen()) den Zustandswechsel aus, den
    server_vad ausloesen wuerde, wenn Paula genau in diesem Fenster zu
    sprechen anfaengt. Das ist der sauberste deterministische Weg, das
    await-Fenster in nachfassen() nachzustellen, ohne echte Nebenlaeufigkeit
    zu brauchen — die Session wird per Setter nachgereicht, weil sie beim
    Bau des Transports noch nicht existiert."""

    def __init__(self):
        super().__init__()
        self.session: RealtimeSession | None = None

    async def send(self, msg: str) -> None:
        await super().send(msg)
        if len(self.sent) == 1 and self.session is not None:
            self.session._transition(SessionState.USER_SPEAKING)


async def test_nachfassen_ueberspringt_response_create_bei_sprechbeginn():
    t = RaceTransport()
    s = _session(t)
    t.session = s
    s._transition(SessionState.LISTENING)

    await s.nachfassen("Bist du noch da?")

    # Der Hinweis wurde eingespeist ...
    assert len(_gesendete(t, "conversation.item.create")) == 1
    # ... aber KEIN eigenes response.create — das wuerde der VAD-Antwort
    # auf Paulas Aeusserung in die Quere kommen.
    assert _gesendete(t, "response.create") == []
    assert s.state == SessionState.USER_SPEAKING


async def test_nachfassen_ohne_zwischenfunken_stoesst_response_create_an():
    """Regression: das Bestandsverhalten (kein Zwischenfunken) bleibt
    unveraendert — genau ein response.create."""
    t = FakeTransport()
    s = _session(t)
    s._transition(SessionState.LISTENING)

    await s.nachfassen("Bist du noch da?")

    assert len(_gesendete(t, "conversation.item.create")) == 1
    assert len(_gesendete(t, "response.create")) == 1


async def test_conversation_already_has_active_response_ist_gutartig():
    t = FakeTransport()
    s = _session(t)
    s._transition(SessionState.LISTENING)

    await s._handle_server_event(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Conversation already has an active response",
            },
        }
    )

    # Die Session lebt weiter — kein ERROR, Zustand unveraendert.
    assert s.state == SessionState.LISTENING
    assert s.state is not SessionState.ERROR


async def test_response_cancel_race_bleibt_weiterhin_gutartig():
    """Die aeltere Cancel-Race-Whitelist darf durch den neuen Zweig nicht
    verdraengt werden (Bestandstest dafuer: tests/test_bargein.py::
    test_verspaeteter_abbruch_beendet_das_gespraech_nicht — hier zusaetzlich
    direkt am Error-Zweig geprueft)."""
    t = FakeTransport()
    s = _session(t)
    s._transition(SessionState.ASSISTANT_RESPONDING)

    await s._handle_server_event(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Cancellation failed: no active response found",
            },
        }
    )

    assert s.state is not SessionState.ERROR
    assert s.state == SessionState.ASSISTANT_RESPONDING
