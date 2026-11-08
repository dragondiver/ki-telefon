"""Anrufzaehler fuer Home Assistant (#69).

Gefaktes HTTP (httpx.MockTransport) und injizierbare Uhr — kein Netz, keine
echte Zeit.
"""

import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from kitelefon.anrufzaehler import Anrufzaehler
from kitelefon.call_manager import CallManager
from kitelefon.hookswitch import HookEvent
from kitelefon.tasks import TaskQueue
from tests.test_call_manager import FakeAudio, FakeDialer, FakeHookSwitch, FakeSession

BERLIN = ZoneInfo("Europe/Berlin")
HA_URL = "http://ha.test:8123"


class Uhr:
    def __init__(self, jetzt: datetime):
        self.jetzt = jetzt

    def __call__(self) -> datetime:
        return self.jetzt


def _ha_fake(anfragen: list[httpx.Request], status: int = 200) -> httpx.MockTransport:
    def route(request: httpx.Request) -> httpx.Response:
        anfragen.append(request)
        return httpx.Response(status)

    return httpx.MockTransport(route)


def _zaehler(tmp_path, uhr, anfragen, **kw) -> Anrufzaehler:
    kw.setdefault("ha_url", HA_URL)
    kw.setdefault("ha_token", "geheim")
    kw.setdefault("transport", _ha_fake(anfragen))
    return Anrufzaehler(tmp_path / "anrufe_heute.json", uhr=uhr, **kw)


async def test_zwei_anrufe_am_selben_tag_zaehlen_eins_dann_zwei(tmp_path):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen)

    assert await z.gespraech_beendet() == 1
    uhr.jetzt = datetime(2026, 9, 18, 23, 59, tzinfo=BERLIN)
    assert await z.gespraech_beendet() == 2

    assert len(anfragen) == 2
    req = anfragen[-1]
    assert req.method == "POST"
    assert str(req.url) == f"{HA_URL}/api/states/sensor.kitelefon_anrufe_heute"
    assert req.headers["Authorization"] == "Bearer geheim"
    body = json.loads(req.content)
    assert body["state"] == "2"
    assert body["attributes"]["friendly_name"] == "Anrufe heute"
    assert body["attributes"]["state_class"] == "total"
    assert body["attributes"]["last_reset"] == "2026-09-18T00:00:00+02:00"


async def test_tageswechsel_beginnt_bei_eins(tmp_path):
    uhr = Uhr(datetime(2026, 9, 18, 23, 30, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen)
    await z.gespraech_beendet()
    await z.gespraech_beendet()

    uhr.jetzt = datetime(2026, 9, 19, 0, 5, tzinfo=BERLIN)
    assert await z.gespraech_beendet() == 1
    body = json.loads(anfragen[-1].content)
    assert body["state"] == "1"
    assert body["attributes"]["last_reset"] == "2026-09-19T00:00:00+02:00"


async def test_tageswechsel_nach_berlin_nicht_utc(tmp_path):
    # 23:30 UTC am 18.09. ist in Berlin schon der 19.09. (01:30 MESZ).
    uhr = Uhr(datetime(2026, 9, 18, 12, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen)
    await z.gespraech_beendet()

    uhr.jetzt = datetime(2026, 9, 18, 23, 30, tzinfo=ZoneInfo("UTC"))
    assert await z.gespraech_beendet() == 1


async def test_neustart_behaelt_den_stand(tmp_path):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z1 = _zaehler(tmp_path, uhr, anfragen)
    await z1.gespraech_beendet()
    await z1.gespraech_beendet()

    z2 = _zaehler(tmp_path, uhr, anfragen)  # neue Instanz, dieselbe Datei
    assert await z2.gespraech_beendet() == 3


async def test_kaputte_datei_beginnt_bei_null(tmp_path, caplog):
    pfad = tmp_path / "anrufe_heute.json"
    pfad.write_text("kein json")
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    z = _zaehler(tmp_path, uhr, [])
    assert await z.gespraech_beendet() == 1


async def test_ha_nicht_erreichbar_zaehlt_trotzdem_und_warnt(tmp_path, caplog):
    def kaputt(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("keine Route", request=request)

    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    z = _zaehler(tmp_path, uhr, [], transport=httpx.MockTransport(kaputt))
    with caplog.at_level(logging.WARNING, logger="kitelefon.anrufzaehler"):
        assert await z.gespraech_beendet() == 1
    assert any("Home Assistant" in r.message for r in caplog.records)
    assert all(r.levelno == logging.WARNING for r in caplog.records)
    # Der Zaehlerstand ist trotzdem gesichert.
    assert json.loads((tmp_path / "anrufe_heute.json").read_text())["anzahl"] == 1


async def test_ha_fehlerstatus_warnt(tmp_path, caplog):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen, transport=_ha_fake(anfragen, status=401))
    with caplog.at_level(logging.WARNING, logger="kitelefon.anrufzaehler"):
        await z.gespraech_beendet()
    assert any("401" in r.message for r in caplog.records)


async def test_ohne_ha_url_oder_token_kein_aufruf(tmp_path, caplog):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    with caplog.at_level(logging.INFO, logger="kitelefon.anrufzaehler"):
        z = _zaehler(tmp_path, uhr, anfragen, ha_url="", ha_token="")
    assert any("HA_URL" in r.message for r in caplog.records)
    assert await z.gespraech_beendet() == 1
    assert anfragen == []

    anfragen2: list[httpx.Request] = []
    z2 = _zaehler(tmp_path, uhr, anfragen2, ha_token="")
    await z2.gespraech_beendet()
    assert anfragen2 == []


async def test_zaehler_stand_ohne_neuen_anruf(tmp_path):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    z = _zaehler(tmp_path, uhr, [])
    assert z.stand() == 0
    await z.gespraech_beendet()
    assert z.stand() == 1
    uhr.jetzt = datetime(2026, 9, 19, 10, 0, tzinfo=BERLIN)
    assert z.stand() == 0


async def test_call_manager_meldet_nach_dem_auflegen(tmp_path):
    """Verdrahtung: ein gewaehlter Anruf, der aufgelegt wird, zaehlt."""
    gemeldet: list[int] = []

    async def bei_gespraechsende() -> None:
        gemeldet.append(1)

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=1),
        audio=FakeAudio(),
        open_session=lambda role, on_audio, tool_handler: FakeSession(),
        queue=TaskQueue(tmp_path / "tasks.db"),
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
        on_gespraech_beendet=bei_gespraechsende,
    )
    await cm.run()
    await asyncio.sleep(0)
    assert gemeldet == [1]


async def test_call_manager_zaehlt_nicht_ohne_gespraech(tmp_path):
    """Abheben und ohne Wahl wieder auflegen ist kein Gespraech."""
    gemeldet: list[int] = []

    async def bei_gespraechsende() -> None:
        gemeldet.append(1)

    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=None),
        audio=FakeAudio(),
        open_session=lambda role, on_audio, tool_handler: FakeSession(),
        queue=TaskQueue(tmp_path / "tasks.db"),
        dial_timeout_s=0.01,
        on_gespraech_beendet=bei_gespraechsende,
    )
    await cm.run()
    assert gemeldet == []


async def test_call_manager_fehler_im_zaehler_stoert_das_auflegen_nicht(tmp_path, caplog):
    async def kaputt() -> None:
        raise RuntimeError("boom")

    audio = FakeAudio()
    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.05),
        dialer=FakeDialer(digit=1),
        audio=audio,
        open_session=lambda role, on_audio, tool_handler: FakeSession(),
        queue=TaskQueue(tmp_path / "tasks.db"),
        ringback_repeats=1,
        ringback_tone_s=0.01,
        ringback_pause_s=0.01,
        dial_timeout_s=0.5,
        on_gespraech_beendet=kaputt,
    )
    with caplog.at_level(logging.WARNING, logger="kitelefon.call_manager"):
        await cm.run()
    assert audio.stopped
    assert any("Anrufzaehler" in r.message for r in caplog.records)


# --- Takt: Stand periodisch melden (#83) -----------------------------------
#
# HA haelt per REST gesetzte Zustaende nur im Speicher. Nach einem HA-Neustart
# fehlt der Sensor, bis jemand ihn wieder setzt — ohne Takt erst beim naechsten
# Anruf. Der Takt meldet deshalb beim Start und dann alle paar Minuten.


async def _takt_laufen_lassen(z: Anrufzaehler, bis) -> None:
    """Startet den Takt mit kurzem Intervall und wartet, bis `bis()` wahr ist."""
    task = asyncio.create_task(z.takt(intervall_s=0.01))
    try:
        for _ in range(500):
            if bis():
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_takt_meldet_stand_ohne_gespraech(tmp_path):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen)

    await _takt_laufen_lassen(z, lambda: len(anfragen) >= 2)

    # Einmal sofort beim Start, danach mindestens ein weiterer Takt.
    assert len(anfragen) >= 2
    for req in anfragen:
        assert str(req.url) == f"{HA_URL}/api/states/sensor.kitelefon_anrufe_heute"
        body = json.loads(req.content)
        assert body["state"] == "0"
        assert body["attributes"]["last_reset"] == "2026-09-18T00:00:00+02:00"
    # Nur gespraech_beendet schreibt die Datei.
    assert not (tmp_path / "anrufe_heute.json").exists()


async def test_takt_meldet_den_gespeicherten_stand(tmp_path):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen)
    await z.gespraech_beendet()
    await z.gespraech_beendet()
    anfragen.clear()

    await _takt_laufen_lassen(z, lambda: len(anfragen) >= 1)
    assert json.loads(anfragen[0].content)["state"] == "2"


async def test_takt_holt_fehlgeschlagene_meldung_nach(tmp_path, caplog):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    versuche: list[httpx.Request] = []
    erfolgreich: list[httpx.Request] = []

    def erst_kaputt(request: httpx.Request) -> httpx.Response:
        versuche.append(request)
        if len(versuche) == 1:
            raise httpx.ConnectError("HA startet gerade neu", request=request)
        erfolgreich.append(request)
        return httpx.Response(200)

    z = _zaehler(tmp_path, uhr, [], transport=httpx.MockTransport(erst_kaputt))
    with caplog.at_level(logging.WARNING, logger="kitelefon.anrufzaehler"):
        await _takt_laufen_lassen(z, lambda: len(erfolgreich) >= 1)

    assert len(erfolgreich) >= 1
    assert json.loads(erfolgreich[0].content)["state"] == "0"
    assert any("nicht erreichbar" in r.message for r in caplog.records)


async def test_takt_ueberlebt_unerwarteten_fehler(tmp_path, caplog, monkeypatch):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen)
    echt = z.stand
    aufrufe = {"n": 0}

    def einmal_kaputt() -> int:
        aufrufe["n"] += 1
        if aufrufe["n"] == 1:
            raise RuntimeError("boom")
        return echt()

    monkeypatch.setattr(z, "stand", einmal_kaputt)
    with caplog.at_level(logging.WARNING, logger="kitelefon.anrufzaehler"):
        await _takt_laufen_lassen(z, lambda: len(anfragen) >= 1)
    assert len(anfragen) >= 1
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_takt_nach_mitternacht_meldet_null_mit_neuem_last_reset(tmp_path):
    uhr = Uhr(datetime(2026, 9, 18, 23, 50, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen)
    await z.gespraech_beendet()
    await z.gespraech_beendet()
    datei_vorher = (tmp_path / "anrufe_heute.json").read_text()
    anfragen.clear()

    uhr.jetzt = datetime(2026, 9, 19, 0, 5, tzinfo=BERLIN)
    await _takt_laufen_lassen(z, lambda: len(anfragen) >= 1)

    body = json.loads(anfragen[0].content)
    assert body["state"] == "0"
    assert body["attributes"]["last_reset"] == "2026-09-19T00:00:00+02:00"
    # Der Takt schreibt nichts — die Datei steht noch auf dem Vortag.
    assert (tmp_path / "anrufe_heute.json").read_text() == datei_vorher


async def test_takt_ohne_ha_url_oder_token_laeuft_nicht(tmp_path):
    uhr = Uhr(datetime(2026, 9, 18, 10, 0, tzinfo=BERLIN))
    anfragen: list[httpx.Request] = []
    z = _zaehler(tmp_path, uhr, anfragen, ha_token="")
    assert z.meldet is False
    # Kehrt sofort zurueck statt endlos zu warten.
    await asyncio.wait_for(z.takt(intervall_s=0.01), timeout=1)
    assert anfragen == []
    assert _zaehler(tmp_path, uhr, []).meldet is True


def test_standardtakt_fuenf_minuten():
    from kitelefon.anrufzaehler import TAKT_S

    assert TAKT_S == 300
