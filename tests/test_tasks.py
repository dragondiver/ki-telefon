import json
from datetime import datetime

import pytest

from kitelefon import tasks as tasks_mod
from kitelefon.tasks import RemoteTaskQueue, TaskQueue


@pytest.fixture
def queue(tmp_path):
    return TaskQueue(tmp_path / "tasks.db")


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Ersetzt psycopg.connect(), damit RemoteTaskQueue._insert() ohne
    echte Datenbank getestet werden kann — die 'konsument'-Spalte gibt es
    erst nach Plan 3, ein Test gegen eine echte DB waere also entweder
    veraltet oder haette die Migration zur Voraussetzung. Hier zaehlt nur,
    WELCHES SQL/welche Parameter RemoteTaskQueue._insert() verschickt."""

    def __init__(self, captured, naechste_id=42):
        self._captured = captured
        self._naechste_id = naechste_id

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self._captured.append((sql, params))
        return _FakeCursor((self._naechste_id,))

    def commit(self):
        pass


def test_enqueue_returns_increasing_ids(queue):
    id1 = queue.enqueue(
        agent_type="research",
        description="Find Italian restaurants in Cologne",
        deliver_via="email",
        deliver_to="me@example.com",
        payload={"raw": "..."},
    )
    id2 = queue.enqueue(
        agent_type="reminder",
        description="Buy bread tomorrow",
        deliver_via="callback",
        deliver_to=None,
        payload={},
    )
    assert id2 == id1 + 1


def test_enqueue_persists_row(queue):
    task_id = queue.enqueue(
        agent_type="research",
        description="Find a hotel",
        deliver_via="email",
        deliver_to="me@example.com",
        payload={"foo": "bar"},
    )
    rows = queue.list_pending()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == task_id
    assert row["agent_type"] == "research"
    assert row["description"] == "Find a hotel"
    assert row["deliver_via"] == "email"
    assert row["deliver_to"] == "me@example.com"
    assert row["status"] == "pending"
    assert json.loads(row["payload_json"]) == {"foo": "bar"}
    datetime.fromisoformat(row["created_at"])


def test_enqueue_rejects_invalid_agent_type(queue):
    with pytest.raises(ValueError):
        queue.enqueue(
            agent_type="invalid",
            description="x",
            deliver_via="email",
            deliver_to=None,
            payload={},
        )


def test_enqueue_rejects_invalid_deliver_via(queue):
    with pytest.raises(ValueError):
        queue.enqueue(
            agent_type="research",
            description="x",
            deliver_via="carrier_pigeon",
            deliver_to=None,
            payload={},
        )


def test_enqueue_speichert_konsument_lokal(queue):
    """Der lokale SQLite-Puffer muss 'konsument' ueberleben (F8): faellt die
    Remote-Queue beim Anlegen einer Sprachkurs-Auswertung weg, landet die
    Aufgabe hier — und beim spaeteren Nachschieben (_flush_fallback) muss
    'konsument' erhalten bleiben, sonst greift der Filter des heim-agent-
    Worker nicht mehr."""
    task_id = queue.enqueue(
        agent_type="sprachkurs_auswertung",
        description="Französisch-Sitzung auswerten",
        deliver_via="email",
        deliver_to="paula",
        payload={"session_id": 1},
        konsument="sprachkurs",
    )
    rows = queue.list_pending()
    assert len(rows) == 1
    assert rows[0]["id"] == task_id
    assert rows[0]["agent_type"] == "sprachkurs_auswertung"
    assert rows[0]["konsument"] == "sprachkurs"


def test_enqueue_ohne_konsument_bleibt_leer(queue):
    queue.enqueue(
        agent_type="research",
        description="x",
        deliver_via="email",
        deliver_to=None,
        payload={},
    )
    rows = queue.list_pending()
    assert rows[0]["konsument"] is None


def test_lokaler_puffer_kennt_die_zustellung_nicht(tmp_path):
    """Er wirft nicht, er sagt nein.

    Die Vorgaben im Protocol greifen nur bei ausdruecklicher Vererbung.
    Ohne eigene Methoden wirft ein Rueckruf einen AttributeError, sobald der
    lokale Puffer im Einsatz ist — also genau dann, wenn Postgres beim Start
    nicht erreichbar war und ohnehin schon etwas im Argen liegt.
    """
    q = TaskQueue(tmp_path / "tasks.db")
    assert q.nachtraeglich_per_mail(1, "florian") is False
    assert q.zustellung_aendern(1, "email", "florian") is False
    assert q.als_zugestellt_melden(1) is False


def test_remote_insert_mit_konsument_haengt_spalte_an(monkeypatch, tmp_path):
    captured: list = []
    monkeypatch.setattr(tasks_mod.psycopg, "connect", lambda *a, **kw: _FakeConn(captured))
    fallback = TaskQueue(tmp_path / "fallback.db")
    q = RemoteTaskQueue("postgresql://irrelevant", fallback=fallback)

    task_id = q.enqueue(
        agent_type="sprachkurs_auswertung",
        description="Französisch-Sitzung auswerten",
        deliver_via="email",
        deliver_to="paula",
        payload={"session_id": 3},
        konsument="sprachkurs",
    )

    assert task_id == 42
    sql, params = captured[0]
    assert "konsument" in sql
    assert params[-1] == "sprachkurs"


def test_remote_insert_ohne_konsument_laesst_spalte_weg(monkeypatch, tmp_path):
    captured: list = []
    monkeypatch.setattr(tasks_mod.psycopg, "connect", lambda *a, **kw: _FakeConn(captured))
    fallback = TaskQueue(tmp_path / "fallback.db")
    q = RemoteTaskQueue("postgresql://irrelevant", fallback=fallback)

    task_id = q.enqueue(
        agent_type="research",
        description="x",
        deliver_via="email",
        deliver_to=None,
        payload={},
    )

    assert task_id == 42
    sql, params = captured[0]
    assert "konsument" not in sql
    assert "sprachkurs" not in params


def test_flush_fallback_reicht_konsument_durch(monkeypatch, tmp_path):
    """F8: eine lokal gepufferte Aufgabe (Postgres beim enqueue() nicht
    erreichbar) muss beim Nachschieben ihr 'konsument' behalten — sonst
    greift der Filter des heim-agent-Worker beim Replay nicht mehr."""
    fallback = TaskQueue(tmp_path / "fallback.db")
    fallback.enqueue(
        agent_type="sprachkurs_auswertung",
        description="Französisch-Sitzung auswerten",
        deliver_via="email",
        deliver_to="paula",
        payload={"session_id": 5},
        konsument="sprachkurs",
    )

    captured: list = []
    monkeypatch.setattr(tasks_mod.psycopg, "connect", lambda *a, **kw: _FakeConn(captured))
    q = RemoteTaskQueue("postgresql://irrelevant", fallback=fallback)

    q._flush_fallback()

    assert len(captured) == 1
    sql, params = captured[0]
    assert "konsument" in sql
    assert params[-1] == "sprachkurs"
    assert fallback.list_pending() == []  # als synced markiert
