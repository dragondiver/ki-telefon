import asyncio
import contextlib
import logging
import os
import sys
import termios
import time
import tty
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from dotenv import load_dotenv


class _RawTermHandler(logging.StreamHandler):
    """Logging handler that emits \r\n so lines start at column 0 in raw mode."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.stream.write(msg.replace("\n", "\r\n") + "\r\n")
            self.flush()
        except Exception:
            self.handleError(record)


from kitelefon.agents import themenurteil
from kitelefon.agents.erinnerung import destilliere
from kitelefon.anrufzaehler import Anrufzaehler
from kitelefon.audio import AudioIO
from kitelefon.bell import make_bell
from kitelefon.call_manager import CallManager
from kitelefon.config import load_settings
from kitelefon.dialer import make_dialer
from kitelefon.dialer.keyboard import KeyboardDialer
from kitelefon.haushalt import Haushalt
from kitelefon.hookswitch import make_hookswitch
from kitelefon.hookswitch.keyboard import KeyboardHookSwitch
from kitelefon.memory import RoleMemory
from kitelefon.realtime import RealtimeSession, SessionState, open_websocket
from kitelefon.roles import Role
from kitelefon.rueckruf import start_server as start_rueckruf_server
from kitelefon.sprachkurs_call import (
    SprachkursNichtVerfuegbar,
    spool_nachtragen,
    start_sprachkurs_call,
)
from kitelefon.tasks import RemoteTaskQueue, TaskQueue
from kitelefon.tools import make_sprachkurs_tool_handler
from kitelefon.werkzeugdienst import start_server as start_werkzeug_server

# Modulweiter Logger: _shared_stdin laeuft ausserhalb von _async_main, dessen
# lokales `log` hier nicht sichtbar ist — ein NameError im Leser wuerde ihn
# still sterben lassen und dabei den Rohmodus des Terminals zuruecksetzen.
log = logging.getLogger("kitelefon")

# Vorlauf des Vorrat-Prefetch (Wetter/News). Beim Anrufstart konkurriert
# der Verbindungsaufbau mit Realtime-Handshake, Audio-Start und dem
# Kontextblock aus Postgres — Sitzungen 9 und 10 (19./22.08.) endeten dort
# in ConnectTimeout, obwohl dieselbe API vom Pi in 0,1 s antwortet.
# Gebraucht wird der Vorrat erst beim Werkzeugaufruf im Gespraech.
VORRAT_VORLAUF_S = 8.0


async def _shared_stdin() -> tuple[asyncio.Queue[str], asyncio.Queue[str], asyncio.Task]:
    """Fan stdin chars out into two queues: hook (space) and dialer (digits).

    Puts the terminal in raw mode so each keypress is delivered immediately
    without waiting for Enter. Restores the original settings on exit.
    """
    hook_q: asyncio.Queue[str] = asyncio.Queue()
    dial_q: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setraw(fd)

    def _restore_terminal() -> None:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            pass

    async def reader() -> None:
        try:
            while True:
                ch = await loop.run_in_executor(None, sys.stdin.read, 1)
                if ch in ("", "\x03"):  # EOF or Ctrl+C
                    raise KeyboardInterrupt
                if ch == " ":
                    log.info("stdin: Leertaste -> Gabelschalter")
                    await hook_q.put(ch)
                elif ch.isdigit():
                    log.info("stdin: Ziffer %s -> Waehlscheibe", ch)
                    await dial_q.put(ch)
                else:
                    # Sichtbar machen, was die Tastatur WIRKLICH schickt —
                    # Escape-Sequenzen, Umlaut-Bytes etc. gehen sonst
                    # kommentarlos verloren.
                    log.info("stdin: verworfen %r", ch)
        except KeyboardInterrupt:
            raise
        except Exception:
            # Ein toter Leser hiess bisher: Tastatur reagiert nicht mehr,
            # ohne jede Meldung. Genau so blieb ein NameError hier
            # unsichtbar — das darf nie wieder lautlos passieren.
            log.exception("stdin-Leser gestorben — Tastatur ist ab jetzt taub")
            raise
        finally:
            _restore_terminal()

    reader_task = asyncio.create_task(reader())
    return hook_q, dial_q, reader_task


async def _queue_iter(q: asyncio.Queue[str]) -> AsyncIterator[str]:
    while True:
        yield await q.get()


def _gepufferte_aufgaben_nachspuelen(queue: Any) -> None:
    """Lokal (bei DB-Ausfall) gepufferte Aufgaben sofort nachschieben.

    RemoteTaskQueue.enqueue() spuelt den Puffer beilaeufig mit, aber nur,
    wenn ueberhaupt wieder eine neue Aufgabe eingereicht wird. Eine
    Sprachkurs-Auswertung, die waehrend eines DB-Ausfalls lokal gepuffert
    wurde, soll nicht erst auf den naechsten Anruf warten — beim Start ist
    die Verbindung ohnehin gerade erst geprueft worden (spool_nachtragen
    laeuft direkt davor), also gleich nachsehen. Nie werfen: ein kaputter
    Flush darf den Telefonstart nicht verhindern.
    """
    if not isinstance(queue, RemoteTaskQueue):
        return  # keine Remote-Queue konfiguriert, also auch kein Puffer
    try:
        queue._flush_fallback()
    except Exception:
        log.exception("Nachspuelen des lokalen Aufgaben-Puffers fehlgeschlagen")


def gedaechtnis_fuer(memory: RoleMemory | None, haushalt: Haushalt | None, rolle_name: str) -> str:
    """Gedaechtnis-Block einer Figur: Rollengedaechtnis plus Themenliste des Haushalts."""
    themen = ""
    if haushalt:
        try:
            themen = haushalt.themenzeile()
        except Exception:
            # Gedaechtnis ist nie Voraussetzung fuer ein Telefonat (kaputte DB, Platte voll).
            log.exception("Themenzeile des Haushalts nicht lesbar, Gespraech startet ohne")
    teile = [
        memory.prompt_block(rolle_name) if memory else "",
        themen,
    ]
    return "\n".join(t for t in teile if t)


class _Lazy:
    """Haelt die RealtimeSession, bis der Websocket-Aufbau fertig ist.

    CallManager ruft enable_mic()/begruessen()/send_mic_frame() schon auf,
    waehrend die Verbindung (transport_future) noch offen ist — ohne diese
    Huelle liefen die ins Leere.

    Frueher eine Closure in open_session() (unten in _async_main). Als
    Modul-Klasse mit expliziten Konstruktor-Argumenten ist sie direkt
    konstruierbar, siehe tests/test_main.py — genau das fehlte beim
    Finalreview 19.08.: _Lazy hatte weder eine begruessen()-Methode noch
    __getattr__-Weiterleitung, also griff der getattr-Schutz in
    CallManager._in_call (call_manager.py) und schluckte die Begruessung
    still, bei JEDEM produktiven Anruf (gewaehlt wie eingehend) — obwohl
    tests/test_begruessung.py komplett gruen war. Der Grund: jene Tests
    pruefen nur RealtimeSession.begruessen(), nie die Verdrahtung ueber
    diese Huelle, weil sie bis dahin gar nicht als eigenstaendiger
    Testgegenstand existierte.
    """

    def __init__(
        self,
        *,
        ist_sprachkurs: bool,
        transport_future: "asyncio.Future",
        role: Role,
        on_audio,
        tool_handler,
        settings,
        memory,
        sprachkurs_session_holder: dict,
        haushalt=None,
    ):
        self._ist_sprachkurs = ist_sprachkurs
        self._transport_future = transport_future
        self._role = role
        self._haushalt = haushalt
        self._on_audio = on_audio
        self._tool_handler = tool_handler
        self._settings = settings
        self._memory = memory
        self._sprachkurs_session_holder = sprachkurs_session_holder
        self._inner: RealtimeSession | None = None
        self._start_time: float | None = None
        self.sprachkurs_call = None  # von run() gesetzt (nur Rolle 3)
        self.uhr = None  # von CallManager._in_call gesetzt
        self.vorrat_task = None  # Referenz beim Anrufstart halten
        # begruessen() kann eintreffen, BEVOR _inner existiert:
        # _await_session_ready in call_manager.py (~5s Timeout) gibt bei
        # langsamem Postgres auf und laesst _in_call weiterlaufen, waehrend
        # run() den Websocket-/RealtimeSession-Aufbau noch nicht fertig hat.
        # Ohne dieses Merken verpufft der Begruessungs-Wunsch, weil noch
        # niemand da ist, der ihn entgegennimmt (Finalreview 19.08.).
        self._begruessung_pending = False

    @property
    def state(self):
        return self._inner.state if self._inner else SessionState.CONNECTING

    def enable_mic(self):
        if self._start_time is None:
            self._start_time = time.monotonic()
        if self._inner:
            self._inner.enable_mic()

    @property
    def verlauf(self):
        return self._inner.verlauf if self._inner else []

    @property
    def sprechzeiten(self):
        return self._inner.sprechzeiten if self._inner else (0.0, 0.0)

    @property
    def dauer_s(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    async def nachfassen(self, hinweis: str) -> None:
        if self._inner:
            await self._inner.nachfassen(hinweis)

    async def begruessen(self) -> None:
        """Reicht die Eroeffnung an RealtimeSession.begruessen() durch.

        Steht _inner schon, geht der Aufruf sofort durch. Steht er noch
        nicht (siehe Kommentar in __init__), wird er gemerkt und in run()
        nachgeholt, sobald _inner existiert — RealtimeSession selbst
        (_begruessung_ausstehend) uebernimmt danach das Timing gegen
        session.updated, das wird hier nicht dupliziert.
        """
        if self._inner:
            await self._inner.begruessen()
        else:
            self._begruessung_pending = True

    async def run(self):
        if self._ist_sprachkurs:
            # Platzhalter sofort raeumen, BEVOR der neue Aufbau
            # versucht wird: schlaegt er fehl (SprachkursNichtVerfuegbar),
            # darf _sprachkurs_abschliessen() fuer DIESEN Anruf nicht
            # noch die Session eines vorherigen, laengst beendeten
            # Anrufs vorfinden und dessen Kontext ein zweites Mal
            # (mit falschem Transkript) abschliessen.
            self._sprachkurs_session_holder.pop("session", None)
            try:
                call = await asyncio.to_thread(start_sprachkurs_call, self._settings)
            except SprachkursNichtVerfuegbar:
                log.error(
                    "Sprachkurs: weder Datenbank noch Schnappschuss "
                    "erreichbar — Anruf wird sauber beendet (Spec 7.6)"
                )
                self._transport_future.cancel()
                raise
            self.sprachkurs_call = call
            self._sprachkurs_session_holder["session"] = self

            # Wetter und News vorab holen, damit der Werkzeugaufruf im
            # Gespraech nicht auf das Netz wartet (17.08.: Timeouts mitten
            # im Dialog). Fire-and-forget: schlaegt es fehl, greift im
            # Gespraech der Live-Abruf wie bisher.
            #
            # Der Vorlauf ist Absicht: Sitzungen 9 und 10 (19./22.08.)
            # liefen beim Anrufstart in ConnectTimeout, waehrend dieselbe
            # API vom selben Pi in 0,1 s antwortete. Der Aufbau —
            # Realtime-Verbindung, Audio-Streams, Kontextblock aus
            # Postgres — laesst den Verbindungsaufbau nicht durch, bevor
            # der Timeout greift. Der Vorrat wird erst gebraucht, wenn
            # Camille das Werkzeug ruft; ein paar Sekunden spaeter ist
            # frueh genug.
            async def _vorrat_fuellen() -> None:
                await asyncio.sleep(VORRAT_VORLAUF_S)
                from kitelefon.agents.news_fr import get_news_fr
                from kitelefon.agents.weather import handle_get_weather

                for args in (
                    {"ort": "Rennes", "wann": "heute"},
                    {"ort": "Rennes", "wann": "jetzt"},
                ):
                    with contextlib.suppress(Exception):
                        await handle_get_weather(args)
                with contextlib.suppress(Exception):
                    await get_news_fr()

            # Referenz halten — siehe Kommentar beim reader_task.
            self.vorrat_task = asyncio.create_task(_vorrat_fuellen())

            transport = await self._transport_future
            self._inner = RealtimeSession(
                transport=transport,
                role=self._role,
                on_audio=self._on_audio,
                tool_handler=make_sprachkurs_tool_handler(call, fallback=self._tool_handler),
                model=self._settings.sprachkurs_model,
                gedaechtnis=call.instructions,
                vad_threshold=self._settings.vad_threshold,
                vad_prefix_padding_ms=self._settings.vad_prefix_padding_ms,
                vad_silence_ms=call.vad_silence_ms,
            )
        else:
            transport = await self._transport_future
            self._inner = RealtimeSession(
                transport=transport,
                role=self._role,
                on_audio=self._on_audio,
                tool_handler=self._tool_handler,
                model=self._role.get("model", self._settings.openai_model),
                gedaechtnis=gedaechtnis_fuer(self._memory, self._haushalt, self._role["name"]),
                vad_threshold=self._settings.vad_threshold,
                vad_prefix_padding_ms=self._settings.vad_prefix_padding_ms,
                vad_silence_ms=self._settings.vad_silence_ms,
                freisprechen=bool(self._settings.rundfunk_verleih_url),
                wohnort=self._settings.wohnort,
            )
        if self._begruessung_pending:
            # Nachholen, JETZT wo _inner existiert — siehe begruessen().
            self._begruessung_pending = False
            await self._inner.begruessen()
        await self._inner.run()

    async def close(self):
        if self._inner:
            await self._inner.close()
        else:
            self._transport_future.cancel()

    async def send_mic_frame(self, pcm: bytes):
        if self._inner:
            await self._inner.send_mic_frame(pcm)


async def _async_main() -> None:
    # sprachkurs (Rolle 3) liest SPRACHKURS_DATABASE_URL direkt aus
    # os.environ (db.py in dem Nachbar-Repo, nicht ueber unser Settings-
    # Objekt). Bisher stand die .env nur zufaellig schon geladen, weil
    # agents/spotify.py beim Import load_dotenv() aufruft — eine Falle,
    # sobald jemand den Spotify-Import entfernt oder Rolle 3 vor Spotify
    # gebraucht wird. Deshalb hier explizit und zuerst.
    load_dotenv()
    settings = load_settings()
    logging.root.setLevel(settings.log_level)

    if settings.platform == "mac":
        handler: logging.Handler = _RawTermHandler(sys.stderr)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.root.addHandler(handler)

    # Anhaengen, NICHT ueberschreiben. Bis 2026-08-05 stand hier mode="w":
    # jeder Neustart loeschte das Protokoll. Nach dem Firmenbesuch war
    # deshalb kein einziges Gespraech mehr nachvollziehbar — der Pi war
    # zwischendurch neu gestartet, und das Journal reichte auch nicht
    # zurueck. Rotation haelt die Datei trotzdem klein.
    file_handler = RotatingFileHandler(
        "kitelefon.log",
        mode="a",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.root.addHandler(file_handler)

    log = logging.getLogger("kitelefon")
    # Trennzeile, damit man beim Lesen sieht, wo ein Neustart lag.
    log.info("──────── Start ────────")
    log.info("Starting on platform=%s, log_level=%s", settings.platform, settings.log_level)

    reader_task: asyncio.Task | None = None
    if settings.platform == "mac":
        hook_q, dial_q, reader_task = await _shared_stdin()
        hookswitch = KeyboardHookSwitch(input_source=_queue_iter(hook_q))
        dialer = KeyboardDialer(input_source=_queue_iter(dial_q))
    else:
        hookswitch = make_hookswitch(settings.platform)
        dialer = make_dialer(settings.platform)

    audio = AudioIO()
    local_queue = TaskQueue(settings.db_path)
    if settings.tasks_database_url:
        queue = RemoteTaskQueue(settings.tasks_database_url, fallback=local_queue)
        log.info("Delegationen gehen an die Remote-Queue (lokaler Puffer bereit)")
    else:
        queue = local_queue
        log.info("Keine Remote-Queue konfiguriert — Delegationen bleiben lokal")

    async def _spool_beim_start() -> None:
        # Fire-and-forget: laeuft einmal im Hintergrund an, waehrend das
        # Telefon schon hochfaehrt. Ein kaputter Spool (z.B. Datenbank
        # weiterhin unerreichbar) darf den Telefonstart nicht verhindern —
        # jede Exception wird deshalb geloggt und geschluckt, nicht
        # weitergereicht.
        try:
            ergebnis = await asyncio.to_thread(spool_nachtragen, settings, queue)
            log.info("Sprachkurs-Spool nachgetragen: %s", ergebnis)
        except Exception:
            log.exception("Sprachkurs-Spool-Replay fehlgeschlagen")

        # Danach auch den lokalen Aufgaben-Puffer nachspuelen — siehe
        # _gepufferte_aufgaben_nachspuelen.
        await asyncio.to_thread(_gepufferte_aufgaben_nachspuelen, queue)

    # Referenz halten, nicht nur create_task() aufrufen: ein Task ohne
    # gehaltene Referenz kann laut asyncio-Doku vom Garbage Collector
    # mitten im Lauf eingesammelt werden — reader_task weiter unten wird
    # aus genau diesem Grund ebenfalls gehalten.
    spool_task = asyncio.create_task(_spool_beim_start())

    bell = make_bell(settings.platform) if settings.bell_enabled else None

    async def do_shutdown() -> None:
        log.info("Herunterfahren: %s", settings.shutdown_command)
        proc = await asyncio.create_subprocess_shell(settings.shutdown_command)
        rc = await proc.wait()
        if rc != 0:
            # Häufigste Ursache: sudo will ein Passwort. Ohne Meldung im Log
            # stünde man vor einem Telefon, das einfach nichts tut.
            log.error(
                "Herunterfahren fehlgeschlagen (Exit %d) — passwortloses sudo "
                "eingerichtet? Siehe README.",
                rc,
            )

    shutdown = do_shutdown if settings.shutdown_digit_enabled else None

    memory = RoleMemory(settings.db_path) if settings.role_memory else None
    haushalt = Haushalt(settings.db_path, urteil_client=themenurteil.get_client())

    # Anrufe pro Tag fuer Home Assistant (#69). HA_URL/HA_TOKEN kommen ohne
    # KITELEFON_-Praefix aus der Umgebung — auf kitelefon aus
    # /etc/pi-status.env, das der Dienst per EnvironmentFile mitliest.
    anrufzaehler = Anrufzaehler(
        settings.db_path.with_name("anrufe_heute.json"),
        ha_url=os.environ.get("HA_URL"),
        ha_token=os.environ.get("HA_TOKEN"),
    )
    # HA vergisst per REST gesetzte Zustaende bei jedem Neustart. Der Takt
    # meldet den Stand jetzt und dann alle 5 Minuten neu (#83). Referenz
    # halten, siehe spool_task; beendet wird er im finally unten.
    anrufzaehler_task = asyncio.create_task(anrufzaehler.takt()) if anrufzaehler.meldet else None

    # Haelt die _Lazy-Huelle der laufenden bzw. gerade beendeten
    # Sprachkurs-Sitzung fest, damit nach_dem_gespraech() (dessen Signatur
    # von CallManager vorgegeben ist: nur role + verlauf) an sprechzeiten,
    # sprachkurs_call und die Sitzungsuhr herankommt, ohne CallManagers
    # on_call_ended-Schnittstelle umzubauen. Es laeuft immer nur ein Anruf
    # gleichzeitig, ein einzelner Platzhalter genuegt.
    _sprachkurs_session_holder: dict[str, Any] = {}

    async def nach_dem_gespraech(role: Role, verlauf: list[tuple[str, str]]) -> None:
        """Nachbereitung nach dem Auflegen.

        Rolle 3 (Sprachkurs) hat nichts mit dem Gedaechtnis der Plauderrollen
        zu tun (Spec 4.3: die Fehlerliste kommt aus der Datenbank, nicht aus
        destillierten Fakten) — sie schliesst stattdessen die Sitzung ab und
        stoesst optional die Auswertung an.
        """
        if role.get("sprachkurs"):
            await _sprachkurs_abschliessen(verlauf)
            return
        if memory is None:
            return
        erinnerung = await destilliere(verlauf)
        if erinnerung is None:
            return
        memory.save(
            role["name"],
            fakten=erinnerung.fakten,
            zusammenfassung=erinnerung.zusammenfassung,
            jetzt=datetime.now(UTC),
        )
        log.info(
            "Gedächtnis für '%s': %d Fakten, '%s'",
            role["name"],
            len(erinnerung.fakten),
            erinnerung.zusammenfassung,
        )

    async def _sprachkurs_abschliessen(verlauf: list[tuple[str, str]]) -> None:
        # pop() statt get(): der Platzhalter wird sofort verbraucht. Ohne das
        # wuerde ein spaeterer Anruf, dessen eigener Aufbau fehlschlaegt (z.B.
        # SprachkursNichtVerfuegbar), sonst noch die Session DIESES Anrufs
        # hier vorfinden und dessen Transkript faelschlich der vorherigen,
        # laengst abgeschlossenen Sitzung zuordnen.
        session = _sprachkurs_session_holder.pop("session", None)
        call = getattr(session, "sprachkurs_call", None)
        if call is None:
            # Session nie richtig aufgebaut (z.B. SprachkursNichtVerfuegbar
            # waehrend des Aufbaus) — nichts zum Abschliessen da.
            log.warning("Sprachkurs: kein Sitzungskontext zum Abschliessen")
            return
        uhr = getattr(session, "uhr", None)
        ended_by = uhr.ended_by if uhr is not None else "hangup"
        anrufer_s, ki_s = getattr(session, "sprechzeiten", (0.0, 0.0))
        gesamt = anrufer_s + ki_s
        ratio = round(anrufer_s / gesamt, 3) if gesamt > 0 else None
        dauer_s = getattr(session, "dauer_s", gesamt)
        # Ein sofortiger Auflegen/Transport-Fehler kann verlauf == [] lassen
        # — trotzdem soll die Sitzung abgeschlossen (oder gespoolt) werden,
        # statt fuer immer offen zu bleiben. transcript=None statt "" macht
        # das im Datensatz sichtbar.
        transcript = "\n".join(f"{wer}: {text}" for wer, text in verlauf) if verlauf else None
        # call.abschliessen() greift synchron auf Postgres zu — mit
        # asyncio.to_thread() blockiert das nicht den Event-Loop, waehrend
        # noch andere Anrufe/Rueckrufe bearbeitet werden koennten.
        ergebnis = await asyncio.to_thread(
            call.abschliessen,
            duration_s=int(dauer_s),
            speech_ratio_learner=ratio,
            ended_by=ended_by,
            transcript=transcript,
        )
        if ergebnis.get("gespoolt"):
            log.info("Sprachkurs-Sitzung gespoolt: %s", ergebnis.get("spool"))
        else:
            log.info(
                "Sprachkurs-Sitzung #%s abgeschlossen (ended_by=%s)",
                ergebnis.get("session_id"),
                ended_by,
            )
        if settings.sprachkurs_enqueue and not ergebnis.get("gespoolt"):
            queue.enqueue(
                agent_type="sprachkurs_auswertung",
                description="Französisch-Sitzung auswerten",
                deliver_via="email",
                deliver_to=settings.sprachkurs_lernende.lower(),
                payload={"session_id": call.session_id},
                konsument="sprachkurs",
            )

    def open_session(role: Role, on_audio, tool_handler):
        # Lazily open the websocket inside an async helper. Rolle 3 nutzt
        # ihr eigenes (kleineres) Modell, siehe settings.sprachkurs_model —
        # das muss schon beim Verbindungsaufbau feststehen, denn das Modell
        # steht in der Websocket-URL.
        ist_sprachkurs = bool(role.get("sprachkurs"))
        default_model = settings.sprachkurs_model if ist_sprachkurs else settings.openai_model
        transport_future = asyncio.ensure_future(
            open_websocket(role.get("model", default_model), settings.openai_api_key)
        )
        return _Lazy(
            ist_sprachkurs=ist_sprachkurs,
            transport_future=transport_future,
            role=role,
            on_audio=on_audio,
            tool_handler=tool_handler,
            settings=settings,
            memory=memory,
            haushalt=haushalt,
            sprachkurs_session_holder=_sprachkurs_session_holder,
        )

    cm = CallManager(
        hookswitch=hookswitch,
        dialer=dialer,
        audio=audio,
        open_session=open_session,
        queue=queue,
        bell=bell,
        half_duplex=settings.half_duplex,
        record_calls=settings.record_calls,
        mise_en_place_url=settings.mise_en_place_url,
        mise_en_place_secret=settings.mise_en_place_secret,
        startup_ring=settings.startup_ring,
        autocall_delay_s=settings.autocall_delay_s,
        autocall_max_rings=settings.autocall_max_rings,
        shutdown=shutdown,
        shutdown_grace_s=settings.shutdown_grace_s,
        on_call_ended=nach_dem_gespraech,
        on_gespraech_beendet=anrufzaehler.gespraech_beendet,
        rundfunk_verleih_url=settings.rundfunk_verleih_url,
        rundfunk_agent_token=settings.rundfunk_agent_token,
        haushalt=haushalt,
        websuche_max_pro_tag=settings.websuche_max_pro_tag,
        wohnort=settings.wohnort,
    )

    rueckruf_server = None
    if settings.callback_port:
        # Ohne Token ist der Zuhoerer nur auf Loopback vertretbar — auf jedem
        # anderen Host (z.B. dem Default 0.0.0.0) koennte sonst jeder im
        # gleichen Netz das Telefon klingeln lassen und beliebigen Text in
        # beliebiger Stimme vorlesen lassen. Anders als beim Werkzeug-Dienst
        # bleibt der Token hier bewusst optional (siehe rueckruf.py), aber nur
        # solange der Host lokal bleibt.
        if not settings.callback_token and settings.callback_host not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            log.error(
                "Rueckruf-Zuhoerer nicht gestartet: KITELEFON_CALLBACK_TOKEN fehlt und "
                "callback_host (%s) ist nicht loopback — das waere offen fuer jeden im Netz.",
                settings.callback_host,
            )
        else:
            # Erst hier, nach dem CallManager: der Zuhoerer braucht dessen
            # Annahme-Methode. Faellt der Start fehl (Port belegt), soll das
            # Telefon trotzdem laufen — Rueckrufe landen dann beim Absender als
            # Mail, telefonieren kann man weiter.
            try:
                rueckruf_server = await start_rueckruf_server(
                    cm.nimm_rueckruf_an,
                    host=settings.callback_host,
                    port=settings.callback_port,
                    pfad=settings.callback_path,
                    token=settings.callback_token,
                )
            except OSError as e:
                log.error("Rueckruf-Zuhoerer nicht gestartet: %s", e)

    werkzeug_server = None
    if settings.werkzeug_port and settings.werkzeug_token:
        # Elektra (rundfunk) leiht sich die Werkzeuge des Telefons. Faellt der
        # Start fehl, telefoniert man trotzdem — Elektra kann dann nur ihre
        # eigenen Werkzeuge.
        try:
            werkzeug_server = await start_werkzeug_server(
                cm.werkzeug_ausfuehren,
                host=settings.werkzeug_host,
                port=settings.werkzeug_port,
                token=settings.werkzeug_token,
            )
        except OSError as e:
            log.error("Werkzeug-Dienst nicht gestartet: %s", e)

    if settings.platform == "mac":
        print(
            "KI-Telefon bereit. Leertaste = abheben/auflegen, 1–9 = Rolle wählen.", file=sys.stderr
        )
    else:
        print("KI-Telefon bereit (Pi). Hörer abheben + wählen.", file=sys.stderr)
    try:
        await cm.run()
    finally:
        if werkzeug_server is not None:
            werkzeug_server.close()
        if rueckruf_server is not None:
            rueckruf_server.close()
        if bell is not None:
            bell.close()
        if reader_task is not None:
            reader_task.cancel()
        spool_task.cancel()
        if anrufzaehler_task is not None:
            anrufzaehler_task.cancel()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        print("\nbye.", file=sys.stderr)


if __name__ == "__main__":
    main()
