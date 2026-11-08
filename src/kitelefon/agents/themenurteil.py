"""Gehoert ein neuer Themenname zu einem vorhandenen Haushalts-Thema?

Das ist ein Urteil ueber Bedeutung ("Reise nach Rom" = "Urlaub Italien",
"Urlaub Spanien" != "Urlaub Italien"), und Urteile gehoeren dem LLM; Wortregeln
liefen bei "Kategorie Ding"-Namen ins Leere (#103). Der Code prueft nur die
Antwort (haushalt.py) und faellt bei jedem Fehler auf die Wortregel zurueck.

Claude Haiku, ein kurzer Aufruf. Der Werkzeugdienst bricht nach 30 s ab
(werkzeugdienst.py); hier ist nach TIMEOUT_S Schluss.
"""

import asyncio
import json
import logging
import os

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
TIMEOUT_S = 5.5
ENTSCHEIDUNGEN = ("vorhanden", "neu", "unklar")

SYSTEM = """Du ordnest einen neuen Themennamen den vorhandenen Themen eines \
Haushalts-Notizbuchs zu. Antworte NUR mit einem JSON-Objekt, ohne weiteren Text:
{"entscheidung": "vorhanden" | "neu" | "unklar", "thema": "<exakter Name eines vorhandenen Themas oder leer>"}

- "vorhanden": nur wenn der neue Name dasselbe Vorhaben oder Thema meint wie genau ein \
vorhandenes Thema. Reihenfolge, Wortzusammensetzung, Synonyme und Tippfehler sind egal: \
"Italienurlaub", "Italien" und "Reise nach Rom" meinen "Urlaub Italien". Gib in "thema" \
den Namen exakt so an, wie er in der Liste steht.
- "neu": es ist ein eigenes Thema. "Urlaub Spanien" ist NICHT "Urlaub Italien", \
"Geburtstag Paula" ist NICHT "Geburtstag Oma", "Auto Reifen" ist nicht "Auto TÜV". \
"thema" bleibt leer.
- "unklar": mehrere vorhandene Themen passen gleich gut, oder es ist wirklich offen. \
"thema" bleibt leer.
Im Zweifel "unklar", nicht "vorhanden"."""

_client: anthropic.AsyncAnthropic | None = None


class Abgelehnt(Exception):
    """Haiku hat die Anfrage verweigert (stop_reason "refusal")."""


def get_client() -> anthropic.AsyncAnthropic | None:
    """Der Haiku-Client oder None, wenn kein Schluessel konfiguriert ist."""
    global _client
    if _client is None and os.environ.get("ANTHROPIC_API_KEY"):
        _client = anthropic.AsyncAnthropic(max_retries=0)  # kein verstecktes Wiederholen
    return _client


def _json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        daten = json.loads(text)
    except json.JSONDecodeError:
        return None
    return daten if isinstance(daten, dict) else None


async def urteile(
    neuer_name: str,
    notiz: str,
    vorhandene: list[tuple[str, int]],
    *,
    client,
    timeout_s: float = TIMEOUT_S,
) -> dict | None:
    """{"entscheidung", "thema"} laut Haiku oder None bei jedem Fehler.

    Prueft nur die Form; ob "thema" wirklich in `vorhandene` steht, prueft der
    Aufrufer.
    """
    liste = "\n".join(f"- {name} ({n} Notizen)" for name, n in vorhandene)
    frage = f"Vorhandene Themen:\n{liste}\n\nNeuer Themenname: {neuer_name}\nNotiz dazu: {notiz}"
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=MODEL,
                max_tokens=100,  # reicht nur ohne Denken, deshalb thinking aus (#121)
                thinking={"type": "disabled"},
                system=SYSTEM,
                messages=[{"role": "user", "content": frage}],
            ),
            timeout_s,
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            raise Abgelehnt  # kein Fallback: gleicher Weg wie ein API-Fehler
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    except TimeoutError:
        log.warning("Themenurteil: Zeitlimit (%.1f s) bei '%s'", timeout_s, neuer_name[:60])
        return None
    except Exception as exc:  # noqa: BLE001 — merke darf nie an Haiku scheitern
        log.warning("Themenurteil: Aufruf fehlgeschlagen (%s)", type(exc).__name__)
        return None
    daten = _json(text)
    if (
        daten is None
        or daten.get("entscheidung") not in ENTSCHEIDUNGEN
        or not isinstance(daten.get("thema", ""), str)
    ):
        log.warning("Themenurteil: ungültige Antwort '%s'", text[:100])
        return None
    return {"entscheidung": daten["entscheidung"], "thema": daten.get("thema", "")}
