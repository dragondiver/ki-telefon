"""Wetter im Gespräch — reine Aufbereitung plus ein Verdrahtungstest.

Kein Test darf ins Netz. Die Aufbereitung ist deshalb als reine Funktion
gebaut, der Handler bekommt für den Verdrahtungstest einen MockTransport.
"""

import httpx
import pytest

from kitelefon.agents import weather
from kitelefon.agents.weather import (
    GET_WEATHER_TOOL_SCHEMA,
    WEATHER_TIMEOUT_S,
    baue_antwort,
    beschreibe_wetterlage,
    handle_get_weather,
)


@pytest.fixture(autouse=True)
def frischer_vorrat():
    weather.vorrat_leeren()
    yield
    weather.vorrat_leeren()


# --- Schema ---------------------------------------------------------------


def test_schema_shape():
    s = GET_WEATHER_TOOL_SCHEMA
    assert s["type"] == "function"
    assert s["name"] == "get_weather"
    props = s["parameters"]["properties"]
    assert set(props.keys()) == {"ort", "wann"}
    assert s["parameters"]["required"] == ["ort"]
    assert set(props["wann"]["enum"]) == {"jetzt", "heute", "morgen"}


# --- Wettercodes ----------------------------------------------------------


@pytest.mark.parametrize(
    "code,erwartet",
    [
        (0, "klar"),
        (3, "bedeckt"),
        (45, "neblig"),
        (61, "Regen"),
        (71, "Schnee"),
        (95, "Gewitter"),
    ],
)
def test_wettercodes_werden_uebersetzt(code, erwartet):
    assert erwartet.lower() in beschreibe_wetterlage(code).lower()


def test_unbekannter_wettercode_bleibt_aussprechbar():
    """Open-Meteo kann Codes ergänzen — die KI darf nie eine Zahl vorlesen."""
    text = beschreibe_wetterlage(999)
    assert text
    assert "999" not in text


# --- Aufbereitung ---------------------------------------------------------

GEO = {"name": "Hamburg", "country": "Deutschland", "latitude": 53.55, "longitude": 9.99}

FORECAST = {
    "current": {"temperature_2m": 12.4, "weather_code": 61, "wind_speed_10m": 18.0},
    "daily": {
        "time": ["2026-08-01", "2026-08-02"],
        "weather_code": [3, 95],
        "temperature_2m_max": [18.2, 22.7],
        "temperature_2m_min": [9.1, 13.4],
        "precipitation_probability_max": [20, 80],
    },
}


def test_antwort_jetzt_nennt_aktuelle_temperatur():
    a = baue_antwort(GEO, FORECAST, "jetzt")
    assert a["ok"] is True
    assert a["ort"] == "Hamburg"
    assert a["land"] == "Deutschland"
    assert a["wann"] == "jetzt"
    assert a["temperatur"] == 12.4
    assert "regen" in a["lage"].lower()


def test_antwort_heute_nennt_min_max_und_regenrisiko():
    a = baue_antwort(GEO, FORECAST, "heute")
    assert a["temperatur_max"] == 18.2
    assert a["temperatur_min"] == 9.1
    assert a["regenwahrscheinlichkeit"] == 20
    assert "bedeckt" in a["lage"].lower()


def test_antwort_morgen_nimmt_den_zweiten_tag():
    a = baue_antwort(GEO, FORECAST, "morgen")
    assert a["temperatur_max"] == 22.7
    assert a["regenwahrscheinlichkeit"] == 80
    assert "gewitter" in a["lage"].lower()


def test_antwort_nennt_immer_den_aufgeloesten_ort():
    """Bei mehrdeutigen Namen muss die KI sagen können, welchen Ort sie
    genommen hat, statt still den falschen zu raten."""
    geo = {"name": "Neustadt", "country": "Deutschland", "latitude": 1.0, "longitude": 2.0}
    for wann in ("jetzt", "heute", "morgen"):
        assert baue_antwort(geo, FORECAST, wann)["ort"] == "Neustadt"


def test_fehlende_vorhersagedaten_geben_fehler_statt_absturz():
    a = baue_antwort(GEO, {"daily": {"time": []}}, "morgen")
    assert a["ok"] is False
    assert a["error"]


# --- Handler --------------------------------------------------------------


async def test_handler_ohne_ort_fragt_zurueck():
    a = await handle_get_weather({})
    assert a["ok"] is False
    assert "ort" in a["error"].lower()


async def test_handler_unbekanntes_wann_faellt_auf_heute_zurueck():
    """Lieber heute beantworten als das Gespräch mit einem Fehler stören."""
    transport = httpx.MockTransport(_route)
    a = await handle_get_weather({"ort": "Hamburg", "wann": "übermorgen"}, transport=transport)
    assert a["ok"] is True
    assert a["wann"] == "heute"


def _route(request: httpx.Request) -> httpx.Response:
    if "geocoding" in request.url.host:
        return httpx.Response(200, json={"results": [GEO]})
    return httpx.Response(200, json=FORECAST)


async def test_handler_verdrahtet_geocoding_und_vorhersage():
    a = await handle_get_weather(
        {"ort": "Hamburg", "wann": "morgen"}, transport=httpx.MockTransport(_route)
    )
    assert a["ok"] is True
    assert a["ort"] == "Hamburg"
    assert a["temperatur_max"] == 22.7


async def test_handler_meldet_unbekannten_ort():
    def leer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    a = await handle_get_weather({"ort": "Xyzabc"}, transport=httpx.MockTransport(leer))
    assert a["ok"] is False
    assert "xyzabc" in a["error"].lower()


async def test_handler_meldet_netzfehler_statt_zu_erfinden():
    def kaputt(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("zu langsam")

    a = await handle_get_weather({"ort": "Hamburg"}, transport=httpx.MockTransport(kaputt))
    assert a["ok"] is False
    assert a["error"]


def test_timeout_ist_kurz_genug_fuers_gespraech():
    assert WEATHER_TIMEOUT_S <= 3.0
