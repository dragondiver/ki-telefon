"""Wetterauskunft im laufenden Gespräch (Open-Meteo, ohne API-Key).

Wetter ist eine Live-Frage — sie wird im Anruf beantwortet und nicht an die
Agenten-Queue delegiert.

Zwei Aufrufe: erst Geocoding (Ortsname -> Koordinaten), dann die Vorhersage.
Zurück kommen **strukturierte Werte**, keine fertigen Sätze: Das Formulieren
übernimmt das Realtime-Modell, damit die Antwort in der Stimme der jeweiligen
Rolle klingt.

Netzwerk und Aufbereitung sind getrennt (`baue_antwort` ist rein), damit die
Tests ohne Netzzugriff auskommen.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Am Telefon darf nichts hängen: lieber ein ehrliches "hab ich nicht
# hinbekommen" als eine Pause, in der die KI verstummt.
# 2.0 war auf dem Pi zu knapp: der TLS-Handshake auf ARM braucht kalt
# gern >2 s, und am 17.08. scheiterten damit 3 von 4 Abfragen ("Wetterdienst
# gerade nicht erreichbar"), waehrend curl daneben mit 2,1 s durchkam.
# 3.0 ist die Obergrenze, die der Test bewusst erzwingt — mitten im
# Gespraech darf ein Werkzeug nicht laenger stocken.
WEATHER_TIMEOUT_S = 3.0

WANN_WERTE = ("jetzt", "heute", "morgen")

# "Wie ist das Wetter bei uns?" (#141): diese Angaben meinen den Wohnort aus
# KITELEFON_WOHNORT. Am 01.11. riet das Modell stattdessen einen verhoerten
# Ortsnamen ("Conwestheim"), den die Geokodierung nicht kennt. Ist kein
# Wohnort eingestellt, bleibt alles wie vorher.
WOHNORT_ANGABEN = frozenset({"hier", "bei uns", "zuhause", "zu hause", "bei mir"})


def ort_aufloesen(ort: str, wohnort: str) -> str:
    """Leeren Ort oder "hier"/"bei uns" auf den Wohnort abbilden. Rein."""
    ort = ort.strip()
    wohnort = wohnort.strip()
    if not wohnort:
        return ort
    if not ort or ort.lower().rstrip(".!?") in WOHNORT_ANGABEN:
        return wohnort
    return ort


# Ein gelungenes Ergebnis bleibt eine halbe Stunde gueltig — laenger als
# jede Sitzung (Sitzungsuhr: 21 min). Der Vorrat wird beim Anrufstart
# gefuellt (__main__), damit der Tool-Aufruf im Gespraech nicht auf
# ARM-TLS wartet (17.08.: drei von vier Abrufen im Timeout). Fehler
# landen NIE im Vorrat — der Prompt sagt Ausfaelle ehrlich an.
WETTER_VORRAT_S = 1800
_vorrat: dict[tuple[str, str], tuple[float, dict]] = {}


def vorrat_leeren() -> None:
    _vorrat.clear()


def vorrat_altern(sekunden: float) -> None:
    """Nur fuer Tests: alle Eintraege um `sekunden` aelter machen."""
    for k, (t, wert) in list(_vorrat.items()):
        _vorrat[k] = (t - sekunden, wert)


GET_WEATHER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "get_weather",
    "description": (
        "Frage das aktuelle Wetter oder die Vorhersage für einen Ort ab. "
        "Verwende dies, wenn der Nutzer nach dem Wetter, der Temperatur, "
        "Regen oder Sonne fragt. Antworte danach in eigenen Worten und nenne "
        "den zurückgegebenen Ortsnamen mit, damit klar ist, welcher Ort "
        "gemeint war. Delegiere Wetterfragen niemals — sie werden sofort "
        "hier beantwortet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ort": {
                "type": "string",
                "description": "Ortsname, z.B. 'Hamburg' oder 'Wagenfeld'.",
            },
            "wann": {
                "type": "string",
                "enum": list(WANN_WERTE),
                "description": "Zeitpunkt. Default 'heute'.",
                "default": "heute",
            },
        },
        "required": ["ort"],
    },
}

# WMO-Wettercodes, wie Open-Meteo sie liefert.
WETTERCODES: dict[int, str] = {
    0: "klar",
    1: "überwiegend klar",
    2: "teilweise bewölkt",
    3: "bedeckt",
    45: "neblig",
    48: "neblig mit Raureif",
    51: "leichter Nieselregen",
    53: "Nieselregen",
    55: "starker Nieselregen",
    56: "gefrierender Nieselregen",
    57: "starker gefrierender Nieselregen",
    61: "leichter Regen",
    63: "Regen",
    65: "starker Regen",
    66: "gefrierender Regen",
    67: "starker gefrierender Regen",
    71: "leichter Schnee",
    73: "Schnee",
    75: "starker Schnee",
    77: "Schneegriesel",
    80: "leichte Regenschauer",
    81: "Regenschauer",
    82: "kräftige Regenschauer",
    85: "leichte Schneeschauer",
    86: "Schneeschauer",
    95: "Gewitter",
    96: "Gewitter mit Hagel",
    99: "schweres Gewitter mit Hagel",
}


def beschreibe_wetterlage(code: int) -> str:
    """Wettercode als deutscher Text.

    Nie die nackte Zahl zurückgeben — die KI würde sie vorlesen. Open-Meteo
    kann Codes ergänzen, deshalb ein aussprechbarer Rückfall.
    """
    return WETTERCODES.get(code, "wechselhaft")


def baue_antwort(geo: dict, forecast: dict, wann: str) -> dict:
    """Roh-JSON zu den Werten, die die KI aussprechen soll. Reine Funktion."""
    basis = {
        "ok": True,
        "ort": geo.get("name", ""),
        "land": geo.get("country", ""),
        "wann": wann,
    }

    if wann == "jetzt":
        current = forecast.get("current") or {}
        if "temperature_2m" not in current:
            return {"ok": False, "error": "Keine aktuellen Messwerte verfügbar."}
        return {
            **basis,
            "temperatur": current["temperature_2m"],
            "wind_kmh": current.get("wind_speed_10m"),
            "lage": beschreibe_wetterlage(current.get("weather_code", -1)),
        }

    daily = forecast.get("daily") or {}
    index = 1 if wann == "morgen" else 0
    tage = daily.get("time") or []
    if len(tage) <= index:
        return {"ok": False, "error": "Keine Vorhersagedaten für diesen Tag."}

    def hole(feld: str) -> Any:
        werte = daily.get(feld) or []
        return werte[index] if len(werte) > index else None

    return {
        **basis,
        "datum": tage[index],
        "temperatur_max": hole("temperature_2m_max"),
        "temperatur_min": hole("temperature_2m_min"),
        "regenwahrscheinlichkeit": hole("precipitation_probability_max"),
        "lage": beschreibe_wetterlage(hole("weather_code") or -1),
    }


async def handle_get_weather(
    args: dict,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    wohnort: str = "",
) -> dict:
    """Tool-Handler. `transport` dient nur den Tests (MockTransport).

    `wohnort` (KITELEFON_WOHNORT) springt ein, wenn kein Ort oder "hier",
    "bei uns" usw. kommt — siehe `ort_aufloesen`.
    """
    ort = ort_aufloesen(args.get("ort") or "", wohnort)
    if not ort:
        return {"ok": False, "error": "Kein Ort angegeben — bitte nachfragen."}

    wann = args.get("wann") or "heute"
    if wann not in WANN_WERTE:
        # Lieber heute beantworten als das Gespräch mit einem Fehler stören.
        log.info("unbekanntes 'wann'=%r, nehme heute", wann)
        wann = "heute"

    schluessel = (ort.lower(), wann)
    eintrag = _vorrat.get(schluessel)
    if eintrag is not None:
        alter = time.monotonic() - eintrag[0]
        if alter < WETTER_VORRAT_S:
            log.debug("Wetter %s/%s aus dem Vorrat (%.0f s alt)", ort, wann, alter)
            # Kopie statt Alias: heute serialisieren alle Abnehmer nur,
            # aber ein kuenftiger Aufrufer, der das Ergebnis veraendert,
            # wuerde sonst den Vorrat fuer den Rest der 30 Minuten
            # vergiften (Finalreview Plan 4, 19.08.).
            return copy.deepcopy(eintrag[1])

    try:
        async with httpx.AsyncClient(timeout=WEATHER_TIMEOUT_S, transport=transport) as client:
            geo = await _suche_ort(client, ort)
            if geo is None:
                return {"ok": False, "error": f"Ort '{ort}' nicht gefunden."}
            forecast = await _hole_vorhersage(client, geo, wann)
    except httpx.HTTPError as exc:
        # %r statt %s: ein httpx-Timeout stringifiziert leer, und eine
        # Warnung ohne Grund hat uns am 17.08. die Fehlersuche gekostet.
        log.warning("Wetterabfrage fehlgeschlagen: %r", exc)
        return {"ok": False, "error": "Wetterdienst gerade nicht erreichbar."}

    antwort = baue_antwort(geo, forecast, wann)
    # Schnappschuss beim SCHREIBEN, nicht nur Kopie beim Lesen: der
    # erste Aufrufer haelt sonst genau das Objekt, das im Vorrat liegt,
    # und wuerde ihn beim Veraendern rueckwirkend vergiften.
    _vorrat[schluessel] = (time.monotonic(), copy.deepcopy(antwort))
    return antwort


async def _suche_ort(client: httpx.AsyncClient, ort: str) -> dict | None:
    r = await client.get(
        GEOCODING_URL, params={"name": ort, "count": 1, "language": "de", "format": "json"}
    )
    r.raise_for_status()
    treffer = (r.json() or {}).get("results") or []
    return treffer[0] if treffer else None


async def _hole_vorhersage(client: httpx.AsyncClient, geo: dict, wann: str) -> dict:
    params: dict[str, Any] = {
        "latitude": geo["latitude"],
        "longitude": geo["longitude"],
        "timezone": "auto",
    }
    if wann == "jetzt":
        params["current"] = "temperature_2m,weather_code,wind_speed_10m"
    else:
        params["daily"] = (
            "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        )
        params["forecast_days"] = 2
    r = await client.get(FORECAST_URL, params=params)
    r.raise_for_status()
    return r.json() or {}
