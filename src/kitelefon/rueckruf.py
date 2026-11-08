"""HTTP-Zuhörer für Rückrufe des Heim-Agenten.

Wenn der Agent eine Aufgabe erledigt hat, deren `deliver_via` auf `callback`
steht, schickt sein Zusteller einen POST hierher. Das Telefon klingelt, und
beim Abheben liest eine Rolle das Ergebnis vor.

Bewusst ohne Web-Framework, wie der Rest des Projekts: es geht um genau eine
Route mit genau einer Methode. Ein Server dafür ist rund hundert Zeilen
stdlib — eine Abhängigkeit dafür wäre teurer als der Code.

Der Vertrag mit heim-agent (`deliver.py:_ring`):

    POST <pfad>
    X-Agent-Token: <geheim>            (optional, aber empfohlen)
    {"task_id": 12, "agent_type": "research", "description": "...",
     "result": "...", "role_name": "Gärtnerin"}

    202  angenommen, das Telefon klingelt gleich
    503  gerade im Gespräch — später nochmal
    401  Token fehlt oder stimmt nicht
    400  kein JSON oder kein Ergebnis darin
"""

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Ein Ergebnis, das laenger ist, wuerde am Telefon ohnehin niemand anhoeren —
# und es blaeht den Prompt auf. Der Rest steht in der Datenbank.
MAX_ERGEBNIS = 4000
MAX_BODY = 64 * 1024


@dataclass(frozen=True)
class Rueckruf:
    task_id: int
    beschreibung: str
    ergebnis: str
    # Welche Figur vorliest. Der Heim-Agent waehlt sie beim Abschliessen der
    # Aufgabe; leer heisst neutrale KI-Stimme.
    rolle: str = ""


# Nimmt den Rueckruf an. False heisst: gerade nicht moeglich (Gespraech laeuft).
Annahme = Callable[[Rueckruf], Awaitable[bool]] | Callable[[Rueckruf], bool]


class _Fehler(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


async def _lies_anfrage(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    kopfzeile = await reader.readline()
    if not kopfzeile:
        raise _Fehler(400, "leere Anfrage")
    try:
        methode, pfad, _ = kopfzeile.decode("latin-1").split(" ", 2)
    except ValueError:
        raise _Fehler(400, "unverstaendliche Anfragezeile") from None

    kopf: dict[str, str] = {}
    while True:
        zeile = await reader.readline()
        if zeile in (b"\r\n", b"\n", b""):
            break
        name, _, wert = zeile.decode("latin-1").partition(":")
        kopf[name.strip().lower()] = wert.strip()

    laenge = int(kopf.get("content-length", "0") or 0)
    if laenge > MAX_BODY:
        raise _Fehler(400, "Anfrage zu gross")
    koerper = await reader.readexactly(laenge) if laenge else b""
    return methode, pfad.split("?", 1)[0], kopf, koerper


def _pruefe(
    methode: str,
    pfad: str,
    kopf: dict[str, str],
    koerper: bytes,
    *,
    erwarteter_pfad: str,
    token: str,
) -> Rueckruf:
    if pfad != erwarteter_pfad:
        raise _Fehler(404, "unbekannter Pfad")
    if methode != "POST":
        raise _Fehler(405, "nur POST")
    if token:
        # compare_digest, damit die Laufzeit nichts ueber das Geheimnis verraet.
        if not hmac.compare_digest(kopf.get("x-agent-token", ""), token):
            raise _Fehler(401, "Token fehlt oder stimmt nicht")

    try:
        daten = json.loads(koerper.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _Fehler(400, "kein gueltiges JSON") from None
    if not isinstance(daten, dict):
        raise _Fehler(400, "JSON-Objekt erwartet")

    ergebnis = str(daten.get("result") or "").strip()
    if not ergebnis:
        # Ohne Ergebnis gaebe es nichts vorzulesen — dann lieber ein klarer
        # Fehler als ein Telefon, das grundlos klingelt.
        raise _Fehler(400, "kein Ergebnis im Rueckruf")

    return Rueckruf(
        task_id=int(daten.get("task_id") or 0),
        beschreibung=str(daten.get("description") or "").strip(),
        ergebnis=ergebnis[:MAX_ERGEBNIS],
        rolle=str(daten.get("role_name") or "").strip(),
    )


def _antwort(status: int, text: str) -> bytes:
    grund = {
        202: "Accepted",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        503: "Service Unavailable",
    }.get(status, "Error")
    koerper = json.dumps({"status": status, "message": text}, ensure_ascii=False).encode()
    kopf = [
        f"HTTP/1.1 {status} {grund}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(koerper)}",
        "Connection: close",
    ]
    if status == 503:
        # Sagt dem Zusteller, dass es sich lohnt, es gleich nochmal zu
        # versuchen — im Gegensatz zu 401 oder 400.
        kopf.append("Retry-After: 60")
    return ("\r\n".join(kopf) + "\r\n\r\n").encode("latin-1") + koerper


def make_handler(annahme: Annahme, *, pfad: str, token: str):
    async def behandle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                methode, angefragt, kopf, koerper = await _lies_anfrage(reader)
                rueckruf = _pruefe(
                    methode, angefragt, kopf, koerper, erwarteter_pfad=pfad, token=token
                )
            except _Fehler as e:
                log.warning("Rueckruf abgewiesen (%d): %s", e.status, e.text)
                writer.write(_antwort(e.status, e.text))
            except (asyncio.IncompleteReadError, ConnectionError) as e:
                log.warning("Rueckruf-Verbindung abgebrochen: %s", e)
            else:
                ergebnis = annahme(rueckruf)
                if asyncio.iscoroutine(ergebnis):
                    ergebnis = await ergebnis
                if ergebnis:
                    log.info("Rueckruf #%s angenommen", rueckruf.task_id)
                    writer.write(_antwort(202, "Telefon klingelt"))
                else:
                    log.info("Rueckruf #%s abgelehnt: gerade besetzt", rueckruf.task_id)
                    writer.write(_antwort(503, "gerade im Gespraech"))
            await writer.drain()
        except Exception:  # noqa: BLE001 — ein kaputter Aufruf darf nicht alles reissen
            log.exception("Fehler beim Rueckruf")
        finally:
            writer.close()

    return behandle


async def start_server(
    annahme: Annahme, *, host: str, port: int, pfad: str, token: str
) -> asyncio.AbstractServer:
    server = await asyncio.start_server(make_handler(annahme, pfad=pfad, token=token), host, port)
    log.info(
        "Rueckruf-Zuhoerer auf %s:%d%s (Token: %s)",
        host,
        port,
        pfad,
        "ja" if token else "NEIN — jeder im Netz kann anrufen lassen",
    )
    return server
