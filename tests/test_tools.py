import pytest

from kitelefon.tasks import TaskQueue
from kitelefon.tools import (
    DELEGATE_TASK_TOOL_SCHEMA,
    DelegateTaskArgs,
    handle_delegate_task,
)


@pytest.fixture
def queue(tmp_path):
    return TaskQueue(tmp_path / "tasks.db")


def test_schema_has_expected_top_level():
    s = DELEGATE_TASK_TOOL_SCHEMA
    assert s["type"] == "function"
    assert s["name"] == "delegate_task"
    assert "description" in s
    assert s["parameters"]["type"] == "object"
    props = s["parameters"]["properties"]
    assert set(props.keys()) == {"agent_type", "description", "deliver_via", "deliver_to"}
    # agent_type ist seit dem 14.08. optional mit Standardwert 'research'.
    assert set(s["parameters"]["required"]) == {"description", "deliver_via"}


def test_args_validates_good_input():
    args = DelegateTaskArgs(
        agent_type="research",
        description="Find a hotel",
        deliver_via="email",
        deliver_to="me@example.com",
    )
    assert args.agent_type == "research"


def test_args_rejects_bad_agent_type():
    with pytest.raises(ValueError):
        DelegateTaskArgs(
            agent_type="weird",
            description="x",
            deliver_via="email",
            deliver_to=None,
        )


def test_handle_delegate_task_writes_row(queue):
    raw = {
        "agent_type": "reminder",
        "description": "Buy bread tomorrow",
        "deliver_via": "callback",
        "deliver_to": None,
    }
    result = handle_delegate_task(raw, queue)
    assert result["ok"] is True
    assert isinstance(result["task_id"], int)
    rows = queue.list_pending()
    assert len(rows) == 1
    assert rows[0]["agent_type"] == "reminder"


def test_handle_delegate_task_invalid_returns_error(queue):
    raw = {"agent_type": "weird", "description": "x", "deliver_via": "email"}
    result = handle_delegate_task(raw, queue)
    assert result["ok"] is False
    assert "error" in result
    assert queue.list_pending() == []


def test_agent_type_ist_kein_pflichtfeld():
    """Am 14.08. scheiterte ein delegate_task an fehlendem agent_type.

    Das Modell musste ein Feld raten, dessen Wert fast immer 'research'
    ist — der Aufruf schlug an der Validierung fehl und brauchte einen
    zweiten Anlauf mitten im Gespraech.
    """
    from kitelefon.tools import DELEGATE_TASK_TOOL_SCHEMA, DelegateTaskArgs

    assert "agent_type" not in DELEGATE_TASK_TOOL_SCHEMA["parameters"]["required"]
    args = DelegateTaskArgs(description="Faehrpreise raussuchen", deliver_via="email")
    assert args.agent_type == "research"


class _Merker:
    """Queue-Attrappe, die sich den Aenderungswunsch merkt."""

    def __init__(self, erfolg=True):
        self.erfolg = erfolg
        self.gesehen = None

    def zustellung_aendern(self, task_id, weg, an):
        self.gesehen = (task_id, weg, an)
        return self.erfolg


def test_zustellung_aendern_reicht_durch():
    """Am 14.08. sagte die Gaertnerin eine Mail zu und hatte kein Werkzeug."""
    from kitelefon.tools import handle_zustellung_aendern

    q = _Merker()
    out = handle_zustellung_aendern(
        {"task_id": 27, "deliver_via": "email", "deliver_to": "florian"}, q
    )
    assert out["ok"] is True
    assert q.gesehen == (27, "email", "florian")


def test_zustellung_aendern_meldet_unbekannte_aufgabe():
    from kitelefon.tools import handle_zustellung_aendern

    out = handle_zustellung_aendern(
        {"task_id": 999, "deliver_via": "callback"}, _Merker(erfolg=False)
    )
    assert out["ok"] is False


def test_zustellung_aendern_lehnt_unsinn_ab():
    from kitelefon.tools import handle_zustellung_aendern

    q = _Merker()
    out = handle_zustellung_aendern({"task_id": 27, "deliver_via": "brieftaube"}, q)
    assert out["ok"] is False
    assert q.gesehen is None


def test_zustellung_aendern_ist_im_werkzeugkasten():
    from kitelefon.tools import ALL_TOOL_SCHEMAS

    namen = [s["name"] for s in ALL_TOOL_SCHEMAS]
    assert "zustellung_aendern" in namen


def test_elektra_ist_ein_zustellweg(queue):
    """rundfunk#9, 13.09.2026: Aufgaben, die über Elektra delegiert werden,
    kommen als Kuvert auf das Sprachgerät statt als Anruf ins Leere."""
    from kitelefon.tools import ALL_TOOL_SCHEMAS

    for s in ALL_TOOL_SCHEMAS:
        if s["name"] in ("delegate_task", "zustellung_aendern"):
            assert "elektra" in s["parameters"]["properties"]["deliver_via"]["enum"]
    result = handle_delegate_task(
        {"description": "Oleander schneiden?", "deliver_via": "elektra"}, queue
    )
    assert result["ok"] is True
    assert "zu Elektra" in result["ack"]
    assert queue.list_pending()[0]["deliver_via"] == "elektra"


def test_gedaechtnis_gehoert_nicht_zu_delegate_task():
    """#122: Elektra delegierte nach "Kannst du das übernehmen?" das Aufräumen
    doppelter Notizen an delegate_task, statt selbst vergiss zu rufen."""
    from kitelefon.tools import ALL_TOOL_SCHEMAS

    text = DELEGATE_TASK_TOOL_SCHEMA["description"]
    assert "Haushaltsgedächtnis" in text
    assert "erledigst du selbst mit merke, erinnere und vergiss" in text
    assert "‚übernimm das‘" in text and "‚kümmer dich drum‘" in text
    namen = {s["name"] for s in ALL_TOOL_SCHEMAS}
    assert {"merke", "erinnere", "vergiss"} <= namen
