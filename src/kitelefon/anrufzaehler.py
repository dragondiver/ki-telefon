"""Anrufe pro Tag zaehlen und an Home Assistant melden (#69).

Nach jedem beendeten Gespraech setzt das Telefon den Sensor
`sensor.kitelefon_anrufe_heute` per REST (`POST /api/states/...`). Der
Zaehler lebt in einer kleinen JSON-Datei neben `tasks.db`, damit er einen
Dienst-Neustart ueberlebt; um Mitternacht (Europe/Berlin) beginnt er bei 0.

HA haelt per REST gesetzte Zustaende nur im Speicher; nach einem HA-Neustart
fehlt der Sensor. Deshalb meldet `takt()` den Stand zusaetzlich beim Start von
kitelefon und danach alle `TAKT_S` Sekunden erneut (#83). Nach Mitternacht
meldet der Takt 0 mit dem `last_reset` des neuen Tages, schreibt die Datei aber
nicht — das tut nur `gespraech_beendet()`.

Ein Fehler beim Melden darf den Anrufpfad nie stoeren: alles wird geloggt,
nichts geworfen; der naechste Takt holt eine fehlgeschlagene Meldung nach.
Ohne HA_URL/HA_TOKEN wird nur gezaehlt, nicht gemeldet, und kein Takt laeuft.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")
SENSOR = "sensor.kitelefon_anrufe_heute"
TIMEOUT_S = 3.0
TAKT_S = 300.0  # Stand alle 5 Minuten erneut melden (#83)


def _jetzt_berlin() -> datetime:
    return datetime.now(BERLIN)


class Anrufzaehler:
    def __init__(
        self,
        pfad: Path,
        *,
        ha_url: str | None,
        ha_token: str | None,
        uhr: Callable[[], datetime] = _jetzt_berlin,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
    ):
        self._pfad = pfad
        self._uhr = uhr
        self._transport = transport
        self._timeout_s = timeout_s
        self._ha_url = (ha_url or "").rstrip("/")
        self._ha_token = ha_token or ""
        if not self._ha_url or not self._ha_token:
            log.info(
                "HA_URL/HA_TOKEN nicht gesetzt — Anrufe werden gezaehlt, aber nicht an "
                "Home Assistant gemeldet (Datei: %s)",
                pfad,
            )

    @property
    def meldet(self) -> bool:
        """Ob HA_URL und HA_TOKEN gesetzt sind, also ueberhaupt gemeldet wird."""
        return bool(self._ha_url and self._ha_token)

    # --- Zaehler -----------------------------------------------------------

    def _heute(self) -> date:
        return self._uhr().astimezone(BERLIN).date()

    def _lesen(self) -> tuple[date | None, int]:
        try:
            daten = json.loads(self._pfad.read_text(encoding="utf-8"))
            return date.fromisoformat(daten["datum"]), int(daten["anzahl"])
        except FileNotFoundError:
            return None, 0
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Anrufzaehler-Datei %s unlesbar (%r) — beginne bei 0", self._pfad, exc)
            return None, 0

    def stand(self) -> int:
        """Anzahl Gespraeche des laufenden Tages, ohne etwas zu veraendern."""
        datum, anzahl = self._lesen()
        return anzahl if datum == self._heute() else 0

    def _schreiben(self, datum: date, anzahl: int) -> None:
        tmp = self._pfad.with_suffix(self._pfad.suffix + ".tmp")
        tmp.write_text(json.dumps({"datum": datum.isoformat(), "anzahl": anzahl}), encoding="utf-8")
        tmp.replace(self._pfad)

    async def gespraech_beendet(self) -> int:
        """Ein Gespraech ist zu Ende: hochzaehlen, sichern, melden.

        Gibt den neuen Tagesstand zurueck. Wirft nie — der Aufrufer sitzt im
        Auflege-Pfad des Telefons.
        """
        heute = self._heute()
        try:
            datum, anzahl = self._lesen()
            anzahl = anzahl + 1 if datum == heute else 1
            self._schreiben(heute, anzahl)
        except OSError as exc:
            log.warning("Anrufzaehler konnte %s nicht schreiben: %r", self._pfad, exc)
            return 0
        await self._melden(heute, anzahl)
        return anzahl

    # --- Takt (#83) ---------------------------------------------------------

    async def stand_melden(self) -> None:
        """Den aktuellen Tagesstand an HA melden, ohne etwas zu schreiben."""
        await self._melden(self._heute(), self.stand())

    async def takt(self, intervall_s: float = TAKT_S) -> None:
        """Meldet den Stand sofort und danach alle `intervall_s` Sekunden.

        Laeuft bis zum Abbruch (Task.cancel() beim Herunterfahren). Ohne
        HA_URL/HA_TOKEN kehrt er sofort zurueck. Wirft nie ausser
        CancelledError — ein einzelner Fehler kostet nur diesen Takt.
        """
        if not self.meldet:
            return
        while True:
            try:
                await self.stand_melden()
            except Exception as exc:
                log.warning("Anrufzaehler-Takt: %s nicht gemeldet: %r", SENSOR, exc)
            await asyncio.sleep(intervall_s)

    # --- Home Assistant ----------------------------------------------------

    async def _melden(self, heute: date, anzahl: int) -> None:
        if not self.meldet:
            return
        mitternacht = datetime(heute.year, heute.month, heute.day, tzinfo=BERLIN)
        body = {
            "state": str(anzahl),
            "attributes": {
                "friendly_name": "Anrufe heute",
                "state_class": "total",
                "last_reset": mitternacht.isoformat(),
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                antwort = await client.post(
                    f"{self._ha_url}/api/states/{SENSOR}",
                    json=body,
                    headers={"Authorization": f"Bearer {self._ha_token}"},
                )
        except httpx.HTTPError as exc:
            # %r: ein httpx-Timeout stringifiziert leer (siehe agents/weather.py).
            log.warning("Home Assistant nicht erreichbar, %s nicht gemeldet: %r", SENSOR, exc)
            return
        if antwort.status_code >= 400:
            log.warning(
                "Home Assistant lehnt %s ab: HTTP %d %s",
                SENSOR,
                antwort.status_code,
                antwort.text[:200],
            )
            return
        log.info("Home Assistant: %s = %d", SENSOR, anzahl)
