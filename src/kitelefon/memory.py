"""Ein kleines Gedaechtnis pro Rolle.

Jede Figur ist ein eigener Bekannter: was Hein weiss, weiss Marlene nicht.
Deshalb ist die Rolle der Schluessel und es gibt bewusst keine gemeinsame
Ablage — das ist eine Entwurfsentscheidung, keine Bequemlichkeit.

Zwei Arten von Erinnerung, mit unterschiedlicher Haltbarkeit:

- **Fakten** (Vorlieben, ein paar Personendaten) bleiben. Sie sind der
  Grund, warum sich ein Wiedersehen ueberhaupt wie eines anfuehlt.
- **Das letzte Gespraech** verfaellt nach zwei Wochen. Wer sich nach einem
  Monat noch an ein beilaeufiges Detail erinnert, wirkt nicht vertraut,
  sondern unheimlich.

Die Obergrenzen sind knapp gewaehlt: das Gedaechtnis wandert bei jedem
Anruf in den System-Prompt, und ein aufgeblaehter Prompt verwaessert genau
die Verhaltensregeln, die wir gerade erst geschaerft haben.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

MAX_FAKTEN = 8
MAX_GESPRAECHS_ALTER = timedelta(days=14)

SCHEMA = """
CREATE TABLE IF NOT EXISTS role_memory (
    rolle             TEXT PRIMARY KEY,
    fakten_json       TEXT NOT NULL DEFAULT '[]',
    letztes_gespraech TEXT,
    letztes_am        TEXT
);
"""


class RoleMemory:
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def load(self, rolle: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM role_memory WHERE rolle = ?", (rolle,)).fetchone()
        if row is None:
            return None
        try:
            fakten = json.loads(row["fakten_json"])
        except json.JSONDecodeError:
            log.warning("Fakten für '%s' unlesbar, ignoriert", rolle)
            fakten = []
        return {
            "fakten": fakten,
            "letztes_gespraech": row["letztes_gespraech"],
            "letztes_am": row["letztes_am"],
        }

    def save(
        self,
        rolle: str,
        *,
        fakten: list[str],
        zusammenfassung: str,
        jetzt: datetime,
    ) -> None:
        """Fakten ergaenzen, letztes Gespraech ersetzen.

        Fakten werden zusammengefuehrt statt ueberschrieben — sonst
        verlernt die Figur bei jedem Anruf alles, was diesmal nicht zur
        Sprache kam. Neue Fakten stehen vorn, damit beim Kuerzen das
        Aelteste faellt.
        """
        alt = self.load(rolle) or {"fakten": []}
        zusammen: list[str] = []
        for f in [*fakten, *alt["fakten"]]:
            f = f.strip()
            if f and f.lower() not in {z.lower() for z in zusammen}:
                zusammen.append(f)
        zusammen = zusammen[:MAX_FAKTEN]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO role_memory (rolle, fakten_json, letztes_gespraech, letztes_am)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(rolle) DO UPDATE SET
                    fakten_json = excluded.fakten_json,
                    letztes_gespraech = excluded.letztes_gespraech,
                    letztes_am = excluded.letztes_am
                """,
                (
                    rolle,
                    json.dumps(zusammen, ensure_ascii=False),
                    zusammenfassung.strip() or None,
                    jetzt.isoformat(),
                ),
            )
            conn.commit()

    def prompt_block(self, rolle: str, jetzt: datetime | None = None) -> str:
        """Prompt-Abschnitt oder leerer String, wenn es nichts zu sagen gibt."""
        jetzt = jetzt or datetime.now(UTC)
        eintrag = self.load(rolle)
        if not eintrag:
            return ""

        zeilen: list[str] = []
        for f in eintrag["fakten"]:
            zeilen.append(f"- {f}")

        letztes = self._frisches_gespraech(eintrag, jetzt)
        if letztes:
            zeilen.append(f"- Beim letzten Mal: {letztes}")

        if not zeilen:
            return ""
        return (
            "# Was du vom Anrufer schon weißt\n"
            "- Das stammt aus früheren Anrufen. Nutze es beiläufig, wenn es "
            "passt.\n"
            "- ZÄHLE ES NIE AUF und frag nicht ab, ob es noch stimmt.\n" + "\n".join(zeilen) + "\n"
        )

    @staticmethod
    def _frisches_gespraech(eintrag: dict, jetzt: datetime) -> str | None:
        text = eintrag.get("letztes_gespraech")
        stamp = eintrag.get("letztes_am")
        if not text or not stamp:
            return None
        try:
            am = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if am.tzinfo is None:
            am = am.replace(tzinfo=UTC)
        if jetzt - am > MAX_GESPRAECHS_ALTER:
            return None
        return text
