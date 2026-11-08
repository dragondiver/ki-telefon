"""_gepufferte_aufgaben_nachspuelen: der Puffer wird beim Start mitgespuelt.

RemoteTaskQueue.enqueue() spuelt lokal gepufferte Aufgaben nur beilaeufig
mit, wenn danach ueberhaupt wieder etwas eingereicht wird. Eine bei
DB-Ausfall gepufferte Sprachkurs-Auswertung soll nicht erst auf den
naechsten Anruf warten — deshalb wird beim Start explizit nachgespuelt.
"""

import asyncio

from kitelefon.__main__ import _gepufferte_aufgaben_nachspuelen, _Lazy
from kitelefon.roles import ROLES
from kitelefon.tasks import RemoteTaskQueue, TaskQueue


class _FakeRemoteTaskQueue(RemoteTaskQueue):
    """RemoteTaskQueue, ohne echte DB-Verbindung — nur _flush_fallback zaehlt."""

    def __init__(self):  # kein super().__init__(): keine echte URL noetig
        self.aufrufe = 0
        self.wirft = False

    def _flush_fallback(self) -> None:
        self.aufrufe += 1
        if self.wirft:
            raise RuntimeError("Datenbank weiterhin weg")


def test_flush_wird_fuer_remote_queue_aufgerufen():
    queue = _FakeRemoteTaskQueue()
    _gepufferte_aufgaben_nachspuelen(queue)
    assert queue.aufrufe == 1


def test_kein_flush_ohne_remote_queue(tmp_path):
    """Ohne TASKS_DATABASE_URL ist queue eine reine lokale TaskQueue —
    kein Puffer, kein Nachspuelen noetig, und vor allem kein Absturz."""
    lokal = TaskQueue(tmp_path / "tasks.db")
    _gepufferte_aufgaben_nachspuelen(lokal)  # wirft nicht


def test_fehlschlag_beim_flush_wird_geschluckt():
    """Ein kaputter Flush (DB immer noch weg) darf den Telefonstart nicht
    verhindern — genau der Fehlermodus, den spool_nachtragen daneben
    schon abfaengt."""
    queue = _FakeRemoteTaskQueue()
    queue.wirft = True
    _gepufferte_aufgaben_nachspuelen(queue)  # wirft nicht, obwohl der Flush wirft
    assert queue.aufrufe == 1


# --- _Lazy: die Verdrahtung zu begruessen() -------------------------------
#
# Finalreview 19.08.: _Lazy (die Huelle, die open_session() zurueckgibt, bis
# der Websocket-Aufbau fertig ist) hatte weder eine begruessen()-Methode
# noch __getattr__-Weiterleitung. Der getattr-Schutz in
# CallManager._in_call griff deshalb bei JEDEM produktiven Anruf — gewaehlt
# wie eingehend — und schluckte die Begruessung still, obwohl
# tests/test_begruessung.py komplett gruen war (die pruefen nur
# RealtimeSession.begruessen(), nie diese Huelle). Bis zu diesem Fix war
# _Lazy nirgends direkt Testgegenstand — deshalb hier.


class _FakeSettings:
    """Reicht fuer den else-Zweig (nicht-Sprachkurs) von _Lazy.run()."""

    vad_threshold = 0.6
    vad_prefix_padding_ms = 500
    vad_silence_ms = 800
    openai_model = "gpt-realtime-2.1-mini"
    sprachkurs_model = "gpt-realtime-2.1-mini"
    rundfunk_verleih_url = ""  # Freisprechen (#38) nicht eingerichtet
    wohnort = ""  # kein Wohnort (#141)


class _FakeInnerSession:
    """Ersetzt RealtimeSession in den _Lazy-Tests — zaehlt nur begruessen()
    und run(), ohne echten Websocket."""

    def __init__(self, **kwargs):
        self.begruesst = 0
        self.gelaufen = False

    async def begruessen(self):
        self.begruesst += 1

    async def run(self):
        self.gelaufen = True


def _lazy(transport_future):
    return _Lazy(
        ist_sprachkurs=False,
        transport_future=transport_future,
        role=ROLES[0],
        on_audio=lambda pcm: None,
        tool_handler=lambda name, args: {"ok": True},
        settings=_FakeSettings(),
        memory=None,
        sprachkurs_session_holder={},
    )


async def test_lazy_begruessen_nach_inner_wird_sofort_weitergereicht():
    """Steht _inner schon (Websocket-Aufbau ist fertig), muss begruessen()
    sofort durchgereicht werden — kein Warten, kein Merken."""
    lazy = _lazy(asyncio.Future())
    fake_inner = _FakeInnerSession()
    lazy._inner = fake_inner

    await lazy.begruessen()

    assert fake_inner.begruesst == 1


async def test_lazy_begruessen_vor_inner_wird_von_run_genau_einmal_nachgeholt(monkeypatch):
    """Kommt begruessen(), bevor run() die Session gebaut hat — z.B. weil
    _await_session_ready in call_manager.py bei langsamem Postgres nach 5s
    aufgibt und _in_call trotzdem weiterlaeuft (siehe call_manager.py
    ~Zeile 574) — darf der Gruss nicht verpuffen. run() muss ihn genau
    einmal nachholen, sobald _inner existiert."""
    monkeypatch.setattr("kitelefon.__main__.RealtimeSession", _FakeInnerSession)

    transport_future = asyncio.Future()
    transport_future.set_result(object())  # run() awaitet ihn nur
    lazy = _lazy(transport_future)

    await lazy.begruessen()
    assert lazy._begruessung_pending is True  # noch niemand da, der es entgegennimmt

    await lazy.run()

    assert lazy._inner is not None
    assert lazy._inner.begruesst == 1
    assert lazy._inner.gelaufen is True
    assert lazy._begruessung_pending is False  # nicht nochmal beim naechsten begruessen()
