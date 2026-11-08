import asyncio
import logging

import pytest

from kitelefon.call_manager import Sitzungsuhr


class FakeSession:
    def __init__(self, sprachkurs_call=None):
        self.nachrichten: list[str] = []
        self.sprechzeiten = (60.0, 10.0)  # KI-Anteil ~14 %
        # None heisst: der Anruf-Kontext ist (noch) nicht da — genau der
        # Zustand, den ein langsames Postgres nach start_sprachkurs_call()
        # hinterlaesst, bevor _await_session_ready() aufgibt.
        self.sprachkurs_call = sprachkurs_call

    async def nachfassen(self, text):
        self.nachrichten.append(text)


class FakeSessionMitZustand(FakeSession):
    """state wie die echte Session: ein Objekt mit .value."""

    class _Z:
        def __init__(self, wert):
            self.value = wert

    def __init__(self, sprachkurs_call=None):
        super().__init__(sprachkurs_call)
        self.state = self._Z("listening")

    def setze(self, wert):
        self.state = self._Z(wert)


class FakeCall:
    def offene_fehler_text(self):
        return "Diese Sätze stehen zur Fehlerarbeit an:\n- x → y"


@pytest.mark.asyncio
async def test_phasen_feuern_in_reihenfolge():
    session, call = FakeSession(), FakeCall()
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(session, hangup, call=call, tick_s=0.01, minute_s=0.02)  # 1 "Minute" = 20 ms
    task = asyncio.create_task(uhr.run())
    await asyncio.wait_for(hangup.wait(), timeout=2.0)
    task.cancel()
    texte = "\n".join(session.nachrichten)
    assert "PHASE_ACCURACY" in texte
    assert "x → y" in texte  # Fehlerliste kam mit
    assert "SESSION_ENDING" in texte
    assert texte.index("PHASE_ACCURACY") < texte.index("SESSION_ENDING")
    assert uhr.ended_by == "timer"


@pytest.mark.asyncio
async def test_talk_less_feuert_nur_einmal_und_erst_ab_minute_5():
    session, call = FakeSession(), FakeCall()
    session.sprechzeiten = (10.0, 60.0)  # KI-Anteil ~86 %
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(session, hangup, call=call, tick_s=0.01, minute_s=0.02)
    task = asyncio.create_task(uhr.run())
    await asyncio.wait_for(hangup.wait(), timeout=2.0)
    task.cancel()
    talk_less = [t for t in session.nachrichten if "TALK_LESS" in t]
    assert len(talk_less) == 1


@pytest.mark.asyncio
async def test_auflegen_beendet_die_uhr_leise():
    session, call = FakeSession(), FakeCall()
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(session, hangup, call=call, tick_s=0.01, minute_s=10.0)
    task = asyncio.create_task(uhr.run())
    await asyncio.sleep(0.05)
    hangup.set()  # Paula legt auf
    await asyncio.wait_for(task, timeout=1.0)
    assert uhr.ended_by == "hangup"
    assert session.nachrichten == []


@pytest.mark.asyncio
async def test_call_kommt_erst_nach_ein_paar_ticks_uhr_feuert_trotzdem():
    """Langsames Postgres: sprachkurs_call haengt am Anfang noch auf None.

    Die Uhr wird ohne 'call' konstruiert (wie in call_manager._in_call, wo
    das Attribut zum Startzeitpunkt der Uhr oft noch fehlt) und muss den
    Kontext bei einem spaeteren Tick selbst nachholen, bevor PHASE_ACCURACY
    feuert.
    """
    session = FakeSession(sprachkurs_call=None)
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(session, hangup, tick_s=0.01, minute_s=0.02)
    task = asyncio.create_task(uhr.run())

    async def spaeter_call_liefern():
        await asyncio.sleep(0.03)  # ein paar Ticks vergehen
        session.sprachkurs_call = FakeCall()

    liefer_task = asyncio.create_task(spaeter_call_liefern())
    await asyncio.wait_for(hangup.wait(), timeout=2.0)
    task.cancel()
    await liefer_task
    texte = "\n".join(session.nachrichten)
    assert "PHASE_ACCURACY" in texte
    assert "x → y" in texte  # der spaet gelieferte Call zaehlt


@pytest.mark.asyncio
async def test_call_kommt_nie_fallback_text_und_warnung(caplog):
    """Der Anruf-Kontext trifft nie ein (z.B. DB dauerhaft weg): die Uhr
    darf trotzdem PHASE_ACCURACY und SESSION_ENDING ausloesen, mit dem
    Fallback-Text statt einer echten Fehlerliste, und warnt einmalig."""
    session = FakeSession(sprachkurs_call=None)
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(session, hangup, tick_s=0.01, minute_s=0.02)
    with caplog.at_level(logging.WARNING, logger="kitelefon.call_manager"):
        task = asyncio.create_task(uhr.run())
        await asyncio.wait_for(hangup.wait(), timeout=2.0)
        task.cancel()
    texte = "\n".join(session.nachrichten)
    assert "PHASE_ACCURACY" in texte
    assert Sitzungsuhr.KEIN_CALL_FEHLERTEXT in texte
    assert "SESSION_ENDING" in texte
    warnungen = [r for r in caplog.records if "Anruf-Kontext" in r.message]
    assert len(warnungen) == 1


@pytest.mark.asyncio
async def test_stille_nachfrage_kommt_nach_langer_stille():
    session = FakeSessionMitZustand(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.05
    )
    task = asyncio.create_task(uhr.run())
    await asyncio.sleep(0.3)
    hangup.set()
    task.cancel()
    nachfragen = [t for t in session.nachrichten if "noch da" in t]
    assert 1 <= len(nachfragen) <= Sitzungsuhr.STILLE_NACHFRAGE_MAX


@pytest.mark.asyncio
async def test_stille_nachfrage_hoechstens_zweimal_bis_paula_spricht():
    session = FakeSessionMitZustand(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.03
    )
    task = asyncio.create_task(uhr.run())
    await asyncio.sleep(0.5)  # Zeit fuer weit mehr als zwei Fenster
    hangup.set()
    task.cancel()
    nachfragen = [t for t in session.nachrichten if "noch da" in t]
    assert len(nachfragen) == Sitzungsuhr.STILLE_NACHFRAGE_MAX


@pytest.mark.asyncio
async def test_sprechen_setzt_die_nachfragen_zurueck():
    session = FakeSessionMitZustand(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.03
    )
    task = asyncio.create_task(uhr.run())
    await asyncio.sleep(0.25)  # Budget aufgebraucht (2 Nachfragen)
    session.setze("user_speaking")  # Paula spricht wieder
    await asyncio.sleep(0.03)
    session.setze("listening")  # neue Stillephase
    await asyncio.sleep(0.25)
    hangup.set()
    task.cancel()
    nachfragen = [t for t in session.nachrichten if "noch da" in t]
    assert len(nachfragen) > Sitzungsuhr.STILLE_NACHFRAGE_MAX  # Budget war zurueckgesetzt


@pytest.mark.asyncio
async def test_ohne_state_attribut_keine_nachfrage():
    # Die alten FakeSessions haben kein state — die Uhr muss das aushalten.
    session = FakeSession(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.02
    )
    task = asyncio.create_task(uhr.run())
    await asyncio.sleep(0.15)
    hangup.set()
    task.cancel()
    assert not [t for t in session.nachrichten if "noch da" in t]


def test_pruefe_stille_nachfrage_phase_ereignis_hat_vorrang():
    """Bei phase_ereignis=True wird nicht nachgefragt (17.08.-Bug-Schutz)."""
    session = FakeSessionMitZustand(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.05
    )

    # Szenario: Stille schon lange genug, Budget vorhanden, aber phase_ereignis=True
    jetzt = 100.0
    still_seit = 50.0  # 50 Sekunden Stille (weit über threshold)
    nachfragen = 0

    sollte, new_still, new_nachfragen = uhr._pruefe_stille_nachfrage(
        "listening", jetzt, still_seit, nachfragen, phase_ereignis=True
    )

    # KEIN Nachfrage, Zustand unverändert
    assert sollte is False
    assert new_still == still_seit  # unverändert!
    assert new_nachfragen == nachfragen  # unverändert!


def test_pruefe_stille_nachfrage_listening_ohne_phase():
    """Ohne phase_ereignis: normale Stille-Nachfrage bei listening."""
    session = FakeSessionMitZustand(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.05
    )

    jetzt = 100.0
    still_seit = 99.96  # 0.04 Sekunden Stille, unter threshold (0.05)
    nachfragen = 0

    sollte, new_still, new_nachfragen = uhr._pruefe_stille_nachfrage(
        "listening", jetzt, still_seit, nachfragen, phase_ereignis=False
    )

    # Noch nicht fällig, Zustand unverändert
    assert sollte is False
    assert new_still == still_seit
    assert new_nachfragen == 0


def test_pruefe_stille_nachfrage_threshold_erreicht():
    """Threshold überschritten, Budget vorhanden → Nachfrage."""
    session = FakeSessionMitZustand(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.05
    )

    jetzt = 100.0
    still_seit = 94.9  # > 5 Sekunden Stille (threshold)
    nachfragen = 0

    sollte, new_still, new_nachfragen = uhr._pruefe_stille_nachfrage(
        "listening", jetzt, still_seit, nachfragen, phase_ereignis=False
    )

    # Nachfrage fällig, Budget konsumiert
    assert sollte is True
    assert new_still == jetzt  # nächstes Fenster ab jetzt
    assert new_nachfragen == 1


def test_pruefe_stille_nachfrage_budget_aufgebraucht():
    """Budget aufgebraucht → keine weitere Nachfrage."""
    session = FakeSessionMitZustand(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.05
    )

    jetzt = 100.0
    still_seit = 94.9  # > 5 Sekunden Stille
    nachfragen = 2  # Budget ausgeschöpft

    sollte, new_still, new_nachfragen = uhr._pruefe_stille_nachfrage(
        "listening", jetzt, still_seit, nachfragen, phase_ereignis=False
    )

    # Kein Nachfrage mehr, Zustand unverändert
    assert sollte is False
    assert new_still == still_seit
    assert new_nachfragen == 2


def test_pruefe_stille_nachfrage_user_speaking_reset():
    """user_speaking → Budget reset."""
    session = FakeSessionMitZustand(FakeCall())
    hangup = asyncio.Event()
    uhr = Sitzungsuhr(
        session, hangup, call=FakeCall(), tick_s=0.01, minute_s=10.0, stille_nachfrage_s=0.05
    )

    jetzt = 100.0
    still_seit = 50.0
    nachfragen = 1

    sollte, new_still, new_nachfragen = uhr._pruefe_stille_nachfrage(
        "user_speaking", jetzt, still_seit, nachfragen, phase_ereignis=False
    )

    # Kein Nachfrage, Budget reset
    assert sollte is False
    assert new_still is None  # zurückgesetzt!
    assert new_nachfragen == 0  # zurückgesetzt!
