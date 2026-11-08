"""Wohnort fuer "Wie ist das Wetter bei uns?" (#141).

Am 01.11.2026 fragte der Anrufer nach dem Wetter "bei uns im Conwestheim"
(verhoert); Hein suchte zweimal per web_suche und fand nichts. Seitdem:
KITELEFON_WOHNORT springt bei leerem Ort oder "hier"/"bei uns" ein, und der
Prompt schickt jede Wetterfrage an get_weather. Ohne Wohnort bleibt alles
wie vorher. Kein Test geht ins Netz.
"""

import httpx
import pytest
from test_call_manager import FakeAudio, FakeDialer, FakeHookSwitch

from kitelefon import call_manager as cm_modul
from kitelefon.agents import weather
from kitelefon.agents.weather import handle_get_weather, ort_aufloesen
from kitelefon.call_manager import CallManager
from kitelefon.config import Settings
from kitelefon.roles import ROLES, WETTER_SUFFIX, full_instructions
from kitelefon.tasks import TaskQueue

GEO = {"name": "Musterstadt", "country": "Deutschland", "latitude": 48.0, "longitude": 9.0}
FORECAST = {
    "daily": {
        "time": ["2026-11-01", "2026-11-02"],
        "weather_code": [3, 61],
        "temperature_2m_max": [12.0, 10.0],
        "temperature_2m_min": [5.0, 4.0],
        "precipitation_probability_max": [10, 70],
    }
}


@pytest.fixture(autouse=True)
def frischer_vorrat():
    weather.vorrat_leeren()
    yield
    weather.vorrat_leeren()


def _transport(gesucht: list[str]) -> httpx.MockTransport:
    def route(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            gesucht.append(request.url.params["name"])
            return httpx.Response(200, json={"results": [GEO]})
        return httpx.Response(200, json=FORECAST)

    return httpx.MockTransport(route)


# --- Aufloesung (rein) ----------------------------------------------------


@pytest.mark.parametrize(
    "ort",
    ["", "  ", "hier", "Hier", "bei uns", "Bei Uns", "zuhause", "Zuhause", "bei mir", "hier."],
)
def test_wohnort_springt_ein(ort):
    assert ort_aufloesen(ort, "Musterstadt") == "Musterstadt"


def test_genannter_ort_bleibt_trotz_wohnort():
    assert ort_aufloesen("Hamburg", "Musterstadt") == "Hamburg"


@pytest.mark.parametrize("ort", ["", "hier", "bei uns", "Hamburg"])
def test_ohne_wohnort_bleibt_der_ort_wie_er_ist(ort):
    assert ort_aufloesen(ort, "") == ort


# --- Handler --------------------------------------------------------------


async def test_handler_ohne_ort_nimmt_den_wohnort():
    gesucht: list[str] = []
    a = await handle_get_weather({}, transport=_transport(gesucht), wohnort="Musterstadt")
    assert a["ok"] is True
    assert a["ort"] == "Musterstadt"
    assert gesucht == ["Musterstadt"]


async def test_handler_bei_uns_nimmt_den_wohnort():
    gesucht: list[str] = []
    a = await handle_get_weather(
        {"ort": "bei uns", "wann": "morgen"}, transport=_transport(gesucht), wohnort="Musterstadt"
    )
    assert a["ok"] is True
    assert gesucht == ["Musterstadt"]


async def test_handler_ohne_wohnort_fragt_wie_bisher_zurueck():
    a = await handle_get_weather({})
    assert a["ok"] is False
    assert "nachfragen" in a["error"]


async def test_handler_ohne_wohnort_sucht_bei_uns_wie_bisher_woertlich():
    gesucht: list[str] = []
    await handle_get_weather({"ort": "bei uns"}, transport=_transport(gesucht))
    assert gesucht == ["bei uns"]


# --- Verdrahtung ----------------------------------------------------------


def test_einstellung_ist_standardmaessig_leer(monkeypatch):
    monkeypatch.delenv("KITELEFON_WOHNORT", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert Settings(_env_file=None).wohnort == ""


def test_einstellung_kommt_aus_der_umgebung(monkeypatch):
    monkeypatch.setenv("KITELEFON_WOHNORT", "Musterstadt")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert Settings(_env_file=None).wohnort == "Musterstadt"


@pytest.mark.parametrize("wohnort", ["", "Musterstadt"])
async def test_call_manager_reicht_den_wohnort_durch(tmp_path, monkeypatch, wohnort):
    gesehen = []

    async def fake(args, transport=None, *, wohnort=""):
        gesehen.append(wohnort)
        return {"ok": True}

    monkeypatch.setattr(cm_modul, "handle_get_weather", fake)
    cm = CallManager(
        hookswitch=FakeHookSwitch([], delay=0.0),
        dialer=FakeDialer(digit=1),
        audio=FakeAudio(),
        open_session=lambda role, on_audio, th: None,
        queue=TaskQueue(tmp_path / "tasks.db"),
        wohnort=wohnort,
    )
    await cm.werkzeug_ausfuehren("get_weather", {"ort": "hier"})
    assert gesehen == [wohnort]


# --- Prompt ---------------------------------------------------------------

_TELEFON_ROLLEN = [r for r in ROLES if not r.get("sprachkurs")]


@pytest.mark.parametrize("rolle", _TELEFON_ROLLEN, ids=lambda r: r["name"])
def test_wetter_geht_nie_ueber_web_suche(rolle):
    text = full_instructions(rolle)
    assert WETTER_SUFFIX.strip() in text
    assert "NIE mit web_suche" in text


@pytest.mark.parametrize("rolle", _TELEFON_ROLLEN, ids=lambda r: r["name"])
def test_wohnort_steht_im_prompt_nur_wenn_gesetzt(rolle):
    mit = full_instructions(rolle, wohnort="Musterstadt")
    ohne = full_instructions(rolle)
    assert 'ort="Musterstadt"' in mit
    assert '"Bei uns", "hier"' in mit
    assert "Musterstadt" not in ohne
    assert '"Bei uns", "hier"' not in ohne


def test_wohnort_nur_aus_leerzeichen_zaehlt_als_leer():
    assert full_instructions(ROLES[0], wohnort="  ") == full_instructions(ROLES[0])
