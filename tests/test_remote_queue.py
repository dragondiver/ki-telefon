import os

import psycopg
import pytest

from kitelefon.tasks import RemoteTaskQueue, TaskQueue

TEST_URL = os.environ.get("KITELEFON_TASKS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="keine Test-DB konfiguriert")


@pytest.fixture
def remote(tmp_path):
    with psycopg.connect(TEST_URL) as conn:
        conn.execute("TRUNCATE tasks")
        conn.commit()
    fallback = TaskQueue(tmp_path / "fallback.db")
    return RemoteTaskQueue(TEST_URL, fallback=fallback)


def _enqueue(q):
    return q.enqueue(
        agent_type="research",
        description="Fahrzeit Stuttgart-Sindelfingen",
        deliver_via="email",
        deliver_to="x@example.com",
        payload={"a": 1},
    )


def test_enqueue_schreibt_remote(remote):
    task_id = _enqueue(remote)
    with psycopg.connect(TEST_URL) as conn:
        row = conn.execute(
            "SELECT source, status, description FROM tasks WHERE id=%s", (task_id,)
        ).fetchone()
    assert row == ("telefon-ki", "queued", "Fahrzeit Stuttgart-Sindelfingen")


def test_fallback_bei_db_ausfall(tmp_path):
    fallback = TaskQueue(tmp_path / "fb.db")
    q = RemoteTaskQueue("postgresql://agent:falsch@127.0.0.1:1/nix", fallback=fallback)
    task_id = _enqueue(q)
    assert task_id > 0
    assert len(fallback.list_pending()) == 1  # lokal gepuffert, nichts verloren


def test_flush_schiebt_lokale_aufgaben_nach(remote, tmp_path):
    # Aufgabe liegt im lokalen Puffer, Verbindung ist wieder da
    remote._fallback.enqueue(
        agent_type="reminder",
        description="Brot!",
        deliver_via="email",
        deliver_to=None,
        payload={},
    )
    _enqueue(remote)  # erfolgreicher enqueue stößt flush an
    with psycopg.connect(TEST_URL) as conn:
        n = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert n == 2
    assert remote._fallback.list_pending() == []
