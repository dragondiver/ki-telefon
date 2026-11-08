"""Wetter und News aus dem Vorrat: beim Anrufstart geholt, im Gespraech
sofort da.

17.08.: drei von vier Wetterabrufen auf dem Pi liefen in den Timeout —
mitten im Gespraech, mit dem ganzen _antwort_faellig-Tanz dahinter. Beim
Anrufstart (Freizeichen + Session-Aufbau) ist dagegen Luft. Vorschlag
Florian 19.08.: dort vorab fuellen. Fehler werden nie gecacht.
"""

import httpx
import pytest

from kitelefon.agents import news_fr, weather


@pytest.fixture(autouse=True)
def frischer_vorrat():
    weather.vorrat_leeren()
    news_fr.vorrat_leeren()
    yield
    weather.vorrat_leeren()
    news_fr.vorrat_leeren()


def _wetter_transport(zaehler: dict):
    def antwort(request: httpx.Request) -> httpx.Response:
        zaehler["n"] += 1
        if "geocoding" in str(request.url):
            return httpx.Response(
                200, json={"results": [{"name": "Rennes", "latitude": 48.1, "longitude": -1.7}]}
            )
        return httpx.Response(
            200,
            json={
                "daily": {
                    "time": ["2026-08-19", "2026-08-20"],
                    "weather_code": [3, 2],
                    "temperature_2m_max": [22.0, 20.0],
                    "temperature_2m_min": [14.0, 12.0],
                    "precipitation_probability_max": [10, 5],
                }
            },
        )

    return httpx.MockTransport(antwort)


async def test_zweiter_wetterabruf_kommt_aus_dem_vorrat():
    zaehler = {"n": 0}
    t = _wetter_transport(zaehler)
    erste = await weather.handle_get_weather({"ort": "Rennes"}, transport=t)
    assert erste.get("ok", True) is not False
    davor = zaehler["n"]
    zweite = await weather.handle_get_weather({"ort": "Rennes"}, transport=t)
    assert zweite == erste
    assert zaehler["n"] == davor  # kein weiterer HTTP-Verkehr


async def test_abgelaufener_vorrat_holt_frisch():
    zaehler = {"n": 0}
    t = _wetter_transport(zaehler)
    await weather.handle_get_weather({"ort": "Rennes"}, transport=t)
    davor = zaehler["n"]
    weather.vorrat_altern(sekunden=weather.WETTER_VORRAT_S + 1)
    await weather.handle_get_weather({"ort": "Rennes"}, transport=t)
    assert zaehler["n"] > davor


async def test_fehler_werden_nicht_gecacht():
    def kaputt(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("keine Verbindung")

    fehler = await weather.handle_get_weather(
        {"ort": "Rennes"}, transport=httpx.MockTransport(kaputt)
    )
    assert fehler["ok"] is False
    zaehler = {"n": 0}
    gut = await weather.handle_get_weather({"ort": "Rennes"}, transport=_wetter_transport(zaehler))
    assert gut.get("ok", True) is not False
    assert zaehler["n"] > 0  # der Fehler lag NICHT im Vorrat


async def test_vorrat_unterscheidet_ort_und_wann():
    zaehler = {"n": 0}
    t = _wetter_transport(zaehler)
    await weather.handle_get_weather({"ort": "Rennes", "wann": "heute"}, transport=t)
    davor = zaehler["n"]
    await weather.handle_get_weather({"ort": "Paris", "wann": "heute"}, transport=t)
    assert zaehler["n"] > davor  # anderer Ort = kein Treffer


async def test_mutierender_aufrufer_vergiftet_den_wetter_vorrat_nicht():
    """Kopie statt Alias (Finalreview Plan 4, Issue #30): siehe
    test_news_fr — dasselbe Schutzversprechen fuer das Wetter."""
    zaehler = {"n": 0}
    t = _wetter_transport(zaehler)
    erste = await weather.handle_get_weather({"ort": "Rennes"}, transport=t)
    for k in list(erste):
        erste[k] = "VERGIFTET"

    zweite = await weather.handle_get_weather({"ort": "Rennes"}, transport=t)
    assert zweite != erste
    assert "VERGIFTET" not in str(zweite)
