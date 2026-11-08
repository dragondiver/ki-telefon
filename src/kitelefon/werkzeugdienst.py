"""Werkzeug-Dienst: die Werkzeuge des Telefons für andere Geräte im Haus.

Florian, 13.09.2026: „Alles, was telefon-ai kann, sollte auch über Elektra
gehen." Elektra (rundfunk, das Sprachgerät im Wohnzimmer) hat eine eigene
OpenAI-Sitzung, aber keine Spotify-Tokens, keinen Mise-en-Place-Schlüssel und
keinen Zugang zur Aufgaben-Queue — all das liegt hier. Statt es zu kopieren,
stellt das Telefon seine Werkzeuge über HTTP bereit; rundfunk meldet sie bei
OpenAI unter denselben Namen an und reicht Aufrufe durch.

    GET  /werkzeuge          -> {"werkzeuge": [<Function-Calling-Schemas>]}
    POST /werkzeuge/<name>   -> {"ergebnis": <was der Handler zurueckgibt>}
    X-Agent-Token: <geheim>  Pflicht

    401 Token · 404 unbekanntes Werkzeug oder Pfad · 400 kein JSON

Bewusst ohne Web-Framework, wie `rueckruf.py`: zwei Routen, stdlib reicht.
Nicht fernbedienbar ist `ergebnis_per_mail` — es braucht einen laufenden
Rückruf am Telefon; ebenso `freisprechen`/`hoerer_zurueck` (#38), die den
Audioweg eines laufenden Gesprächs umlegen. Der Sprachkurs (Camille) ebenfalls nicht: seine Werkzeuge
hängen am Gesprächszustand (`sprachkurs_call.py`), das ist ein eigener Schritt.
"""

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable

from kitelefon.tools import ALL_TOOL_SCHEMAS

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
# freisprechen/hoerer_zurueck (#38) haengen am Audioweg eines laufenden
# Gespraechs — von Elektra aus ergibt das keinen Sinn.
NUR_AM_TELEFON = {"ergebnis_per_mail", "freisprechen", "hoerer_zurueck"}
FERN_WERKZEUGE = [s["name"] for s in ALL_TOOL_SCHEMAS if s["name"] not in NUR_AM_TELEFON]

Ausfuehren = Callable[[str, dict], Awaitable[dict]]


def _antwort(status: int, daten: dict) -> bytes:
    grund = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
    }
    koerper = json.dumps(daten, ensure_ascii=False).encode()
    kopf = [
        f"HTTP/1.1 {status} {grund.get(status, 'Error')}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(koerper)}",
        "Connection: close",
    ]
    return ("\r\n".join(kopf) + "\r\n\r\n").encode("latin-1") + koerper


async def _lies(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    kopf = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    zeilen = kopf.decode("latin-1").split("\r\n")
    methode, pfad, *_ = zeilen[0].split(" ")
    kopfzeilen: dict[str, str] = {}
    for z in zeilen[1:]:
        if ":" in z:
            name, wert = z.split(":", 1)
            kopfzeilen[name.strip().lower()] = wert.strip()
    laenge = min(int(kopfzeilen.get("content-length", "0") or 0), MAX_BODY)
    koerper = await reader.readexactly(laenge) if laenge else b""
    return methode, pfad.split("?", 1)[0], kopfzeilen, koerper


def make_handler(ausfuehren: Ausfuehren, *, token: str):
    schemas = [s for s in ALL_TOOL_SCHEMAS if s["name"] in FERN_WERKZEUGE]

    async def behandle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                methode, pfad, kopf, koerper = await _lies(reader)
            except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError) as e:
                log.warning("Werkzeug-Anfrage kaputt: %s", e)
                return
            if not token or not hmac.compare_digest(kopf.get("x-agent-token", ""), token):
                writer.write(_antwort(401, {"message": "Token fehlt oder stimmt nicht"}))
            elif pfad.rstrip("/") == "/werkzeuge" and methode == "GET":
                writer.write(_antwort(200, {"werkzeuge": schemas}))
            elif pfad.startswith("/werkzeuge/") and methode == "POST":
                name = pfad[len("/werkzeuge/") :].strip("/")
                if name not in FERN_WERKZEUGE:
                    writer.write(_antwort(404, {"message": f"unbekanntes Werkzeug: {name}"}))
                else:
                    try:
                        args = json.loads(koerper.decode("utf-8") or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("JSON-Objekt erwartet")
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
                        writer.write(_antwort(400, {"message": f"kein gültiges JSON: {e}"}))
                    else:
                        log.info(
                            "Fernaufruf %s(%s)", name, json.dumps(args, ensure_ascii=False)[:120]
                        )
                        try:
                            ergebnis = await asyncio.wait_for(ausfuehren(name, args), 30)
                        except Exception as e:  # noqa: BLE001 — der Aufrufer soll es vorlesen koennen
                            log.exception("Fernaufruf %s fehlgeschlagen", name)
                            ergebnis = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
                        writer.write(_antwort(200, {"ergebnis": ergebnis}))
            else:
                writer.write(
                    _antwort(404, {"message": "nur GET /werkzeuge und POST /werkzeuge/<name>"})
                )
            await writer.drain()
        except Exception:  # noqa: BLE001 — ein kaputter Aufruf darf nicht alles reissen
            log.exception("Fehler im Werkzeug-Dienst")
        finally:
            writer.close()

    return behandle


async def start_server(
    ausfuehren: Ausfuehren, *, host: str, port: int, token: str
) -> asyncio.AbstractServer:
    server = await asyncio.start_server(make_handler(ausfuehren, token=token), host, port)
    log.info(
        "Werkzeug-Dienst auf %s:%d (%d Werkzeuge, Token: %s)",
        host,
        port,
        len(FERN_WERKZEUGE),
        "ja" if token else "NEIN — Dienst nimmt nichts an",
    )
    return server
