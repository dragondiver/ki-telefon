"""Aktuelle Websuche mitten im Gespraech (Telefon und Elektra).

Fuer alles, was nur im Netz steht und sich schnell beantworten laesst:
Spielstaende, Oeffnungszeiten, Nachrichten. Dasselbe Verfahren wie im
heim-agent (processor.py): Claude Haiku mit dem Server-Werkzeug web_search.
Lange Recherchen bleiben bei delegate_task — hier zaehlt, dass die Antwort
in wenigen Sekunden kommt und sich vorlesen laesst.

Der Werkzeugdienst bricht nach 30 s ab (werkzeugdienst.py); die Suche gibt
sich deshalb nach 22 s selbst auf und sagt das, statt ins Leere zu laufen.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import date
from urllib.parse import urlparse

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
TIMEOUT_S = 22.0
MAX_FORTSETZUNGEN = 2
MAX_QUELLEN = 3

SYSTEM = """Du beantwortest eine Frage, die jemand am Telefon gestellt hat. Suche im Netz \
und antworte so, dass man es vorlesen kann.

Heute ist {heute}.

- Antworte auf Deutsch in höchstens drei kurzen Sätzen, ohne Aufzählungen und ohne Formatierung.
- Nenne Quellen nur als Namen ("laut kicker.de"), nie als Adresse.
- Nenne Datum oder Uhrzeit, wenn die Quelle nicht von heute ist.
- Widersprechen sich die Quellen oder findest du nichts Belastbares, sag das offen. Rate nicht."""

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=0)  # kein verstecktes Wiederholen
    return _client


class Tageszaehler:
    """Zaehlt Suchen pro Kalendertag. Keine Abrechnung, nur eine Bremse gegen
    Schleifen und Missverstaendnisse (eine Suche kostet rund einen Cent)."""

    def __init__(self, limit: int, heute: Callable[[], date] = date.today):
        self._limit = limit
        self._heute = heute
        self._tag = heute()
        self._anzahl = 0

    def versuche(self) -> bool:
        heute = self._heute()
        if heute != self._tag:
            self._tag, self._anzahl = heute, 0
        if self._anzahl >= self._limit:
            return False
        self._anzahl += 1
        return True


class _Abgelehnt(Exception):
    """Haiku hat die Anfrage verweigert (stop_reason "refusal"). Kein Fallback
    auf ein anderes Modell: gleicher Weg wie ein API-Fehler."""


def _quellen(urls: list[str]) -> list[str]:
    namen: list[str] = []
    for url in urls:
        host = urlparse(url).netloc.removeprefix("www.")
        if host and host not in namen:
            namen.append(host)
    return namen[:MAX_QUELLEN]


async def _suche(client, frage: str) -> tuple[str, list[str]]:
    messages: list[dict] = [{"role": "user", "content": frage}]
    texte: list[str] = []
    zitiert: list[str] = []
    treffer: list[str] = []
    heute_str = date.today().strftime("%d.%m.%Y")
    for _ in range(MAX_FORTSETZUNGEN + 1):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM.format(heute=heute_str),
            # Haiku 5.5 denkt sonst standardmaessig; hier zaehlt die Latenz (#121).
            thinking={"type": "disabled"},
            # Die _20260209-Variante ist nur fuer Opus/Sonnet dokumentiert.
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=messages,
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            kategorie = getattr(getattr(resp, "stop_details", None), "category", None)
            raise _Abgelehnt(f"Anfrage abgelehnt (Kategorie: {kategorie})")
        for block in resp.content:
            if block.type == "web_search_tool_result":
                # Alles davor war Ankuendigung ("Ich suche kurz ..."), nicht die Antwort.
                texte.clear()
                ergebnisse = getattr(block, "content", None)
                if isinstance(ergebnisse, list):  # bei Suchfehlern ein Fehlerobjekt
                    treffer += [
                        e.url for e in ergebnisse if isinstance(getattr(e, "url", None), str)
                    ]
            elif block.type == "text":
                texte.append(block.text)
                for z in getattr(block, "citations", None) or []:
                    if isinstance(getattr(z, "url", None), str):
                        zitiert.append(z.url)
        if getattr(resp, "stop_reason", None) != "pause_turn":
            break
        messages = [*messages, {"role": "assistant", "content": resp.content}]
    antwort = " ".join(t.strip() for t in texte if t.strip())
    return antwort, zitiert or treffer


async def web_suche(frage: str, *, client=None, timeout_s: float = TIMEOUT_S) -> dict:
    frage = (frage or "").strip()
    if not frage:
        return {"ok": False, "error": "Es fehlt die Frage."}
    try:
        antwort, urls = await asyncio.wait_for(_suche(client or _get_client(), frage), timeout_s)
    except TimeoutError:
        log.warning("Websuche: Zeitlimit (%.0f s) bei '%s'", timeout_s, frage[:80])
        return {"ok": False, "error": "Die Suche hat zu lange gedauert."}
    except (anthropic.APIError, _Abgelehnt) as exc:
        log.warning("Websuche: API-Fehler: %s", exc)
        return {"ok": False, "error": "Die Suche ist gerade nicht erreichbar."}
    except Exception:  # noqa: BLE001 — Werkzeug-Handler werfen nie nach aussen
        log.exception("Websuche: unerwarteter Fehler")
        return {"ok": False, "error": "Die Suche ist gerade nicht erreichbar."}
    if not antwort:
        return {"ok": False, "error": "Die Suche hat nichts Brauchbares gefunden."}
    return {"ok": True, "antwort": antwort, "quellen": _quellen(urls)}


async def handle_web_suche(args: dict, *, zaehler: Tageszaehler | None = None, client=None) -> dict:
    frage = str(args.get("frage") or "").strip()
    if not frage:
        return {"ok": False, "error": "Es fehlt die Frage."}
    if zaehler is not None and not zaehler.versuche():
        return {"ok": False, "error": "Die Websuche ist für heute ausgeschöpft."}
    return await web_suche(frage, client=client)
