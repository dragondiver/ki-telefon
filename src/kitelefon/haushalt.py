"""Ein gemeinsames Gedaechtnis fuer den ganzen Haushalt, nach Themen getrennt.

Anders als memory.py (ein Gedaechtnis pro Figur, letztes Gespraech verfaellt
nach zwei Wochen) gehoert dieses Gedaechtnis niemandem: Telefon und Elektra
lesen und schreiben dieselben Themen. Ein Thema ("Urlaub Italien") lebt so
lange, wie es benutzt wird; erst 90 Tage ohne Lesen oder Schreiben lassen es
verfallen. So kann eine Urlaubsplanung ueber Wochen liegen bleiben.

Das Modell schreibt nur auf ausdrueckliche Bitte (steht in den Werkzeug-
beschreibungen, tools.py). In den Prompt kommt nur die Themenliste, nie der
Inhalt: er wandert bei jedem Gespraech mit, und ein aufgeblaehter Prompt
verwaessert die Verhaltensregeln.

Alle Zeitstempel sind UTC-ISO-Texte; `jetzt` muss deshalb zeitzonenbewusst sein.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kitelefon.agents.themenurteil import TIMEOUT_S, urteile

log = logging.getLogger(__name__)

MAX_THEMEN = 30
MAX_THEMA_ZEICHEN = 60
MAX_NOTIZEN = 40
MAX_NOTIZ_ZEICHEN = 300
VERFALL = timedelta(days=90)

SCHEMA = """
CREATE TABLE IF NOT EXISTS haushalt_themen (
    thema        TEXT PRIMARY KEY,
    anzeige      TEXT NOT NULL,
    notizen_json TEXT NOT NULL DEFAULT '[]',
    angelegt_am  TEXT NOT NULL,
    zuletzt_am   TEXT NOT NULL
);
"""


def _norm(text: str) -> str:
    """Schluessel eines Themas oder einer Notiz: klein, Leerraum zusammengezogen."""
    return " ".join(text.lower().split())


_MIN_TEILWORT = 4  # kuerzere Woerter ("fuer", "und") zaehlen nicht als Ueberschneidung
_AEHNLICH_AB = 0.75  # difflib-Verhaeltnis der Gesamtschluessel


def _woerter(schluessel: str) -> list[str]:
    return [w for w in re.split(r"[\s\-_/,.]+", schluessel) if w]


def _aehnlich(neu: str, vorhanden: str) -> bool:
    """Gehoeren zwei normalisierte Themenschluessel vermutlich zusammen?

    Teilwort-Ueberschneidung (auch Komposita: "italienurlaub" enthaelt
    "italien" und "urlaub") oder aehnliche Schreibweise. Nur Kandidaten; ob es
    dasselbe Thema ist, entscheidet das Modell, nicht der Code.
    """
    a, b = _woerter(neu), _woerter(vorhanden)
    for x in a:
        for y in b:
            if x == y:
                return True
            kurz, lang = sorted((x, y), key=len)
            if len(kurz) >= _MIN_TEILWORT and kurz in lang:
                return True
    gesamt_a, gesamt_b = "".join(a), "".join(b)
    return difflib.SequenceMatcher(None, gesamt_a, gesamt_b).ratio() >= _AEHNLICH_AB


def _notizen(row: sqlite3.Row) -> list[dict]:
    try:
        notizen = json.loads(row["notizen_json"])
    except json.JSONDecodeError:
        log.warning("Notizen fuer '%s' unlesbar, ignoriert", row["anzeige"])
        return []
    return notizen if isinstance(notizen, list) else []


def _substring_treffer(notizen: list[dict], suche: str) -> list[int]:
    """Indices aller Notizen, die den normalisierten Suchtext enthalten."""
    ziel = _norm(suche)
    return [i for i, n in enumerate(notizen) if ziel in _norm(n["text"])]


def _beste_notiz(notizen: list[dict], suche: str) -> int | None:
    """Index der Notiz, die am besten zum gesprochenen Text passt, sonst None.

    - Genau ein Substring-Treffer -> diese Notiz
    - Mehrere Substring-Treffer -> nur, wenn exakt ein Match nach _norm
    - Null Substring-Treffer -> Fuzzy-Matching ab 0.5
    Lieber nichts loeschen als die falsche Notiz.

    Rückgabe None bedeutet sowohl "kein Match" als auch "mehrdeutig".
    """
    ziel = _norm(suche)
    enthalten = _substring_treffer(notizen, suche)
    if len(enthalten) == 1:
        return enthalten[0]
    if len(enthalten) > 1:
        # Mehrere Substring-Treffer: nur exakter Match
        exakt = [i for i in enthalten if _norm(notizen[i]["text"]) == ziel]
        if len(exakt) == 1:
            return exakt[0]
        return None
    # Keine Substring-Treffer: Fuzzy-Matching über alle Notizen
    beste, wert = None, 0.0
    for i in range(len(notizen)):
        w = difflib.SequenceMatcher(None, ziel, _norm(notizen[i]["text"])).ratio()
        if w > wert:
            beste, wert = i, w
    return beste if wert >= 0.5 else None


def _pruefe_eingabe(anzeige: str, schluessel: str, text: str) -> dict | None:
    if not schluessel or not text:
        return {"ok": False, "error": "Thema und Notiz dürfen nicht leer sein."}
    if len(anzeige) > MAX_THEMA_ZEICHEN:
        return {
            "ok": False,
            "error": f"Der Themenname ist zu lang ({len(anzeige)} Zeichen, höchstens "
            f"{MAX_THEMA_ZEICHEN}). Nimm ein kürzeres Stichwort.",
        }
    if len(text) > MAX_NOTIZ_ZEICHEN:
        return {
            "ok": False,
            "error": f"Die Notiz ist zu lang ({len(text)} Zeichen, höchstens "
            f"{MAX_NOTIZ_ZEICHEN}). Fass sie kürzer.",
        }
    return None


class Haushalt:
    def __init__(self, db_path: Path, *, urteil_client=None, urteil_timeout_s: float = TIMEOUT_S):
        # Haiku-Client fuer das Themenurteil in merke_ki; None = nur Wortregel.
        self._urteil_client = urteil_client
        self._urteil_timeout_s = urteil_timeout_s
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        self.aufraeumen()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _namen(conn: sqlite3.Connection) -> list[str]:
        zeilen = conn.execute("SELECT anzeige FROM haushalt_themen ORDER BY zuletzt_am DESC")
        return [z["anzeige"] for z in zeilen]

    def merke(
        self, thema: str, notiz: str, jetzt: datetime | None = None, *, neu: bool = False
    ) -> dict:
        jetzt = jetzt or datetime.now(UTC)
        anzeige = " ".join(thema.split())
        schluessel = _norm(thema)
        text = " ".join(notiz.split())
        fehler = _pruefe_eingabe(anzeige, schluessel, text)
        if fehler:
            return fehler
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM haushalt_themen WHERE thema = ?", (schluessel,)
            ).fetchone()
            if row is None and not neu:
                kandidaten = [
                    {"thema": z["anzeige"], "notizen": len(_notizen(z))}
                    for z in conn.execute(
                        "SELECT * FROM haushalt_themen ORDER BY zuletzt_am DESC"
                    ).fetchall()
                    if _aehnlich(schluessel, z["thema"])
                ]
                if kandidaten:
                    return {
                        "ok": False,
                        "aehnliche_themen": kandidaten,
                        "hinweis": "Ähnliche Themen gefunden. Ist es dasselbe, ruf merke "
                        "erneut mit genau diesem Namen auf. Unklar: frag den Anrufer. "
                        "Wirklich neu: ruf merke mit neu=true auf.",
                    }
            if row is None:
                anzahl = conn.execute("SELECT COUNT(*) FROM haushalt_themen").fetchone()[0]
                if anzahl >= MAX_THEMEN:
                    return {
                        "ok": False,
                        "error": f"Es gibt schon {MAX_THEMEN} Themen. Frag, welches "
                        "vergessen werden soll.",
                    }
                notizen: list[dict] = []
                angelegt = jetzt.isoformat()
            else:
                notizen = _notizen(row)
                angelegt = row["angelegt_am"]
                anzeige = row["anzeige"]
            if _norm(text) not in {_norm(n["text"]) for n in notizen}:
                if len(notizen) >= MAX_NOTIZEN:
                    return {
                        "ok": False,
                        "error": f"Zu '{anzeige}' gibt es schon {MAX_NOTIZEN} Notizen. "
                        "Frag, welche vergessen werden soll.",
                    }
                notizen.append({"text": text, "am": jetzt.date().isoformat()})
            conn.execute(
                """
                INSERT INTO haushalt_themen (thema, anzeige, notizen_json, angelegt_am, zuletzt_am)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(thema) DO UPDATE SET
                    notizen_json = excluded.notizen_json,
                    zuletzt_am = excluded.zuletzt_am
                """,
                (
                    schluessel,
                    anzeige,
                    json.dumps(notizen, ensure_ascii=False),
                    angelegt,
                    jetzt.isoformat(),
                ),
            )
            conn.commit()
        return {"ok": True, "ack": f"Unter '{anzeige}' notiert ({len(notizen)} Notizen)."}

    def _themenliste(self) -> list[tuple[str, int]]:
        with self._connect() as conn:
            zeilen = conn.execute(
                "SELECT * FROM haushalt_themen ORDER BY zuletzt_am DESC"
            ).fetchall()
        return [(z["anzeige"], len(_notizen(z))) for z in zeilen]

    async def merke_ki(
        self, thema: str, notiz: str, jetzt: datetime | None = None, *, neu: bool = False
    ) -> dict:
        """Wie merke, aber ob ein aehnlicher Name dasselbe Thema meint, urteilt Haiku.

        Kein KI-Aufruf bei: neu=True, exaktem Namen (normalisiert), leerem
        Haushalt, ungueltiger Eingabe oder fehlendem Client. Der Code prueft die
        Antwort: "vorhanden" gilt nur mit einem Namen aus der Liste. Jeder Fehler
        faellt auf die Wortregel in merke() zurueck.
        """
        anzeige = " ".join(thema.split())
        schluessel = _norm(thema)
        text = " ".join(notiz.split())
        if neu or self._urteil_client is None or _pruefe_eingabe(anzeige, schluessel, text):
            return self.merke(thema, notiz, jetzt, neu=neu)
        vorhandene = self._themenliste()
        if not vorhandene or schluessel in {_norm(n) for n, _ in vorhandene}:
            return self.merke(thema, notiz, jetzt)
        urteil = await urteile(
            anzeige, text, vorhandene, client=self._urteil_client, timeout_s=self._urteil_timeout_s
        )
        if urteil is None:
            return self.merke(thema, notiz, jetzt)
        if urteil["entscheidung"] == "neu":
            return self.merke(thema, notiz, jetzt, neu=True)
        if urteil["entscheidung"] == "vorhanden":
            ziel = {_norm(n): n for n, _ in vorhandene}.get(_norm(urteil["thema"]))
            if ziel is None:
                log.warning("Themenurteil: '%s' ist kein vorhandenes Thema", urteil["thema"][:60])
                return self.merke(thema, notiz, jetzt)
            ergebnis = self.merke(ziel, notiz, jetzt)
            if ergebnis.get("ok"):
                ergebnis["zugeordnet_zu"] = ziel
            return ergebnis
        return {
            "ok": False,
            "aehnliche_themen": [{"thema": n, "notizen": z} for n, z in vorhandene],
            "hinweis": f"Unklar, ob '{anzeige}' zu einem vorhandenen Thema gehört. "
            "Frag den Anrufer. Dasselbe: ruf merke mit genau dem vorhandenen Namen "
            "erneut auf. Wirklich neu: ruf merke mit neu=true auf.",
        }

    def erinnere(self, thema: str | None = None, jetzt: datetime | None = None) -> dict:
        jetzt = jetzt or datetime.now(UTC)
        self.aufraeumen(jetzt)
        with self._connect() as conn:
            if not thema or not thema.strip():
                zeilen = conn.execute(
                    "SELECT * FROM haushalt_themen ORDER BY zuletzt_am DESC"
                ).fetchall()
                if not zeilen:
                    return {"ok": True, "themen": [], "hinweis": "Es gibt noch keine Themen."}
                return {
                    "ok": True,
                    "themen": [
                        {"thema": z["anzeige"], "notizen": len(_notizen(z))} for z in zeilen
                    ],
                }
            schluessel = _norm(thema)
            row = conn.execute(
                "SELECT * FROM haushalt_themen WHERE thema = ?", (schluessel,)
            ).fetchone()
            if row is None:
                return {
                    "ok": False,
                    "error": f"Zu '{thema.strip()}' gibt es kein Thema.",
                    "vorhandene_themen": self._namen(conn),
                }
            conn.execute(
                "UPDATE haushalt_themen SET zuletzt_am = ? WHERE thema = ?",
                (jetzt.isoformat(), schluessel),
            )
            conn.commit()
            return {"ok": True, "thema": row["anzeige"], "notizen": _notizen(row)}

    def vergiss(self, thema: str, notiz: str | None = None, jetzt: datetime | None = None) -> dict:
        jetzt = jetzt or datetime.now(UTC)
        schluessel = _norm(thema)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM haushalt_themen WHERE thema = ?", (schluessel,)
            ).fetchone()
            if row is None:
                return {
                    "ok": False,
                    "error": f"Zu '{thema.strip()}' gibt es kein Thema.",
                    "vorhandene_themen": self._namen(conn),
                }
            if not notiz or not notiz.strip():
                conn.execute("DELETE FROM haushalt_themen WHERE thema = ?", (schluessel,))
                conn.commit()
                return {"ok": True, "ack": f"Das Thema '{row['anzeige']}' ist vergessen."}
            notizen = _notizen(row)
            index = _beste_notiz(notizen, notiz)
            if index is None:
                # Unterscheide zwischen "keine Treffer" und "mehrdeutig"
                treffer = _substring_treffer(notizen, notiz)
                if len(treffer) > 1:
                    # Mehrere Treffer, aber kein eindeutiger Match
                    kandidaten = [notizen[i]["text"] for i in treffer]
                    return {
                        "ok": False,
                        "error": f"Mehrere Notizen passen dazu. Sag, welche gelöscht werden soll: {', '.join(repr(k) for k in kandidaten)}.",
                        "notizen": kandidaten,
                    }
                else:
                    # Kein Treffer
                    return {
                        "ok": False,
                        "error": "Keine Notiz passt dazu. Nichts wurde gelöscht.",
                        "notizen": [n["text"] for n in notizen],
                    }
            weg = notizen.pop(index)
            if not notizen:
                conn.execute("DELETE FROM haushalt_themen WHERE thema = ?", (schluessel,))
                ack = f"Die Notiz '{weg['text']}' ist vergessen, damit auch das Thema."
            else:
                conn.execute(
                    "UPDATE haushalt_themen SET notizen_json = ?, zuletzt_am = ? WHERE thema = ?",
                    (json.dumps(notizen, ensure_ascii=False), jetzt.isoformat(), schluessel),
                )
                ack = f"Die Notiz '{weg['text']}' ist vergessen."
            conn.commit()
        return {"ok": True, "ack": ack}

    def aufraeumen(self, jetzt: datetime | None = None) -> int:
        """Themen ohne Zugriff seit VERFALL loeschen. Gibt die Zahl der geloeschten zurueck."""
        grenze = (jetzt or datetime.now(UTC)) - VERFALL
        with self._connect() as conn:
            weg = conn.execute(
                "DELETE FROM haushalt_themen WHERE zuletzt_am < ?", (grenze.isoformat(),)
            ).rowcount
            conn.commit()
        if weg:
            log.info("Haushalts-Gedächtnis: %d verfallene Themen gelöscht", weg)
        return weg

    def themenzeile(self, jetzt: datetime | None = None) -> str:
        """Prompt-Abschnitt mit den Themennamen oder leerer String."""
        self.aufraeumen(jetzt)
        with self._connect() as conn:
            zeilen = conn.execute(
                "SELECT * FROM haushalt_themen ORDER BY zuletzt_am DESC"
            ).fetchall()
        if not zeilen:
            return ""
        liste = ", ".join(f"{z['anzeige']} ({len(_notizen(z))})" for z in zeilen)
        return (
            "# Haushalts-Gedächtnis\n"
            f"- Themen: {liste}.\n"
            "- Inhalte holst du mit erinnere. Zähle sie nie unaufgefordert auf.\n"
        )
