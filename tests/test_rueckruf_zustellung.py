"""Ein Rückruf gilt erst als zugestellt, wenn wirklich vorgelesen wurde.

Am 14.08. um 16:36 klingelte das Telefon, Marlene meldete sich, fragte
"Hast du 'nen Moment?" — und dann 57 Sekunden nichts. Das fertige Ergebnis
(2435 Zeichen Recherche) wurde nie vorgelesen. Trotzdem stand die Aufgabe
danach auf zugestellt, weil der Zusteller nur sein 202 vom Abheben sieht.
Kein zweiter Versuch, kein Rückfall auf Mail: das Ergebnis war verloren.
"""

from test_autocall import FakeBell
from test_call_manager import FakeAudio, FakeDialer, FakeHookSwitch, FakeSession

from kitelefon import call_manager as cm_modul
from kitelefon.call_manager import CallManager
from kitelefon.rueckruf import Rueckruf
from kitelefon.tasks import TaskQueue

RUECKRUF = Rueckruf(
    task_id=12,
    beschreibung="Fahrzeit Stuttgart–Ulm",
    ergebnis="Etwa 57 Minuten. Quelle: bahn.de",
)


class MerkQueue(TaskQueue):
    """Queue, die sich merkt, was als zugestellt gemeldet wurde."""

    def __init__(self, pfad):
        super().__init__(pfad)
        self.gemeldet: list[int] = []

    def als_zugestellt_melden(self, task_id: int) -> bool:
        self.gemeldet.append(task_id)
        return True


class StummeSession(FakeSession):
    """Sitzung, die mitschreibt, ob nachgefasst wurde."""

    def __init__(self, verlauf=None, spricht=False):
        super().__init__()
        self.verlauf = list(verlauf or [])
        self.hinweise: list[str] = []
        self._spricht = spricht

    @property
    def state(self):
        from kitelefon.realtime import SessionState

        return SessionState.ASSISTANT_RESPONDING if self._spricht else SessionState.LISTENING

    async def nachfassen(self, hinweis: str) -> None:
        self.hinweise.append(hinweis)


def _manager(tmp_path, queue=None):
    return CallManager(
        hookswitch=FakeHookSwitch([], delay=0.01),
        dialer=FakeDialer(None),
        audio=FakeAudio(),
        open_session=lambda role, on_audio, tool_handler: FakeSession(),
        queue=queue or TaskQueue(tmp_path / "tasks.db"),
        bell=FakeBell(),
        startup_ring=False,
    )


# --- Nachfassen bei Schweigen ----------------------------------------------


async def test_schweigen_loest_das_vorlesen_aus(tmp_path, monkeypatch):
    monkeypatch.setattr(cm_modul, "NACHFASS_SEKUNDEN", 0.01)
    monkeypatch.setattr(cm_modul, "NACHFASS_VERSUCHE", 1)
    cm = _manager(tmp_path)
    session = StummeSession(verlauf=[("Rückruf (Berlinerin)", "Hast du 'nen Moment?")])

    await cm._dranbleiben(session)

    assert len(session.hinweise) == 1
    assert "Lies das Ergebnis jetzt vor" in session.hinweise[0]


async def test_wer_geantwortet_hat_wird_nicht_angestupst(tmp_path, monkeypatch):
    """Redet der Anrufer, halten wir uns raus."""
    monkeypatch.setattr(cm_modul, "NACHFASS_SEKUNDEN", 0.01)
    cm = _manager(tmp_path)
    session = StummeSession(
        verlauf=[
            ("Rückruf (Berlinerin)", "Hast du 'nen Moment?"),
            ("Anrufer", "Ja, gerne"),
        ]
    )

    await cm._dranbleiben(session)

    assert session.hinweise == []


async def test_waehrend_sie_spricht_wird_nicht_dazwischengefunkt(tmp_path, monkeypatch):
    monkeypatch.setattr(cm_modul, "NACHFASS_SEKUNDEN", 0.01)
    monkeypatch.setattr(cm_modul, "NACHFASS_VERSUCHE", 1)
    cm = _manager(tmp_path)
    session = StummeSession(verlauf=[("Rückruf (Berlinerin)", "…")], spricht=True)

    await cm._dranbleiben(session)

    assert session.hinweise == []


# --- Zustellung melden ------------------------------------------------------


async def test_vorgelesenes_ergebnis_gilt_als_zugestellt(tmp_path):
    q = MerkQueue(tmp_path / "tasks.db")
    cm = _manager(tmp_path, queue=q)
    cm._letzter_verlauf = [
        ("Rückruf (Berlinerin)", "Hast du 'nen Moment?"),
        ("Anrufer", "Ja"),
        ("Rückruf (Berlinerin)", "Etwa siebenundfünfzig Minuten, Quelle ist die Bahn."),
    ]

    cm._melde_zustellung(RUECKRUF)

    assert q.gemeldet == [12]


async def test_erfundener_text_ist_keine_zustellung(tmp_path):
    """Rückruf #52 am 03.10.2026: Jean-Luc las nichts von Souvignier Gris vor,
    sondern erfand Zugverbindungen und Küstenwanderwege. 2545 Zeichen wurden
    gesprochen, das Log meldete 'zugestellt' — das Ergebnis blieb ungehört."""
    q = MerkQueue(tmp_path / "tasks.db")
    cm = _manager(tmp_path, queue=q)
    ruckruf = Rueckruf(
        task_id=52,
        beschreibung="Souvignier Gris für Ralf",
        ergebnis=(
            "Souvignier Gris ist eine pilzwiderstandsfähige Weißweinrebe aus "
            "Freiburg, gezüchtet 1983. Sie liefert fruchtige Weine mit Aromen "
            "von Birne und Aprikose und eignet sich für den ökologischen Anbau."
        ),
    )
    cm._letzter_verlauf = [
        ("Rückruf (Französischer Koch)", "Es geht um eine Verbindung. Hast du einen Moment?"),
        ("Anrufer", "Stuttgart nach Deven?"),
        (
            "Rückruf (Französischer Koch)",
            "Eh bien, " + "in Deben kannst du an der Küste spazieren. " * 60,
        ),
    ]

    cm._melde_zustellung(ruckruf)

    assert q.gemeldet == []


async def test_nur_begruessung_ist_keine_zustellung(tmp_path):
    """Der Fall vom 14.08.: abgehoben, gefragt, geschwiegen, aufgelegt."""
    q = MerkQueue(tmp_path / "tasks.db")
    cm = _manager(tmp_path, queue=q)
    cm._letzter_verlauf = [
        ("Rückruf (Berlinerin)", "Es geht um 'ne Zugverbindung. Hast du 'nen Moment?"),
    ]

    cm._melde_zustellung(RUECKRUF)

    assert q.gemeldet == []


async def test_gespraech_ohne_ergebnis_ist_keine_zustellung(tmp_path):
    """Auch ein Wortwechsel zaehlt nicht, solange nichts vorgelesen wurde."""
    q = MerkQueue(tmp_path / "tasks.db")
    cm = _manager(tmp_path, queue=q)
    cm._letzter_verlauf = [
        ("Rückruf (Berlinerin)", "Hast du 'nen Moment?"),
        ("Anrufer", "Nee, gerade schlecht"),
        ("Rückruf (Berlinerin)", "Alles gut, ich melde mich spaeter."),
    ]

    cm._melde_zustellung(RUECKRUF)

    assert q.gemeldet == []
