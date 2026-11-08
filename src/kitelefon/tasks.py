import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import psycopg

log = logging.getLogger(__name__)


class TaskSink(Protocol):
    """Wohin eine Delegation geschrieben wird — lokal oder remote.

    Gleiche Rolle wie HookSwitch/Dialer: eine Schnittstelle, zwei
    Implementierungen, die Aufrufer merken den Unterschied nicht.
    """

    def enqueue(
        self,
        *,
        agent_type: str,
        description: str,
        deliver_via: str,
        deliver_to: str | None,
        payload: dict[str, Any],
        konsument: str | None = None,
    ) -> int: ...

    def nachtraeglich_per_mail(self, task_id: int, an: str) -> bool:
        """Ein bereits zugestelltes Ergebnis zusaetzlich per Mail schicken.

        Nicht jede Queue kann das — die lokale SQLite-Ablage kennt die
        Zustellung des Heim-Agenten gar nicht. Sie meldet deshalb False,
        statt so zu tun, als sei die Mail unterwegs.
        """
        return False

    def zustellung_aendern(self, task_id: int, weg: str, an: str) -> bool:
        """Bei einer bestehenden Aufgabe umstellen, wie das Ergebnis kommt.

        Anders als nachtraeglich_per_mail geht das in jedem Zustand — der
        haeufigste Fall ist, dass der Anrufer es sich Sekunden nach dem
        Anlegen anders ueberlegt.
        """
        return False

    def als_zugestellt_melden(self, task_id: int) -> bool:
        """Das Ergebnis wurde wirklich vorgelesen.

        Das Abheben allein ist keine Zustellung: am 14.08. galt ein Rueckruf
        als zugestellt, bei dem ausser der Begruessung nichts gesagt wurde.
        Nur das Telefon weiss, ob der Text tatsaechlich herauskam.
        """
        return False


AgentType = Literal["research", "reminder", "booking", "other", "sprachkurs_auswertung"]
DeliverVia = Literal["email", "callback", "elektra"]

VALID_AGENT_TYPES = {"research", "reminder", "booking", "other", "sprachkurs_auswertung"}
VALID_DELIVER_VIA = {"email", "callback", "elektra"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    agent_type   TEXT NOT NULL,
    description  TEXT NOT NULL,
    deliver_via  TEXT NOT NULL,
    deliver_to   TEXT,
    payload_json TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    result       TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    konsument    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_agent ON tasks(status, agent_type);
"""


class TaskQueue:
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Migration fuer bereits existierende tasks.db-Dateien (SCHEMA
            # laeuft nur CREATE-once): 'konsument' kam erst mit Plan 3 dazu.
            # Tolerant gegen einen bereits migrierten Stand.
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN konsument TEXT")
            except sqlite3.OperationalError:
                pass  # Spalte gibt es schon

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(
        self,
        *,
        agent_type: str,
        description: str,
        deliver_via: str,
        deliver_to: str | None,
        payload: dict[str, Any],
        konsument: str | None = None,
    ) -> int:
        if agent_type not in VALID_AGENT_TYPES:
            raise ValueError(f"invalid agent_type: {agent_type!r}")
        if deliver_via not in VALID_DELIVER_VIA:
            raise ValueError(f"invalid deliver_via: {deliver_via!r}")

        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tasks (created_at, agent_type, description, deliver_via, deliver_to, payload_json, konsument)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    agent_type,
                    description,
                    deliver_via,
                    deliver_to,
                    json.dumps(payload),
                    konsument,
                ),
            )
            return cur.lastrowid

    def list_pending(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def mark_synced(self, task_id: int) -> None:
        """Aufgabe wurde in die Remote-Queue uebertragen — lokal abhaken."""
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET status='synced' WHERE id=?", (task_id,))

    # Die Vorgaben im Protocol greifen nur bei ausdruecklicher Vererbung —
    # strukturell geprueft fehlen sie hier einfach. Ohne diese drei Methoden
    # wirft ein Rueckruf einen AttributeError, sobald der lokale Puffer im
    # Einsatz ist (Postgres beim Start nicht erreichbar). Sie melden ehrlich
    # False: dieser Puffer kennt die Zustellung des Heim-Agenten nicht.
    def nachtraeglich_per_mail(self, task_id: int, an: str) -> bool:
        log.warning("Lokaler Puffer: #%s laesst sich nicht per Mail nachreichen", task_id)
        return False

    def zustellung_aendern(self, task_id: int, weg: str, an: str) -> bool:
        log.warning("Lokaler Puffer: Zustellweg von #%s nicht aenderbar", task_id)
        return False

    def als_zugestellt_melden(self, task_id: int) -> bool:
        log.warning("Lokaler Puffer: Zustellung von #%s nicht vermerkbar", task_id)
        return False


class RemoteTaskQueue:
    """Schreibt in die Railway-Queue; bei Netzausfall in die lokale SQLite.

    Der Realtime-Handler ist synchron und laeuft mitten im Gespraech —
    deshalb connect_timeout=2 und sofortiger Fallback statt Retry. Das
    Telefon soll nie haengen, nur weil die Datenbank gerade nicht da ist.
    """

    SOURCE = "telefon-ki"

    def nachtraeglich_per_mail(self, task_id: int, an: str) -> bool:
        """Waehrend eines Rueckrufs: das Ergebnis zusaetzlich per Mail.

        Nur fuer fertige Aufgaben — es gibt sonst nichts zu verschicken.
        """
        return self._zustellung(task_id, "email", an, nur_fertige=True)

    def als_zugestellt_melden(self, task_id: int) -> bool:
        try:
            with psycopg.connect(self._url, connect_timeout=2) as conn:
                cur = conn.execute(
                    "UPDATE tasks SET delivered_at = now() WHERE id = %s AND delivered_at IS NULL",
                    (task_id,),
                )
                conn.commit()
                return cur.rowcount == 1
        except psycopg.Error as exc:
            log.warning("Zustellung von #%s nicht vermerkt: %s", task_id, exc)
            return False

    def zustellung_aendern(self, task_id: int, weg: str, an: str) -> bool:
        """Umstellen, egal in welchem Zustand die Aufgabe gerade ist.

        Am 14.08. sagte die Gaertnerin eine Mail zu und konnte sie nicht
        einloesen: nachtraeglich_per_mail verlangt status='done', die
        Aufgaben waren aber gerade erst angelegt. Beide haetten stattdessen
        angerufen — zugesagt und nicht getan.
        """
        return self._zustellung(task_id, weg, an, nur_fertige=False)

    def _zustellung(self, task_id: int, weg: str, an: str, *, nur_fertige: bool) -> bool:
        """Wir verschicken hier KEINE Mail.

        Wir setzen die Aufgabe auf unzugestellt zurueck und aendern den Weg —
        den Rest macht der Zusteller des Heim-Agenten innerhalb einer Minute.
        So gibt es den Mailversand genau einmal im Haus, mit einem Schluessel
        und einem Fehlerpfad, statt zweimal in zwei Repos.
        """
        if weg not in VALID_DELIVER_VIA:
            return False
        try:
            with psycopg.connect(self._url, connect_timeout=2) as conn:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET deliver_via = %s, deliver_to = %s,
                           delivered_at = NULL, delivery_attempts = 0,
                           last_attempt_at = NULL
                     WHERE id = %s AND (status = 'done' OR NOT %s)
                    """,
                    (weg, an or None, task_id, nur_fertige),
                )
                conn.commit()
                return cur.rowcount == 1
        except Exception as e:  # noqa: BLE001 — mitten im Gespraech, nie werfen
            log.warning("Mail-Nachreichung fuer #%s fehlgeschlagen: %s", task_id, e)
            return False

    def __init__(self, database_url: str, fallback: TaskQueue):
        self._url = database_url
        self._fallback = fallback

    def enqueue(
        self,
        *,
        agent_type: str,
        description: str,
        deliver_via: str,
        deliver_to: str | None,
        payload: dict[str, Any],
        konsument: str | None = None,
    ) -> int:
        if agent_type not in VALID_AGENT_TYPES:
            raise ValueError(f"invalid agent_type: {agent_type!r}")
        if deliver_via not in VALID_DELIVER_VIA:
            raise ValueError(f"invalid deliver_via: {deliver_via!r}")

        try:
            task_id = self._insert(
                agent_type,
                description,
                deliver_via,
                deliver_to,
                payload,
                konsument,
            )
            self._flush_fallback()
            return task_id
        except psycopg.Error as e:
            log.warning("Queue nicht erreichbar (%s) — Aufgabe lokal gepuffert", e)
            return self._fallback.enqueue(
                agent_type=agent_type,
                description=description,
                deliver_via=deliver_via,
                deliver_to=deliver_to,
                payload=payload,
                konsument=konsument,
            )

    def _insert(
        self,
        agent_type: str,
        description: str,
        deliver_via: str,
        deliver_to: str | None,
        payload: dict[str, Any],
        konsument: str | None = None,
    ) -> int:
        # Die Spalte 'konsument' gibt es erst nach Plan 3 — nur mitschicken,
        # wenn tatsaechlich gesetzt, sonst bricht das INSERT auf einer
        # aelteren Tabelle.
        if konsument is not None:
            sql = """
                INSERT INTO tasks (source, agent_type, description,
                                   deliver_via, deliver_to, payload_json,
                                   konsument)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """
            params = (
                self.SOURCE,
                agent_type,
                description,
                deliver_via,
                deliver_to,
                json.dumps(payload),
                konsument,
            )
        else:
            sql = """
                INSERT INTO tasks (source, agent_type, description,
                                   deliver_via, deliver_to, payload_json)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """
            params = (
                self.SOURCE,
                agent_type,
                description,
                deliver_via,
                deliver_to,
                json.dumps(payload),
            )
        with psycopg.connect(self._url, connect_timeout=2) as conn:
            row = conn.execute(sql, params).fetchone()
            conn.commit()
            return row[0]

    def _flush_fallback(self) -> None:
        """Lokal gepufferte Aufgaben nachschieben. Fehler hier sind nicht fatal."""
        for task in self._fallback.list_pending():
            try:
                self._insert(
                    task["agent_type"],
                    task["description"],
                    task["deliver_via"],
                    task["deliver_to"],
                    json.loads(task["payload_json"]) if task["payload_json"] else {},
                    task["konsument"],
                )
            except psycopg.Error:
                return  # Verbindung wieder weg — Rest bleibt im Puffer
            self._fallback.mark_synced(task["id"])
