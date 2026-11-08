"""Ausloeser fuer das Freisprechen (#38, Bauabschnitt 3).

Zwei Wege ins Wohnzimmer: die Werkzeuge `freisprechen` / `hoerer_zurueck`
(das Modell ruft sie auf Zuruf des Anrufers) und die Ziffer 1 an der
Waehlscheibe waehrend des Gespraechs (Begleit-Task in _in_call). Beide
landen in audio_umschalten()/audio_zurueck() aus Bauabschnitt 2; hier geht
es nur um die Verdrahtung: Settings, Fehlersaetze, Idempotenz, und dass die
Waehlscheibe das Gespraech nie beenden kann.
"""

import asyncio
import logging

import pytest
from test_audio_umschalten import FakeBackend, GesteuerterHook, SessionMitMikrofon, _bis
from test_call_manager import FakeDialer, FakeHookSwitch

from kitelefon.call_manager import CallManager, CallState
from kitelefon.dialer.keyboard import KeyboardDialer
from kitelefon.hookswitch import HookEvent
from kitelefon.netz_audio import VerleihFehler
from kitelefon.tasks import TaskQueue

URL = "ws://elektra.test/verleih"
TOKEN = "geheim"


class ScheibenDialer:
    """Wie GpioDialer: Ziffern aus einer Queue, im Gespraech lesbar."""

    im_gespraech_lesbar = True

    def __init__(self, vor_dem_gespraech: int = 1):
        self._vor = vor_dem_gespraech
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.geleert = 0
        self.verworfen: list[int] = []

    async def read_digit(self, timeout: float) -> int | None:
        if self._vor is not None:
            digit, self._vor = self._vor, None
            return digit
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    def leeren(self) -> None:
        self.geleert += 1
        while not self.queue.empty():
            self.verworfen.append(self.queue.get_nowait())


class Rahmen:
    def __init__(self, tmp_path, *, dialer, url: str = URL, token: str = TOKEN, backend=None):
        self.hook = GesteuerterHook()
        self.hoerer = FakeBackend()
        self.dialer = dialer
        self.sessions: list[SessionMitMikrofon] = []
        self.factory_aufrufe: list[tuple[str, str]] = []
        self.netz = backend if backend is not None else FakeBackend(synchron=False)

        def open_session(role, on_audio, tool_handler):
            s = SessionMitMikrofon()
            self.sessions.append(s)
            return s

        def factory(u: str, t: str):
            self.factory_aufrufe.append((u, t))
            return self.netz

        self.cm = CallManager(
            hookswitch=self.hook,
            dialer=dialer,
            audio=self.hoerer,
            open_session=open_session,
            queue=TaskQueue(tmp_path / "tasks.db"),
            ringback_repeats=1,
            ringback_tone_s=0.01,
            ringback_pause_s=0.01,
            dial_timeout_s=0.5,
            half_duplex=False,
            rundfunk_verleih_url=url,
            rundfunk_agent_token=token,
            netz_audio_factory=factory,
        )

    async def anrufen(self) -> asyncio.Task:
        lauf = asyncio.create_task(self.cm.run())
        await _bis(lambda: self.cm.state is CallState.IN_CALL and self.cm._mikro_task is not None)
        return lauf

    async def auflegen(self, lauf: asyncio.Task) -> None:
        self.hook.auflegen()
        await asyncio.wait_for(lauf, 3.0)


# --- Werkzeuge ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_werkzeug_freisprechen_schaltet_auf_elektra(tmp_path):
    r = Rahmen(tmp_path, dialer=FakeDialer(digit=1))
    lauf = await r.anrufen()

    ergebnis = await r.cm._tool_handler("freisprechen", {})

    assert ergebnis["ok"] is True
    assert "Elektra" in ergebnis["hinweis"]
    assert r.factory_aufrufe == [(URL, TOKEN)], "Factory bekommt URL und Token aus den Settings"
    assert r.cm.freisprechen_aktiv
    assert r.netz.starts == 1
    assert r.hoerer.stops == 1
    assert r.cm.state is CallState.IN_CALL

    # Idempotent: ein zweiter Aufruf baut kein zweites Geraet.
    nochmal = await r.cm._tool_handler("freisprechen", {})
    assert nochmal["ok"] is True
    assert "schon über Elektra" in nochmal["hinweis"]
    assert r.factory_aufrufe == [(URL, TOKEN)]

    await r.auflegen(lauf)
    assert r.netz.stops == 1


@pytest.mark.asyncio
async def test_werkzeug_hoerer_zurueck(tmp_path):
    r = Rahmen(tmp_path, dialer=FakeDialer(digit=1))
    lauf = await r.anrufen()

    # Ohne Freisprechen: nichts zu tun, aber kein Fehler.
    vorher = await r.cm._tool_handler("hoerer_zurueck", {})
    assert vorher["ok"] is True
    assert "schon über den Hörer" in vorher["hinweis"]
    assert r.hoerer.stops == 0

    await r.cm._tool_handler("freisprechen", {})
    assert r.cm.freisprechen_aktiv
    zurueck = await r.cm._tool_handler("hoerer_zurueck", {})
    assert zurueck["ok"] is True
    assert not r.cm.freisprechen_aktiv
    assert r.netz.stops == 1
    assert r.hoerer.starts == 2

    await r.auflegen(lauf)


@pytest.mark.asyncio
async def test_besetzt_meldet_den_grund_und_laesst_den_hoerer_in_ruhe(tmp_path):
    netz = FakeBackend(synchron=False)
    netz.besetzt = "Radio läuft"
    r = Rahmen(tmp_path, dialer=FakeDialer(digit=1), backend=netz)
    lauf = await r.anrufen()

    ergebnis = await r.cm._tool_handler("freisprechen", {})

    assert ergebnis["ok"] is False
    assert ergebnis["error"] == "Elektra ist gerade beschäftigt (Radio läuft)."
    assert not r.cm.freisprechen_aktiv
    assert r.hoerer.stops == 0, "der Hoerer bleibt unangetastet"
    assert r.cm.state is CallState.IN_CALL
    await r.auflegen(lauf)


class UnerreichbaresBackend(FakeBackend):
    def __init__(self):
        super().__init__(synchron=False)

    def start_output(self):
        raise VerleihFehler("Verbindung abgelehnt")


@pytest.mark.asyncio
async def test_bridge_nicht_erreichbar(tmp_path):
    r = Rahmen(tmp_path, dialer=FakeDialer(digit=1), backend=UnerreichbaresBackend())
    lauf = await r.anrufen()

    ergebnis = await r.cm._tool_handler("freisprechen", {})

    assert ergebnis == {"ok": False, "error": "Elektra ist nicht erreichbar."}
    assert not r.cm.freisprechen_aktiv
    assert r.hoerer.stops == 0
    await r.auflegen(lauf)


@pytest.mark.asyncio
async def test_nicht_eingerichtet_ruft_die_factory_nicht(tmp_path):
    r = Rahmen(tmp_path, dialer=FakeDialer(digit=1), url="", token="")
    lauf = await r.anrufen()

    ergebnis = await r.cm._tool_handler("freisprechen", {})

    assert ergebnis == {"ok": False, "error": "Freisprechen ist nicht eingerichtet."}
    assert r.factory_aufrufe == []
    assert not r.cm.freisprechen_aktiv
    await r.auflegen(lauf)


@pytest.mark.asyncio
async def test_werkzeuge_ausserhalb_eines_gespraechs(tmp_path):
    r = Rahmen(tmp_path, dialer=FakeDialer(digit=1))
    assert (await r.cm._tool_handler("freisprechen", {}))["ok"] is False
    assert r.factory_aufrufe == []
    # hoerer_zurueck ist ohne Gespraech ein Nichts, kein Fehler.
    assert (await r.cm._tool_handler("hoerer_zurueck", {}))["ok"] is True
    # Der Werkzeug-Dienst (Elektra) darf beide nicht ausloesen.
    for name in ("freisprechen", "hoerer_zurueck"):
        fern = await r.cm.werkzeug_ausfuehren(name, {})
        assert fern["ok"] is False and "Gespräch" in fern["error"]


def test_default_factory_ist_netz_audio_io(tmp_path):
    """Ohne Test-Factory baut der CallManager ein echtes NetzAudioIO."""
    from kitelefon.netz_audio import NetzAudioIO

    cm = CallManager(
        hookswitch=GesteuerterHook(),
        dialer=FakeDialer(digit=1),
        audio=FakeBackend(),
        open_session=lambda *a: SessionMitMikrofon(),
        queue=TaskQueue(tmp_path / "tasks.db"),
    )
    assert cm._netz_audio_factory is NetzAudioIO
    assert not cm._rundfunk_verleih_url, "Vorgabe: nicht eingerichtet"


# --- Schemas, Prompt, Werkzeug-Dienst ----------------------------------------------


def test_schemas_im_werkzeugkasten_aber_nicht_im_fern_dienst():
    from kitelefon.roles import get_role
    from kitelefon.tools import ALL_TOOL_SCHEMAS
    from kitelefon.werkzeugdienst import FERN_WERKZEUGE

    namen = [s["name"] for s in ALL_TOOL_SCHEMAS]
    assert "freisprechen" in namen and "hoerer_zurueck" in namen
    for s in ALL_TOOL_SCHEMAS:
        if s["name"] in ("freisprechen", "hoerer_zurueck"):
            assert s["parameters"]["properties"] == {}, "keine Parameter"
    assert "freisprechen" not in FERN_WERKZEUGE
    assert "hoerer_zurueck" not in FERN_WERKZEUGE
    # Camille (Rolle 3) hat ihren eigenen, kleinen Werkzeugkasten (Spec 7.4).
    assert "freisprechen" not in {s["name"] for s in get_role(3)["tool_schemas"]}


def test_prompt_abschnitt_nur_wenn_eingerichtet():
    from kitelefon.roles import FREISPRECHEN_SUFFIX, ROLES, full_instructions

    assert "freisprechen" in FREISPRECHEN_SUFFIX and "hoerer_zurueck" in FREISPRECHEN_SUFFIX
    assert len(FREISPRECHEN_SUFFIX.strip().splitlines()) <= 9
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        ohne = full_instructions(r)
        mit = full_instructions(r, freisprechen=True)
        assert "Freisprechen" not in ohne, r["name"]
        assert FREISPRECHEN_SUFFIX.strip() in mit, r["name"]
        assert mit.index("## Rezepte") < mit.index("## Freisprechen")


@pytest.mark.asyncio
async def test_realtime_session_reicht_das_flag_durch():
    """RealtimeSession bekommt das Flag aus den Settings (__main__) und setzt es
    in session.update um — nur so kommt der Abschnitt beim Modell an."""
    import json

    from kitelefon.realtime import RealtimeSession
    from kitelefon.roles import get_role

    class Transport:
        def __init__(self):
            self.gesendet: list[dict] = []

        async def send(self, text: str) -> None:
            self.gesendet.append(json.loads(text))

        async def close(self) -> None:
            pass

    for flag in (False, True):
        t = Transport()
        s = RealtimeSession(
            transport=t,
            role=get_role(1),
            on_audio=lambda pcm: None,
            tool_handler=None,
            freisprechen=flag,
        )
        await s._send_session_update()
        anweisung = t.gesendet[-1]["session"]["instructions"]
        assert ("## Freisprechen" in anweisung) is flag


# --- Waehlscheibe im Gespraech ----------------------------------------------------


@pytest.mark.asyncio
async def test_ziffer_1_schaltet_um_und_wieder_zurueck(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="kitelefon.call_manager")
    scheibe = ScheibenDialer(vor_dem_gespraech=2)
    r = Rahmen(tmp_path, dialer=scheibe)
    lauf = await r.anrufen()
    assert r.cm._current_role["name"] != ""  # Rollenwahl vor dem Gespraech unveraendert
    await _bis(lambda: scheibe.geleert == 1)

    # Andere Ziffern: nichts passiert.
    for d in (0, 5, 9):
        scheibe.queue.put_nowait(d)
    await _bis(lambda: "Ziffer 9 ohne Bedeutung" in caplog.text)
    assert not r.cm.freisprechen_aktiv
    assert r.factory_aufrufe == []

    scheibe.queue.put_nowait(1)
    await _bis(lambda: r.cm.freisprechen_aktiv)
    assert r.factory_aufrufe == [(URL, TOKEN)]
    assert r.netz.starts == 1
    assert r.cm.state is CallState.IN_CALL
    assert not r.sessions[0].closed

    scheibe.queue.put_nowait(1)
    await _bis(lambda: not r.cm.freisprechen_aktiv)
    assert r.netz.stops == 1
    assert r.hoerer.starts == 2
    assert r.cm.state is CallState.IN_CALL

    await r.auflegen(lauf)
    assert r.sessions[0].closed


@pytest.mark.asyncio
async def test_liegengebliebene_ziffern_werden_beim_gespraechsbeginn_verworfen(tmp_path):
    scheibe = ScheibenDialer(vor_dem_gespraech=2)
    # Nachdrehen nach der Rollenwahl: eine 1 liegt schon in der Queue,
    # bevor das Gespraech beginnt.
    scheibe.queue.put_nowait(1)
    r = Rahmen(tmp_path, dialer=scheibe)
    lauf = await r.anrufen()
    await _bis(lambda: scheibe.geleert == 1)
    await asyncio.sleep(0.05)

    assert scheibe.verworfen == [1]
    assert not r.cm.freisprechen_aktiv
    assert r.factory_aufrufe == []
    await r.auflegen(lauf)


@pytest.mark.asyncio
async def test_fehler_beim_umschalten_beendet_das_gespraech_nicht(tmp_path, caplog):
    netz = FakeBackend(synchron=False)
    netz.besetzt = "Radio läuft"
    scheibe = ScheibenDialer()
    r = Rahmen(tmp_path, dialer=scheibe, backend=netz)
    lauf = await r.anrufen()
    await _bis(lambda: scheibe.geleert == 1)

    scheibe.queue.put_nowait(1)
    await _bis(lambda: "Radio läuft" in caplog.text)
    assert r.cm.state is CallState.IN_CALL
    assert not r.cm.freisprechen_aktiv
    assert r.hoerer.stops == 0

    # Ein unerwarteter Fehler aus dem Umschalten: geloggt, Gespraech laeuft.
    async def kaputt():
        raise RuntimeError("Kabelbruch")

    r.cm._freisprechen_umschalten = kaputt
    scheibe.queue.put_nowait(1)
    await _bis(lambda: "Kabelbruch" in caplog.text)
    assert r.cm.state is CallState.IN_CALL
    assert not r.sessions[0].closed

    # Der Begleit-Task lebt noch und reagiert weiter.
    r.hoerer.frames.put_nowait(b"weiter")
    await _bis(lambda: r.sessions[0].frames == [b"weiter"])
    await r.auflegen(lauf)


@pytest.mark.asyncio
async def test_begleit_task_endet_mit_dem_auflegen(tmp_path):
    scheibe = ScheibenDialer()
    r = Rahmen(tmp_path, dialer=scheibe)
    lauf = await r.anrufen()
    await _bis(lambda: scheibe.geleert == 1)
    scheibe_tasks = [
        t
        for t in asyncio.all_tasks()
        if t.get_coro().__qualname__.endswith("_waehlscheibe_im_gespraech")
    ]
    assert len(scheibe_tasks) == 1

    await r.auflegen(lauf)
    assert scheibe_tasks[0].cancelled()
    assert r.cm.state is CallState.IDLE
    # Nach dem Auflegen loest eine 1 nichts mehr aus.
    scheibe.queue.put_nowait(1)
    await asyncio.sleep(0.05)
    assert r.factory_aufrufe == []


@pytest.mark.asyncio
async def test_tastatur_schaltet_im_gespraech_und_ueberlebt_das_auflegen(tmp_path):
    """#74: der echte KeyboardDialer im Begleit-Task — Taste 1 schaltet um,
    Auflegen beendet den Task, und die Tastatur bleibt danach lesbar."""
    tasten: asyncio.Queue[str] = asyncio.Queue()

    async def strom():
        while True:
            yield await tasten.get()

    tastatur = KeyboardDialer(input_source=strom())
    tasten.put_nowait("1")  # Rollenwahl vor dem Gespraech
    r = Rahmen(tmp_path, dialer=tastatur)
    lauf = await r.anrufen()
    await _bis(
        lambda: any(
            t.get_coro().__qualname__.endswith("_waehlscheibe_im_gespraech")
            for t in asyncio.all_tasks()
        )
    )
    await asyncio.sleep(0.01)  # leeren() zu Beginn des Begleit-Tasks ist durch

    tasten.put_nowait("1")
    await _bis(lambda: r.cm.freisprechen_aktiv)

    await r.auflegen(lauf)
    assert r.cm.state is CallState.IDLE
    tasten.put_nowait("4")
    assert await tastatur.read_digit(timeout=1.0) == 4


@pytest.mark.asyncio
async def test_kein_begleit_task_fuer_dialer_ohne_flag(tmp_path):
    """Dialer ohne im_gespraech_lesbar: nur vor dem Gespraech lesen."""
    r = Rahmen(tmp_path, dialer=FakeDialer(digit=1))
    lauf = await r.anrufen()
    scheibe_tasks = [
        t
        for t in asyncio.all_tasks()
        if t.get_coro().__qualname__.endswith("_waehlscheibe_im_gespraech")
    ]
    assert scheibe_tasks == []
    await r.auflegen(lauf)


@pytest.mark.asyncio
async def test_ziffer_0_vor_dem_gespraech_faehrt_weiterhin_herunter(tmp_path):
    """Die Abschalt-Logik vor dem Gespraech bleibt, wie sie war."""
    heruntergefahren = asyncio.Event()

    async def shutdown():
        heruntergefahren.set()

    scheibe = ScheibenDialer(vor_dem_gespraech=0)
    cm = CallManager(
        hookswitch=FakeHookSwitch([HookEvent.LIFTED, HookEvent.HUNG_UP], delay=0.3),
        dialer=scheibe,
        audio=FakeBackend(),
        open_session=lambda *a: SessionMitMikrofon(),
        queue=TaskQueue(tmp_path / "tasks.db"),
        shutdown=shutdown,
        shutdown_grace_s=0.05,
        dial_timeout_s=0.5,
    )
    await asyncio.wait_for(cm.run(), 3.0)
    assert heruntergefahren.is_set()
    assert scheibe.geleert == 0, "ohne Gespraech kein Begleit-Task"
