import asyncio
import contextlib
import inspect
import logging
import random
import re
import time
import wave
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from kitelefon import ansagen
from kitelefon.agents.recipes import (
    handle_confirm_recipe,
    handle_discard_recipe,
    handle_modify_recipe,
    handle_suggest_recipe,
)
from kitelefon.agents.spotify import handle_spotify_now_playing, handle_spotify_play
from kitelefon.agents.weather import handle_get_weather
from kitelefon.agents.websuche import Tageszaehler, handle_web_suche
from kitelefon.audio import (
    ANLASS_BARGEIN,
    ANLASS_WAEHLTON_ENDE,
    FRAME_SAMPLES,
    RATE,
    AudioIO,
    pcm16_dial_tone_frame,
    pcm16_ringback,
    pcm16_shutdown_tone,
)
from kitelefon.bell import Bell
from kitelefon.dialer import Dialer
from kitelefon.haushalt import Haushalt
from kitelefon.hookswitch import HookEvent, HookSwitch
from kitelefon.netz_audio import GeraetBesetzt, NetzAudioIO, VerleihFehler
from kitelefon.roles import ROLES, Role, bote_rolle, get_role
from kitelefon.sprachkurs_call import SprachkursNichtVerfuegbar
from kitelefon.tasks import TaskSink
from kitelefon.tools import (
    handle_add_shopping_item,
    handle_delegate_task,
    handle_zustellung_aendern,
)

log = logging.getLogger(__name__)

# Sentinel: der Hookswitch-Eventstrom ist zu Ende. Die echten Implementierungen
# laufen endlos, aber Tests (und ein sauberes Herunterfahren) brauchen den Weg
# hinaus aus der Hauptschleife.
_HOOK_EOF = object()

# Wie lange ein Rueckruf auf eine Antwort wartet, bevor er einfach vorliest.
# Zehn Sekunden sind lang genug fuer ein "ja, gerne" und kurz genug, dass
# niemand glaubt, die Leitung sei tot.
NACHFASS_SEKUNDEN = 10
NACHFASS_VERSUCHE = 2

# Mikro-Wachhund (#20): ein lebender Input-Stream liefert auch bei Stille
# kontinuierlich Frames. Bleibt der Nachschub laenger als MIC_TOT_S aus,
# ist der Stream tot (Sitzung 9 am 19.08.: 89 s stumme Leitung) — dann
# neu oeffnen, hoechstens MIC_RESTART_VERSUCHE mal pro Anruf.
MIC_PRUEF_S = 3.0
MIC_TOT_S = 5.0
MIC_RESTART_VERSUCHE = 2
# Zweite Todesart (#41): Frames fliessen im Takt, aber es steht nichts
# drin. Sitzung 11 (05.09.) lief 62 s so — der Wachhund schwieg zu Recht,
# denn nach seiner damaligen Logik war alles in Ordnung. Grosszuegiger
# angesetzt als MIC_TOT_S: hier wird auf eine Eigenschaft des Raums
# gewartet (irgendein Geraeusch), nicht auf den Geraetetakt.
MIC_STILL_S = 20.0

# So viele Zeichen muss die Figur nach der Begruessung gesprochen haben,
# damit ein Rueckruf als angekommen gilt. Die Begruessung allein ist keine
# Zustellung — am 14.08. galt ein Anruf als zugestellt, bei dem ausser
# "Hast du 'nen Moment?" nichts gesagt wurde. Nur noch Rueckfall fuer
# Ergebnisse ohne eigene Woerter (z. B. nur Zahlen).
VORGELESEN_AB_ZEICHEN = 200

# Wie viel vom Ergebnis tatsaechlich vorkommen muss. Am 03.10. sprach die
# Figur bei Rueckruf #52 2545 Zeichen — erfunden, nichts davon aus dem
# Ergebnis — und galt trotzdem als zugestellt. Gezaehlt werden Woerter ab
# 5 Buchstaben; wer das Ergebnis nur zusammenfasst, trifft viele davon.
ERGEBNIS_WORT_MIN = 5
ERGEBNIS_TREFFER_MIN = 3
ERGEBNIS_ANTEIL_MIN = 0.2

# Freisprechen (#38): diese Ziffer an der Waehlscheibe schaltet im Gespraech
# zwischen Hoerer und Elektra um. Das Timeout ist nur der Takt, in dem der
# Begleit-Task aufwacht — er liest, solange das Gespraech laeuft.
FREISPRECHEN_ZIFFER = 1
WAEHLSCHEIBE_IM_GESPRAECH_S = 5.0


class CallState(str, Enum):
    IDLE = "idle"
    DIALING = "dialing"
    RINGBACK = "ringback"
    IN_CALL = "in_call"
    TEARDOWN = "teardown"


class SessionLike(Protocol):
    @property
    def state(self) -> Any: ...
    def enable_mic(self) -> None: ...
    async def run(self) -> None: ...
    async def close(self) -> None: ...
    async def send_mic_frame(self, pcm: bytes) -> None: ...
    # Zum Protokoll hinzugefuegt, damit Typechecker die naechste Drift wie
    # die vom Finalreview 19.08. fangen (_Lazy in __main__.py hatte kein
    # begruessen(), die echte Session bekam nie eine Eroeffnung zugerufen).
    # Der getattr-Schutz in _in_call bleibt trotzdem: Fakes in aelteren
    # Tests (und ggf. kuenftige SessionLike-Implementierungen) muessen die
    # Methode nicht zwingend haben, um zur Laufzeit weiter zu funktionieren.
    async def begruessen(self) -> None: ...


from collections.abc import Awaitable as _Awaitable

OpenSession = Callable[
    [Role, Callable[[bytes], None], Callable[[str, dict], _Awaitable[dict]]], SessionLike
]


class Sitzungsuhr:
    """Phasensteuerung des Sprachkurses (Spec 7.3).

    Ein Begleit-Task wie _dranbleiben: speist zu festen Zeitpunkten
    System-Events ueber session.nachfassen ein. Die Fehlerliste kommt aus
    der Datenbank, nicht aus dem Modellgedaechtnis — das ist der Kern von
    Spec 4.3.
    """

    PHASE_ACCURACY_MIN = 13
    SESSION_ENDING_MIN = 17
    HARTER_SCHLUSS_MIN = 21
    TALK_LESS_AB_MIN = 5
    TALK_LESS_SCHWELLE = 0.35

    # Fallback, wenn der Anruf-Kontext bei PHASE_ACCURACY noch nicht da ist
    # (langsames Postgres) — wortgleich mit SprachkursCall.offene_fehler_text()
    # ohne Eintraege, damit sich das Modell nicht anders verhaelt als bei
    # einer leeren, aber vorhandenen Fehlerliste.
    KEIN_CALL_FEHLERTEXT = "(Keine notierten Fehler — geh direkt zum Abschluss über.)"

    # Stille-Nachfrage (#22): Paula stand am 19.08. (Sitzung 9) vor einer
    # stummen Leitung und wusste nicht, ob das System noch lebt. Nach 15 s
    # Stille fragt Camille freundlich nach (Florian 19.08.: "15 sec
    # schweigen sind fuer Paula genug") — hoechstens zweimal, dann wird
    # das Schweigen akzeptiert (vielleicht denkt sie nur nach, Spec 4.2).
    # Erst wenn Paula wieder gesprochen hat, gibt es neues Nachfrage-Budget.
    STILLE_NACHFRAGE_S = 15.0
    STILLE_NACHFRAGE_MAX = 2

    def __init__(
        self,
        session,
        hangup_event: asyncio.Event,
        *,
        call=None,
        tick_s: float = 5.0,
        minute_s: float = 60.0,
        stille_nachfrage_s: float = STILLE_NACHFRAGE_S,
    ):
        self._session = session
        # `call` kommt haeufig noch nicht mit: start_sprachkurs_call() laeuft
        # in einem Hintergrund-Thread und _await_session_ready() gibt nach 5s
        # auf, egal ob die DB schon geantwortet hat. Die Uhr darf trotzdem
        # nie stehen bleiben — sie loest den Kontext bei jedem Tick lazy
        # nach (_aufloesen_call), statt sich beim Start darauf zu verlassen.
        self._call = call
        self._hangup = hangup_event
        self._tick_s = tick_s
        self._minute_s = minute_s
        self._stille_nachfrage_s = stille_nachfrage_s
        self.ended_by = "hangup"

    def _aufloesen_call(self) -> None:
        if self._call is None:
            self._call = getattr(self._session, "sprachkurs_call", None)

    def _pruefe_stille_nachfrage(
        self,
        zustand: str | None,
        jetzt: float,
        still_seit: float | None,
        nachfragen: int,
        phase_ereignis: bool,
    ) -> tuple[bool, float | None, int]:
        """Entscheidet über Stille-Nachfrage, gibt Nachfrage-Flag und neue Zustände zurück.

        Rueckgabe: (sollte_nachfragen, new_still_seit, new_nachfragen)

        Phase-Events haben Vorrang (17.08.-Bug: zwei overlapping response.create
        verursachen conversation_already_has_active_response). Bei
        phase_ereignis=True wird nicht nachgefragt und der Zustand bleibt
        unverändert — die Nachfrage kommt einfach einen Tick später.
        """
        if phase_ereignis:
            # Phase-Event hat Vorrang — keine Nachfrage, Zustand unverändert
            return False, still_seit, nachfragen

        if zustand == "user_speaking":
            # Paula lebt — neues Budget
            return False, None, 0
        elif zustand != "listening":
            # Camille spricht o.ä. — keine Stille
            return False, None, nachfragen
        else:
            # listening
            if still_seit is None:
                # Stille gerade angefangen
                return False, jetzt, nachfragen
            elif (
                jetzt - still_seit >= self._stille_nachfrage_s
                and nachfragen < self.STILLE_NACHFRAGE_MAX
            ):
                # Nachfrage fällig
                return True, jetzt, nachfragen + 1
            else:
                # Warten oder Budget aufgebraucht
                return False, still_seit, nachfragen

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        accuracy_gesendet = ending_gesendet = talk_less_gesendet = False
        still_seit: float | None = None
        nachfragen = 0
        while not self._hangup.is_set():
            await asyncio.sleep(self._tick_s)
            self._aufloesen_call()
            minuten = (loop.time() - start) / self._minute_s
            jetzt = loop.time()

            # Phase-Events blockieren Stille-Nachfrage (17.08.-Bug)
            phase_ereignis = False

            if (
                not talk_less_gesendet
                and minuten >= self.TALK_LESS_AB_MIN
                and not accuracy_gesendet
            ):
                anrufer, ki = self._session.sprechzeiten
                gesamt = anrufer + ki
                if gesamt > 0 and ki / gesamt > self.TALK_LESS_SCHWELLE:
                    talk_less_gesendet = True
                    phase_ereignis = True
                    log.info("Sitzungsuhr: TALK_LESS ausgeloest (KI-Anteil %.2f)", ki / gesamt)
                    await self._einspeisen(
                        "(TALK_LESS — Du redest zu viel. Ab jetzt deutlich "
                        "kürzere Beiträge, eine Frage pro Zug.)"
                    )

            if not accuracy_gesendet and minuten >= self.PHASE_ACCURACY_MIN:
                accuracy_gesendet = True
                phase_ereignis = True
                if self._call is not None:
                    # offene_fehler_text() greift synchron auf Postgres zu —
                    # das darf den Event-Loop nicht blockieren, waehrend die
                    # Sitzungsuhr im Hintergrund laeuft.
                    fehler = await asyncio.to_thread(self._call.offene_fehler_text)
                else:
                    fehler = self.KEIN_CALL_FEHLERTEXT
                    log.warning(
                        "Sitzungsuhr: Anruf-Kontext bei PHASE_ACCURACY immer "
                        "noch nicht da (langsame Datenbank?) — Fallback-Text, "
                        "keine Fehlerliste fuer diese Sitzung"
                    )
                # KEIN_CALL_FEHLERTEXT klammert sich schon selbst ein — mit
                # dem aeusseren Klammernpaar drumherum entstanden vorher
                # verschachtelte Klammern in der Ansage ("(...(...))").
                # Bei einer echten Fehlerliste (ohne eigene Klammern) bleibt
                # das aeussere Klammernpaar bestehen.
                if self._call is not None:
                    text = "(PHASE_ACCURACY — Wechsle jetzt in die Fehlerarbeit. " + fehler + ")"
                else:
                    text = "PHASE_ACCURACY — Wechsle jetzt in die Fehlerarbeit. " + fehler
                log.info("Sitzungsuhr: PHASE_ACCURACY ausgeloest")
                await self._einspeisen(text)

            if not ending_gesendet and minuten >= self.SESSION_ENDING_MIN:
                ending_gesendet = True
                phase_ereignis = True
                log.info("Sitzungsuhr: SESSION_ENDING ausgeloest")
                await self._einspeisen(
                    "(SESSION_ENDING — Komm zum Ritual-Abschluss: drei "
                    "Vokabeln, Mini-Aufgabe, Verabschiedung.)"
                )

            # Stille-Nachfrage (#22): nach langer Stille freundlich nachfassen
            # Phase-Events haben Vorrang — ohne sie könnte PHASE_ACCURACY und
            # Stille-Nachfrage beide in einem Tick feuern und zwei response.create
            # verschicken (17.08.-Bug: conversation_already_has_active_response).
            zustand = getattr(getattr(self._session, "state", None), "value", None)
            sollte_nachfragen, still_seit, nachfragen = self._pruefe_stille_nachfrage(
                zustand, jetzt, still_seit, nachfragen, phase_ereignis
            )
            if sollte_nachfragen:
                log.info(
                    "Sitzungsuhr: Stille-Nachfrage %d/%d", nachfragen, self.STILLE_NACHFRAGE_MAX
                )
                await self._einspeisen(
                    "(Die Lernende schweigt schon eine Weile. Frag kurz "
                    "und freundlich auf Französisch, ob sie noch da ist — "
                    "ein einziger Satz.)"
                )

            if minuten >= self.HARTER_SCHLUSS_MIN:
                # Fallback, sollte nie greifen (Spec 7.3): sauber beenden.
                self.ended_by = "timer"
                self._hangup.set()
                return

    async def _einspeisen(self, text: str) -> None:
        # Eine tote Session darf die Uhr nicht crashen — sie unterdrueckt
        # deshalb weiterhin jede Exception. Aber "still schlucken" verbirgt
        # genau die Faelle, die man bei der Hoerprobe verstehen will, also
        # jetzt mit log.warning statt bare suppress.
        try:
            await self._session.nachfassen(text)
        except Exception:
            log.warning("Sitzungsuhr: Einspeisen fehlgeschlagen", exc_info=True)


def _ergebnis_vorgelesen(ergebnis: str, gesagt: str) -> bool:
    """Kommt das Ergebnis in dem vor, was die Figur gesagt hat?

    Zeichen zu zaehlen reichte nicht: erfundener Text zaehlt genauso. Darum
    der Abgleich der Woerter. Ergebnisse ohne lange Woerter (nur Zahlen)
    fallen auf die Zeichenzahl zurueck.
    """
    erwartet = {w.lower() for w in re.findall(r"\w+", ergebnis) if len(w) >= ERGEBNIS_WORT_MIN}
    if not erwartet:
        return len(gesagt) >= VORGELESEN_AB_ZEICHEN
    gehoert = {w.lower() for w in re.findall(r"\w+", gesagt)}
    treffer = len(erwartet & gehoert)
    return treffer >= min(ERGEBNIS_TREFFER_MIN, len(erwartet)) and (
        treffer / len(erwartet) >= ERGEBNIS_ANTEIL_MIN
    )


class _Rueckrufmarke:
    """Umschlag, damit ein Rueckruf in der Hauptschleife nicht mit einem
    Hook-Event verwechselt wird — beide kommen durch dieselbe Tuer."""

    __slots__ = ("rueckruf",)

    def __init__(self, rueckruf: Any):
        self.rueckruf = rueckruf


class _AudioSenke:
    """on_audio der Sitzung: ruft _on_audio_from_session, traegt zusaetzlich .antwort (#113).

    RealtimeSession sucht per getattr nach ``antwort`` und meldet damit
    Antwortbeginn/-ende fuer die Wiedergabe-Messung — ohne dass sich die
    Signatur von open_session() aendert.
    """

    def __init__(self, cm: "CallManager"):
        self._cm = cm

    def __call__(self, pcm: bytes) -> None:
        self._cm._on_audio_from_session(pcm)

    def antwort(self, ereignis: str, **info: Any) -> None:
        self._cm._antwort_ereignis(ereignis, **info)


class CallManager:
    def __init__(
        self,
        *,
        hookswitch: HookSwitch,
        dialer: Dialer,
        audio: AudioIO,
        open_session: OpenSession,
        queue: TaskSink,
        bell: Bell | None = None,
        half_duplex: bool = True,
        record_calls: bool = False,
        mise_en_place_url: str = "",
        mise_en_place_secret: str = "",
        dial_timeout_s: float = 30.0,
        ringback_tone_s: float = 1.0,
        ringback_pause_s: float = 4.0,
        ringback_repeats: int = 2,
        startup_ring: bool = False,
        autocall_delay_s: float = 0.0,
        autocall_max_rings: int = 5,
        shutdown: Callable[[], _Awaitable[None]] | None = None,
        shutdown_grace_s: float = 5.0,
        on_call_ended: Callable[[Role, list[tuple[str, str]]], _Awaitable[None]] | None = None,
        on_gespraech_beendet: Callable[[], _Awaitable[None]] | None = None,
        rundfunk_verleih_url: str = "",
        rundfunk_agent_token: str = "",
        netz_audio_factory: Callable[[str, str], Any] = NetzAudioIO,
        haushalt: Haushalt | None = None,
        websuche_max_pro_tag: int = 50,
        wohnort: str = "",
        hoerer_frist_s: float = 10.0,
        hoerer_pruef_s: float = 1.0,
    ):
        # Hoerer-Wachhund (#86): seit wann der Pump-Task den Hoerer als
        # aufgelegt sieht (monotonic), None = abgehoben oder noch unbekannt.
        self._hoerer_frist_s = hoerer_frist_s
        self._hoerer_pruef_s = hoerer_pruef_s
        self._aufgelegt_seit: float | None = None
        self._on_call_ended = on_call_ended
        # Freisprechen (#38): leere URL = nicht eingerichtet, das Werkzeug
        # sagt das dann ehrlich. Die Factory ist nur fuer Tests austauschbar.
        self._rundfunk_verleih_url = rundfunk_verleih_url
        self._rundfunk_agent_token = rundfunk_agent_token
        self._netz_audio_factory = netz_audio_factory
        # Haushalts-Gedaechtnis und Websuche (#103): fehlt der Haushalt, antworten
        # die Gedaechtnis-Werkzeuge ehrlich statt zu schweigen.
        self._haushalt = haushalt
        self._websuche_zaehler = Tageszaehler(websuche_max_pro_tag)
        # Wohnort fuer "Wetter bei uns" (#141), leer = keiner.
        self._wohnort = wohnort
        # Anrufzaehler (#69): laeuft nach jedem Gespraech als eigener Task,
        # damit das Auflegen nicht auf Home Assistant wartet. Referenzen
        # halten, sonst darf der GC den Task mitten im POST einsammeln.
        self._on_gespraech_beendet = on_gespraech_beendet
        self._hintergrund: set[asyncio.Task] = set()
        self._current_role: Role | None = None
        self._hook = hookswitch
        self._dialer = dialer
        self._audio = audio
        # Freisprechen (#38): der Hoerer ist das Stamm-Audiogeraet. Waehrend
        # eines Gespraechs kann self._audio auf ein Netz-Backend (NetzAudioIO,
        # Elektra) umgeschaltet werden — audio_umschalten()/audio_zurueck().
        # Beim Auflegen landet alles wieder hier.
        self._hoerer_audio = audio
        self._audio_senke = _AudioSenke(self)
        self._session: SessionLike | None = None
        self._mikro_task: asyncio.Task | None = None
        self._wachhund_task: asyncio.Task | None = None
        self._bell = bell
        self._startup_ring = startup_ring
        self._autocall_delay_s = autocall_delay_s
        self._autocall_max_rings = autocall_max_rings
        self._shutdown = shutdown
        self._shutdown_grace_s = shutdown_grace_s
        self._open_session = open_session
        self._queue = queue
        self._half_duplex = half_duplex
        self._record_calls = record_calls
        self._mise_en_place_url = mise_en_place_url
        self._mise_en_place_secret = mise_en_place_secret
        self._dial_timeout_s = dial_timeout_s
        self._ringback_tone_s = ringback_tone_s
        self._ringback_pause_s = ringback_pause_s
        self._ringback_repeats = ringback_repeats
        self._state = CallState.IDLE
        self._hangup_event: asyncio.Event | None = None
        self._wav: wave.Wave_write | None = None
        # Rueckrufe kommen von aussen (HTTP), nicht aus dem Hoerer. Eine
        # Warteschlange mit Platz 1 genuegt: liegt schon einer an, ist das
        # Telefon fuer den naechsten belegt — der Zusteller versucht es
        # spaeter erneut, statt dass sich hier Anrufe stapeln.
        self._rueckruf_q: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        # Welche Aufgabe gerade vorgelesen wird. Nur waehrend eines
        # Rueckrufs gesetzt — daran haengt, ob 'ergebnis_per_mail' etwas
        # nachreichen kann.
        self._laufender_rueckruf: Any = None
        # Das Transkript des letzten Gespraechs — daran haengt, ob ein
        # Rueckruf als zugestellt gilt.
        self._letzter_verlauf: list[tuple[str, str]] = []

    def nimm_rueckruf_an(self, rueckruf: Any) -> bool:
        """Einen Rueckruf annehmen. False heisst: gerade nicht moeglich.

        Wird vom HTTP-Zuhoerer aufgerufen, also aus einer anderen Aufgabe
        heraus. Deshalb nur einreihen und sofort zurueckkehren — geklingelt
        wird in der Hauptschleife, wo auch alle anderen Anrufe entstehen.
        """
        if self._bell is None:
            log.warning("Rueckruf abgelehnt: keine Klingel konfiguriert")
            return False
        if self._state is not CallState.IDLE:
            return False
        try:
            self._rueckruf_q.put_nowait(rueckruf)
        except asyncio.QueueFull:
            return False
        return True

    @property
    def state(self) -> CallState:
        return self._state

    @property
    def freisprechen_aktiv(self) -> bool:
        """Laeuft das Audio gerade ueber Elektra statt ueber den Hoerer?"""
        return self._audio is not self._hoerer_audio

    def _transition(self, new: CallState) -> None:
        log.info("call state: %s -> %s", self._state.value, new.value)
        self._state = new

    async def run(self) -> None:
        # Hook-Events laufen über eine Queue statt direkt aus dem Iterator:
        # nur so lässt sich das Warten mit einem Timeout rennen lassen (für
        # den Auto-Anruf), ohne ein hängendes __anext__ abbrechen zu müssen.
        hook_q: asyncio.Queue[Any] = asyncio.Queue()
        pump_task = asyncio.create_task(self._pump_hook_events(hook_q))
        try:
            if self._startup_ring and self._bell is not None:
                log.info("Funktionstest: einmal klingeln")
                await self._bell.ring(1)

            _call_task: asyncio.Task | None = None
            while True:
                timeout = self._autocall_delay_s if self._autocall_armed() else None
                ev = await self._next_hook_event(hook_q, timeout)

                if isinstance(ev, _Rueckrufmarke):
                    await self._rueckruf(hook_q, ev.rueckruf)
                    continue

                if ev is _HOOK_EOF:
                    if _call_task is not None:
                        await _call_task
                    return

                if ev is None:  # Timeout abgelaufen -> die KI ruft an
                    await self._autocall(hook_q)
                    continue

                if ev is HookEvent.LIFTED and self._state is CallState.IDLE:
                    self._hangup_event = asyncio.Event()
                    _call_task = asyncio.create_task(self._run_call())
                elif ev is HookEvent.HUNG_UP and self._hangup_event is not None:
                    self._hangup_event.set()
                    if _call_task is not None:
                        await _call_task
                        _call_task = None
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
            # Eine noch laufende Meldung an Home Assistant zu Ende bringen
            # (hoechstens ihr Timeout), statt sie beim Stopp abzuwuergen.
            if self._hintergrund:
                await asyncio.gather(*self._hintergrund, return_exceptions=True)

    async def _pump_hook_events(self, hook_q: asyncio.Queue[Any]) -> None:
        async for ev in self._hook.events():
            # Der Stand wird hier gefuehrt, VOR der Queue: er haengt nicht
            # davon ab, ob die Hauptschleife das Event je abholt (#86).
            if ev is HookEvent.HUNG_UP:
                self._aufgelegt_seit = time.monotonic()
            elif ev is HookEvent.LIFTED:
                self._aufgelegt_seit = None
            await hook_q.put(ev)
        await hook_q.put(_HOOK_EOF)

    async def _hoerer_wachhund(self, hangup_event: asyncio.Event) -> None:
        """Beendet ein Gespraech, wenn der Hoerer laengst aufliegt (#86).

        Nur der Hoerer zaehlt, nie die Gespraechsdauer: ein aktives Gespraech
        mit abgehobenem Hoerer laeuft beliebig lange. Setzt dasselbe Event
        wie das normale Auflegen, das geordnete Teardown bleibt unveraendert.
        """
        while not hangup_event.is_set():
            await asyncio.sleep(self._hoerer_pruef_s)
            seit = self._aufgelegt_seit
            if seit is not None and time.monotonic() - seit >= self._hoerer_frist_s:
                log.warning(
                    "Hoerer seit %.0f s aufgelegt, Auflegen nicht verarbeitet "
                    "— beende das Gespraech (#86)",
                    time.monotonic() - seit,
                )
                hangup_event.set()
                return

    async def _next_hook_event(self, hook_q: asyncio.Queue[Any], timeout: float | None) -> Any:
        """Nächstes Ereignis: Hook-Event, Rückruf, None bei Timeout,
        `_HOOK_EOF` am Stream-Ende.

        Hörer und Rückruf laufen gleichberechtigt nebeneinander — wer zuerst
        kommt, gewinnt. Der jeweils andere Wartevorgang wird abgebrochen,
        ohne dass ihm ein Ereignis verlorengeht: `get()` hat dann noch nichts
        aus der Warteschlange genommen.
        """
        holer = {
            asyncio.ensure_future(hook_q.get()): "hook",
            asyncio.ensure_future(self._rueckruf_q.get()): "rueckruf",
        }
        try:
            fertig, offen = await asyncio.wait(
                holer, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            pass
        for task in offen:
            task.cancel()
        if not fertig:
            return None
        gewinner = next(iter(fertig))
        # Falls beide gleichzeitig fertig wurden, darf das zweite Ereignis
        # nicht verschwinden — zurueck in seine Warteschlange damit.
        for task in fertig:
            if task is gewinner:
                continue
            if holer[task] == "hook":
                hook_q.put_nowait(task.result())
            else:
                self._rueckruf_q.put_nowait(task.result())
        wert = gewinner.result()
        return _Rueckrufmarke(wert) if holer[gewinner] == "rueckruf" else wert

    def _autocall_armed(self) -> bool:
        return (
            self._bell is not None and self._autocall_delay_s > 0 and self._state is CallState.IDLE
        )

    async def _autocall(self, hook_q: asyncio.Queue[Any]) -> None:
        """Die KI ruft an: Telefon klingelt, beim Abheben startet eine Rolle.

        Anders als beim ausgehenden Anruf gibt es weder Freizeichen noch
        Rufton — der Anrufer sitzt ja auf der anderen Seite. Die Session
        wird während des Klingelns vorgewärmt, damit das Mikrofon beim
        Abheben sofort freigegeben werden kann.
        """
        assert self._bell is not None
        # Camille (Rolle 3) ruft nie von selbst an: sie wuerde die volle
        # Kursgrundlage bekommen, aber die falschen Werkzeuge — tool_schemas
        # kommt nur ueber get_role() zustande, hier waere sie nie gesetzt,
        # also ALL_TOOL_SCHEMAS (Spotify, Rezepte) in einer Franzoesisch-
        # stunde.
        role = random.choice([r for r in ROLES if not r.get("sprachkurs")])
        log.info("Auto-Anruf mit Rolle %s", role.get("name", "?"))
        await self._eingehender_anruf(hook_q, role)

    async def _rueckruf(self, hook_q: asyncio.Queue[Any], rueckruf: Any) -> None:
        """Der Agent meldet ein Ergebnis: klingeln und vorlesen lassen."""
        if self._bell is None:
            return
        log.info("Rueckruf zu Aufgabe #%s", getattr(rueckruf, "task_id", "?"))
        self._laufender_rueckruf = rueckruf
        try:
            await self._eingehender_anruf(
                hook_q,
                bote_rolle(
                    rueckruf.beschreibung,
                    rueckruf.ergebnis,
                    basis=getattr(rueckruf, "rolle", ""),
                ),
            )
        finally:
            self._melde_zustellung(rueckruf)
            self._laufender_rueckruf = None

    def _melde_zustellung(self, rueckruf: Any) -> None:
        """Nur ein wirklich vorgelesenes Ergebnis gilt als zugestellt.

        Der Zusteller im Heim-Agenten sieht nur sein 202 vom Abheben. Am
        14.08. galt damit ein Anruf als erledigt, in dem ausser "Hast du 'nen
        Moment?" nichts gesagt wurde — das Ergebnis war danach unerreichbar,
        weil kein zweiter Versuch mehr kam. Nur hier laesst sich sehen, ob
        der Text tatsaechlich herauskam.
        """
        task_id = getattr(rueckruf, "task_id", None)
        if task_id is None:
            return
        gesagt = [text for wer, text in self._letzter_verlauf if wer != "Anrufer"]
        # Die erste Aeusserung ist die Begruessung mit der Rueckfrage.
        nach_begruessung = " ".join(gesagt[1:])
        vorgelesen = len(nach_begruessung)
        if _ergebnis_vorgelesen(getattr(rueckruf, "ergebnis", ""), nach_begruessung):
            self._queue.als_zugestellt_melden(task_id)
            log.info("Rueckruf #%s: %d Zeichen vorgelesen — zugestellt", task_id, vorgelesen)
        else:
            log.warning(
                "Rueckruf #%s: nur %d Zeichen nach der Begruessung — NICHT "
                "als zugestellt gemeldet, der Zusteller versucht es erneut",
                task_id,
                vorgelesen,
            )

    async def _eingehender_anruf(self, hook_q: asyncio.Queue[Any], role: Role) -> None:
        """Das Telefon klingelt, beim Abheben laeuft `role`.

        Gemeinsamer Ablauf von Auto-Anruf und Rueckruf: kein Freizeichen,
        kein Rufton — der Anrufer sitzt auf dieser Seite.
        """
        assert self._bell is not None
        self._current_role = role

        self._transition(CallState.RINGBACK)
        self._open_recording(role)
        self._audio.start_output()

        answered = asyncio.Event()
        watcher = asyncio.create_task(self._watch_for_lift(hook_q, answered))
        try:
            picked_up = await self._bell.ring_until_answer(answered, self._autocall_max_rings)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

        if not picked_up:
            log.info("Eingehender Anruf: niemand abgehoben")
            await self._teardown()
            return

        # Die Sitzung entsteht ERST NACH dem Abheben — anders als beim
        # ausgehenden Anruf, wo das Freizeichen die Wartezeit fuellt.
        # Vorgewaermt begann das Modell schon waehrend des Klingelns zu
        # sprechen: sobald die Sitzung stand, ging ein response.create
        # raus, und beim Abheben war die Begruessung halb vorbei. Bei einem
        # Rueckruf waere das genau der Satz mit dem Ergebnis gewesen.
        # Der Aufbau dauert ein, zwei Sekunden — die braucht der Hoerer
        # ohnehin bis ans Ohr.
        session = self._open_session(role, self._audio_senke, self._tool_handler)
        session_run_task = asyncio.create_task(session.run())

        # Waehrend eines eingehenden Anrufs steckt die Hauptschleife hier
        # fest und liest keine Hoerer-Ereignisse mehr. Ohne eigenen Waechter
        # kaeme das Auflegen nirgends an: am 13.08. blieb ein Rueckruf
        # deshalb sieben Minuten im Gespraech haengen, bis der Dienst neu
        # startete — und jeder weitere Rueckruf prallte als "besetzt" ab.
        self._hangup_event = asyncio.Event()
        aufleger = asyncio.create_task(self._watch_for_hangup(hook_q))
        # Nur beim Rueckruf: dort gibt es etwas vorzulesen, und genau das
        # ist am 14.08. ausgefallen.
        dranbleiber = (
            asyncio.create_task(self._dranbleiben(session))
            if self._laufender_rueckruf is not None
            else None
        )
        try:
            await self._await_session_ready(session)
            await self._in_call(session, session_run_task)
        except Exception:
            log.exception("unhandled error in incoming call")
        finally:
            for t in (aufleger, dranbleiber):
                if t is None:
                    continue
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            await self._teardown()

    async def _dranbleiben(self, session: SessionLike) -> None:
        """Schweigt der Anrufer nach der Rueckfrage, wird trotzdem vorgelesen.

        Am 14.08. fragte Marlene "Hast du 'nen Moment?" und wartete dann 57
        Sekunden auf eine Antwort, die nie erkannt wurde — Hoerer am Ohr,
        niemand sagte etwas Verstaendliches, und das fertig recherchierte
        Ergebnis blieb ungehoert. Die Rueckfrage ist eine Hoeflichkeit, das
        Ergebnis ist der Zweck des Anrufs.
        """
        for versuch in range(NACHFASS_VERSUCHE):
            await asyncio.sleep(NACHFASS_SEKUNDEN)
            verlauf = list(getattr(session, "verlauf", []))
            if any(wer == "Anrufer" for wer, _ in verlauf):
                return  # es wird geredet, wir halten uns raus
            if getattr(session.state, "value", session.state) == "assistant_responding":
                continue  # sie spricht gerade, nicht dazwischenfunken
            log.info(
                "Rueckruf: keine Antwort nach %ds — lasse vorlesen (%d/%d)",
                NACHFASS_SEKUNDEN * (versuch + 1),
                versuch + 1,
                NACHFASS_VERSUCHE,
            )
            with contextlib.suppress(Exception):
                await session.nachfassen(
                    "(Der Anrufer hat nicht geantwortet, ist aber am Apparat. "
                    "Lies das Ergebnis jetzt vor, ohne noch einmal zu fragen.)"
                )

    async def _watch_for_hangup(self, hook_q: asyncio.Queue[Any]) -> None:
        """Auflegen waehrend eines eingehenden Anrufs an `_in_call` melden."""
        while True:
            ev = await hook_q.get()
            if ev is HookEvent.HUNG_UP or ev is _HOOK_EOF:
                if self._hangup_event is not None:
                    self._hangup_event.set()
                return
            # Ein zweites LIFTED waehrend des Gespraechs ist Prellen am
            # Gabelschalter — nichts, worauf zu reagieren waere.

    async def _watch_for_lift(self, hook_q: asyncio.Queue[Any], answered: asyncio.Event) -> None:
        while True:
            ev = await hook_q.get()
            if ev is _HOOK_EOF:
                await hook_q.put(_HOOK_EOF)  # für die Hauptschleife stehenlassen
                return
            if ev is HookEvent.LIFTED:
                answered.set()
                return

    async def _await_session_ready(self, session: SessionLike, timeout: float = 5.0) -> bool:
        """Wartet bis die Session steht — vorher greift enable_mic() ins Leere.

        Beim ausgehenden Anruf deckt der Rufton diese Zeit ab; beim
        Auto-Anruf kann jemand sofort nach dem ersten Schlag abheben.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            state = getattr(session.state, "value", session.state)
            if state != "connecting":
                return True
            await asyncio.sleep(0.05)
        log.warning("Session nach %.1fs noch nicht bereit — trotzdem weiter", timeout)
        return False

    async def _run_call(self) -> None:
        try:
            await self._handle_call()
        except Exception:
            log.exception("unhandled error in call")
        finally:
            await self._teardown()

    async def _handle_call(self) -> None:
        self._transition(CallState.DIALING)
        log.debug("starting audio output")
        self._audio.start_output()
        log.debug("audio output started")

        dial_tone_task = asyncio.create_task(self._play_dial_tone())
        log.debug("waiting for digit (timeout=%.1fs)", self._dial_timeout_s)

        # Das Warten auf die Ziffer muss gegen das Auflegen rennen — sonst
        # laeuft das Freizeichen bis zum Wähl-Timeout weiter (bis zu 30 s),
        # und weil die Hauptschleife solange keine Hook-Events liest, stauen
        # sich weitere Abheben/Auflegen dahinter auf.
        digit_task = asyncio.create_task(self._dialer.read_digit(self._dial_timeout_s))
        waiters: set[asyncio.Task] = {digit_task}
        hangup_task: asyncio.Task | None = None
        if self._hangup_event is not None:
            hangup_task = asyncio.create_task(self._hangup_event.wait())
            waiters.add(hangup_task)

        done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        dial_tone_task.cancel()
        await self._audio.clear_playback(anlass=ANLASS_WAEHLTON_ENDE)

        if hangup_task is not None and hangup_task in done:
            log.debug("hangup while dialing")
            return

        digit = digit_task.result()
        log.debug("digit received: %s", digit)

        if digit == 0 and self._shutdown is not None:
            await self._shutdown_sequence()
            return

        if digit is None or digit == 0 or not 1 <= digit <= 9:
            log.debug("no valid digit, waiting for hangup")
            # No valid selection — wait for hangup then bail.
            if self._hangup_event is not None:
                await self._hangup_event.wait()
            return

        # get_role() statt direktem Index: nur so bekommt Rolle 3 ihre
        # tool_schemas (Spec 7.4) — ohne die faellt Camille auf
        # ALL_TOOL_SCHEMAS zurueck und wuerde Werkzeuge sehen, die nicht zu
        # ihr passen.
        role = get_role(digit)
        log.info("selected role: %s", role["name"])
        await self._ringback_and_connect(role)

    async def _shutdown_sequence(self) -> None:
        """Ziffer 0: Abschaltton, Gnadenfrist, dann herunterfahren.

        Auflegen während der Frist bricht ab — die natürlichste Geste am
        Telefon, und die zweite Sicherung neben dem langen Zug der 0.
        """
        assert self._shutdown is not None
        log.info("Ziffer 0 — Herunterfahren in %.0fs, Auflegen bricht ab", self._shutdown_grace_s)
        await self._audio.play(pcm16_shutdown_tone())

        if self._hangup_event is not None:
            try:
                await asyncio.wait_for(self._hangup_event.wait(), timeout=self._shutdown_grace_s)
            except TimeoutError:
                pass
            else:
                log.info("aufgelegt — Herunterfahren abgebrochen")
                return
        else:
            await asyncio.sleep(self._shutdown_grace_s)

        log.info("fahre herunter")
        await self._shutdown()

    async def _ringback_and_connect(self, role: Role) -> None:
        self._current_role = role
        self._transition(CallState.RINGBACK)
        self._open_recording(role)

        # Pre-warm session in parallel with ringback.
        session = self._open_session(role, self._audio_senke, self._tool_handler)
        session_run_task = asyncio.create_task(session.run())

        ringback_pcm = pcm16_ringback(
            tone_s=self._ringback_tone_s,
            pause_s=self._ringback_pause_s,
            repeats=self._ringback_repeats,
        )
        await self._audio.play(ringback_pcm)

        total_s = (self._ringback_tone_s + self._ringback_pause_s) * self._ringback_repeats
        hangup_wait = asyncio.create_task(self._hangup_event.wait())  # type: ignore[union-attr]
        sleep_task = asyncio.create_task(asyncio.sleep(total_s))
        done, pending = await asyncio.wait(
            {hangup_wait, sleep_task, session_run_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if hangup_wait in done or session_run_task in done:
            for t in pending:
                t.cancel()
            if session_run_task in done and not session_run_task.cancelled():
                exc = session_run_task.exception()
                if exc:
                    log.error("session failed during ringback: %s", exc)
                    if isinstance(exc, SprachkursNichtVerfuegbar):
                        # Spec §7.6: gesprochene Ansage, danach sauber
                        # auflegen — kein Retry-Loop, keine Warteschleife.
                        # Fehlt das Asset, bleibt der frueher hier
                        # gespielte Ton als Rueckfallebene; ein Piepsen
                        # sagt weniger als ein Satz, aber mehr als Stille.
                        with contextlib.suppress(Exception):
                            ansage = ansagen.lade(ansagen.KURS_NICHT_ERREICHBAR)
                            await self._audio.play(ansage or pcm16_shutdown_tone())
            await self._close_session(session, session_run_task)
            return

        # Ringback finished — cancel only the timer tasks, keep session alive.
        hangup_wait.cancel()
        sleep_task.cancel()
        await self._in_call(session, session_run_task)

    @staticmethod
    async def _mikro_wachhund_lauf(
        audio,
        hangup: asyncio.Event,
        *,
        pruef_s: float = MIC_PRUEF_S,
        tot_s: float = MIC_TOT_S,
        still_s: float = MIC_STILL_S,
    ) -> None:
        """Begleit-Task wie die Sitzungsuhr: prueft Puls UND Inhalt.

        Zwei Todesarten, eine Behandlung: Der Stream liefert keine Frames
        mehr (tot_s), oder er liefert Frames ohne jeden Inhalt (still_s,
        #41). Fuer die Anruferin ist der Unterschied null — sie redet ins
        Leere.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        letzter_versuch = 0.0
        versuche = 0
        ansage_gespielt = False
        while not hangup.is_set():
            await asyncio.sleep(pruef_s)
            alter = audio.sekunden_seit_mic_frame()
            if alter is None:
                # Noch nie ein Frame — entweder seit Anrufbeginn (Sitzung
                # 4/5 am 17.08.: tot ab Beginn) oder seit dem letzten
                # Neustart, denn start_input() setzt den Zeitstempel
                # zurueck auf None.
                #
                # Gerechnet wird ab dem spaeteren der beiden Zeitpunkte.
                # Sonst zaehlt nach Neustart #1 die Zeit seit Wachhund-
                # Start weiter, und bei traegem Device-Open auf dem Pi
                # verbrennt das ganze Restart-Budget in zwei Zyklen, ohne
                # dem frisch geoeffneten Stream je tot_s Zeit zu geben.
                alter = loop.time() - max(start, letzter_versuch)
            if alter <= tot_s:
                # Puls da — aber kommt auch Inhalt an? Ein Geraet, das im
                # Takt Nullen liefert, besteht jede Frame-Zaehlung.
                still = audio.sekunden_seit_signal()
                if still is None:
                    # Noch nie Signal seit Anrufbeginn oder Neustart —
                    # dieselbe Rechnung wie oben beim fehlenden Puls.
                    still = loop.time() - max(start, letzter_versuch)
                if still <= still_s:
                    continue
                grund = f"{still:.0f} s ohne Signal (Frames kommen an)"
            else:
                grund = f"{alter:.1f} s kein Mikrofon-Frame"
            if versuche >= MIC_RESTART_VERSUCHE:
                # Aufgegeben — und es kommt immer noch kein Frame. Erst
                # jetzt darf die Ansage kommen: direkt nach dem letzten
                # Neustart wusste noch niemand, ob er greift, und eine
                # Ansage auf einem wieder lebenden Mikro waere schlimmer
                # als keine. Einmal, nicht bei jedem Pruefzyklus.
                if not ansage_gespielt:
                    ansage_gespielt = True
                    log.error(
                        "Mikro-Wachhund: %s, auch nach %d Neustarts — "
                        "Ansage an die Anruferin (#20, #41)",
                        grund,
                        versuche,
                    )
                    with contextlib.suppress(Exception):
                        ansage = ansagen.lade(ansagen.MIKRO_TOT)
                        if ansage:
                            await audio.play(ansage)
                continue
            versuche += 1
            log.warning(
                "Mikro-Wachhund: %s — Stream-Neustart %d/%d", grund, versuche, MIC_RESTART_VERSUCHE
            )
            try:
                # In den Thread ausgelagert: stop/close eines verklemmten
                # Streams plus Device-Open kosten auf dem Pi dreistellige
                # Millisekunden. Synchron im Event-Loop wuerde ein Neustart
                # mitten in Camilles Antwort hoerbar stottern.
                await asyncio.to_thread(audio.restart_input)
            except Exception:
                log.error("Mikro-Wachhund: Neustart fehlgeschlagen", exc_info=True)
            letzter_versuch = loop.time()
            if versuche == MIC_RESTART_VERSUCHE:
                log.error(
                    "Mikro-Wachhund: letzter Versuch — bleibt der Stream tot, "
                    "kommt beim naechsten Pruefzyklus die Ansage"
                )

    async def _in_call(self, session: SessionLike, session_run_task: asyncio.Task) -> None:
        self._transition(CallState.IN_CALL)
        # Do NOT clear playback here: the AI's greeting was queued behind the
        # ringback and must play through. Barge-in clears the buffer when needed.
        self._audio.start_input()
        session.enable_mic()

        # Jetzt — und erst jetzt — darf die Figur ihre Eroeffnung sprechen.
        # Vorher lief sie ins Freizeichen und wurde vom ersten Barge-in
        # abgeraeumt (Sitzung 8 am 18.08.: Paula hoerte nie ein Bonjour).
        # getattr: die Fakes in den CallManager-Tests und aeltere
        # SessionLike-Implementierungen kennen die Methode nicht.
        begruessen = getattr(session, "begruessen", None)
        if begruessen is not None:
            await begruessen()
        else:
            # Genau dieser Fall lief produktiv lange unbemerkt durch: _Lazy
            # in __main__.py hatte bis zum Finalreview 19.08. kein
            # begruessen(), der getattr-Schutz griff bei JEDEM Anruf still —
            # kein Log, kein Hinweis im Pi-Journal. Diese Zeile soll die
            # naechste Drift sofort sichtbar machen.
            log.warning(
                "Session ohne begruessen() — keine Eroeffnung gesprochen (type=%s)",
                type(session).__name__,
            )

        self._session = session
        self._starte_mikrofon_pumpe(session)
        hangup_task = asyncio.create_task(self._hangup_event.wait())  # type: ignore[union-attr]

        # Rolle 3 (Sprachkurs): die Sitzungsuhr laeuft wie _dranbleiben als
        # Begleit-Task neben dem Gespraech. _in_call ist der gemeinsame Ort
        # fuer den gewaehlten Anruf (_ringback_and_connect) UND den
        # eingehenden Anruf (_eingehender_anruf) — hier reicht ein Haken fuer
        # beide Pfade.
        #
        # Die Entscheidung, OB die Uhr laeuft, haengt an der ROLLE
        # (self._current_role, von beiden Aufrufern vor _in_call gesetzt),
        # nicht am Attribut 'sprachkurs_call' auf der Session: das wird erst
        # gesetzt, NACHDEM start_sprachkurs_call() aus dem Thread
        # zurueckkommt — bei langsamem Postgres kann _await_session_ready
        # (5s Timeout) laengst aufgegeben haben, waehrend das Attribut noch
        # fehlt. Ohne die Rollen-Pruefung liefe dann gar keine Uhr, ganz
        # ohne Logzeile. Der Anruf-Kontext selbst haengt trotzdem als
        # Attribut an der Session (von __main__.open_session gesetzt) —
        # Sitzungsuhr loest ihn lazy auf, siehe dort.
        uhr_task: asyncio.Task | None = None
        if self._current_role is not None and self._current_role.get("sprachkurs"):
            uhr = Sitzungsuhr(session, self._hangup_event)
            session.uhr = uhr  # nach_dem_gespraech liest hier ended_by
            uhr_task = asyncio.create_task(uhr.run())

        # Freisprechen (#38): die Waehlscheibe liest im Gespraech weiter,
        # Ziffer 1 schaltet um. Nur bei Dialern, die das aushalten
        # (im_gespraech_lesbar; GPIO und seit #74 auch die Tastatur).
        scheibe_task: asyncio.Task | None = None
        if getattr(self._dialer, "im_gespraech_lesbar", False):
            scheibe_task = asyncio.create_task(self._waehlscheibe_im_gespraech())

        hoerer_task = asyncio.create_task(
            self._hoerer_wachhund(self._hangup_event)  # type: ignore[arg-type]
        )

        try:
            while True:
                mikro_task = self._mikro_task
                assert mikro_task is not None
                done, _pending = await asyncio.wait(
                    {mikro_task, hangup_task, session_run_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if hangup_task in done or session_run_task in done:
                    break
                if mikro_task is not self._mikro_task:
                    # Backend gewechselt (audio_umschalten/audio_zurueck):
                    # die alte Pumpe ist abgeraeumt, die neue laeuft — mit
                    # ihr weiterwarten.
                    continue
                # Die Pumpe ist von selbst zu Ende. Beim Netz-Backend heisst
                # das: Geraet oder Verbindung weg (#38) — zurueck zum Hoerer,
                # das Gespraech laeuft weiter. Beim Hoerer ist es ein Fehler
                # im Mikrofonweg; der beendet das Gespraech wie bisher.
                if self._audio is not self._hoerer_audio:
                    log.warning("Netz-Audio verloren — zurueck zum Hoerer (#38)")
                    await self.audio_zurueck()
                    continue
                self._mikro_fehler_melden(mikro_task)
                break
            hangup_task.cancel()
            session_run_task.cancel()
        finally:
            for begleiter in (uhr_task, scheibe_task, hoerer_task):
                if begleiter is not None:
                    begleiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await begleiter
            await self._stoppe_mikrofon_pumpe()
            self._session = None
            # Hier — und nur hier — war es ein Gespraech: Mikro offen, Figur
            # hat gesprochen. Auflegen beim Freizeichen oder waehrend des
            # Ruftons kommt nie bis _in_call und zaehlt deshalb nicht.
            # Gewaehlter Anruf, Auto-Anruf und Rueckruf laufen alle hier
            # durch — alle drei zaehlen.
            self._gespraech_beendet_melden()
            await self._close_session(session, session_run_task)

    # --- Audio-Backend: Hoerer oder Netz (Freisprechen, #38) ---------------------
    def _starte_mikrofon_pumpe(self, session: SessionLike) -> None:
        """Mikrofon-Pumpe und Wachhund fuer das AKTUELLE self._audio starten.

        Beide Tasks haengen am Backend, nicht am Gespraech: wechselt das
        Backend, werden sie abgeraeumt und hier neu gestartet. Die Pumpe
        bindet ihr Audio-Objekt beim Start, damit eine alte Pumpe nie aus
        einem neuen Backend liest.
        """
        audio = self._audio

        async def pump_mic() -> None:
            async for frame in audio.mic_frames():
                if self._half_duplex and audio.is_playing:
                    continue
                await session.send_mic_frame(frame)

        self._mikro_task = asyncio.create_task(pump_mic())
        # Der Mikrofon-Tod (#20) trifft jede Rolle — der Wachhund laeuft
        # deshalb bei jedem Anruf mit, nicht nur im Sprachkurs.
        self._wachhund_task = asyncio.create_task(
            self._mikro_wachhund_lauf(audio, self._hangup_event)
        )

    def _pumpe_abloesen(self) -> list[asyncio.Task]:
        """Pumpe und Wachhund abbestellen, ohne zu warten — die Tasks kommen zurueck.

        Kein await hier drin: die Warteschleife in _in_call darf zwischen
        dem Abloesen und dem Start der neuen Pumpe nie ein leeres
        self._mikro_task sehen.
        """
        alte = [t for t in (self._mikro_task, self._wachhund_task) if t is not None]
        self._mikro_task = None
        self._wachhund_task = None
        for t in alte:
            t.cancel()
        return alte

    @staticmethod
    async def _abwarten(tasks: list[asyncio.Task]) -> None:
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def _stoppe_mikrofon_pumpe(self) -> None:
        await self._abwarten(self._pumpe_abloesen())

    @staticmethod
    def _mikro_fehler_melden(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Mikrofon-Pumpe abgebrochen", exc_info=exc)
        else:
            log.warning("Mikrofon-Pumpe ist zu Ende — Gespraech wird beendet")

    @staticmethod
    async def _evtl_warten(ergebnis: Any) -> None:
        """AudioIO ist synchron, NetzAudioIO redet uebers Netz und ist es nicht."""
        if inspect.isawaitable(ergebnis):
            await ergebnis

    async def audio_umschalten(self, neues: Any) -> None:
        """Audioweg im laufenden Gespraech auf ein anderes Backend legen.

        Reihenfolge: erst das neue Backend starten (kann scheitern, etwa
        GeraetBesetzt — dann bleibt alles wie es war), dann self._audio
        tauschen und Pumpe/Wachhund neu starten, zuletzt das alte stoppen.
        Die OpenAI-Sitzung merkt davon nichts: _on_audio_from_session liest
        self._audio bei jedem Paket.
        """
        if self._session is None:
            raise RuntimeError("audio_umschalten() nur waehrend eines Gespraechs")
        if neues is self._audio:
            return
        await self._evtl_warten(neues.start_output())
        await self._evtl_warten(neues.start_input())
        altes = self._audio
        alte_tasks = self._pumpe_abloesen()
        self._audio = neues
        self._starte_mikrofon_pumpe(self._session)
        await self._abwarten(alte_tasks)
        log.info("Audio umgeschaltet: %s -> %s", type(altes).__name__, type(neues).__name__)
        await self._audio_stoppen(altes)

    async def audio_zurueck(self) -> None:
        """Zurueck auf den Hoerer; ohne Wirkung, wenn er schon aktiv ist."""
        if self._audio is self._hoerer_audio:
            return
        if self._session is None:
            await self._netz_zurueckgeben()
            return
        await self.audio_umschalten(self._hoerer_audio)

    async def _netz_zurueckgeben(self) -> None:
        """Ausserhalb eines Gespraechs (Auflegen): Netz-Backend stoppen, Hoerer ist Stamm."""
        netz = self._audio
        self._audio = self._hoerer_audio
        if netz is not self._hoerer_audio:
            await self._audio_stoppen(netz)

    # --- Freisprechen: Ausloeser (#38, Bauabschnitt 3) ---------------------------
    async def _waehlscheibe_im_gespraech(self) -> None:
        """Begleit-Task: Ziffer 1 an der Waehlscheibe schaltet Freisprechen um.

        Alle anderen Ziffern werden ignoriert — die 0 faehrt nur VOR dem
        Gespraech herunter (_handle_call), hier hat sie keine Bedeutung.
        Ein Fehler beim Umschalten beendet nie das Gespraech.
        """
        # Was vor dem Gespraech noch in der Queue lag (Nachdrehen nach der
        # Rollenwahl, Waehlgeraeusch), darf nicht als Ziffer 1 durchgehen.
        self._dialer.leeren()
        while True:
            digit = await self._dialer.read_digit(timeout=WAEHLSCHEIBE_IM_GESPRAECH_S)
            if digit is None:
                continue
            if digit != FREISPRECHEN_ZIFFER:
                log.debug("Waehlscheibe im Gespraech: Ziffer %d ohne Bedeutung", digit)
                continue
            try:
                await self._freisprechen_umschalten()
            except Exception:
                log.warning("Waehlscheibe: Freisprechen umschalten fehlgeschlagen", exc_info=True)

    async def _freisprechen_umschalten(self) -> None:
        """Ziffer 1: hin zu Elektra, und beim zweiten Mal wieder zurueck."""
        if self.freisprechen_aktiv:
            log.info("Waehlscheibe: Ziffer 1 — zurueck zum Hoerer")
            await self.audio_zurueck()
            return
        log.info("Waehlscheibe: Ziffer 1 — Freisprechen ein")
        ergebnis = await self._handle_freisprechen()
        if not ergebnis.get("ok"):
            log.warning("Waehlscheibe: Freisprechen nicht moeglich — %s", ergebnis.get("error"))

    async def _handle_freisprechen(self) -> dict:
        """Werkzeug `freisprechen`: das Gespraech auf Elektra legen.

        Die Fehlersaetze sind fuer den Anrufer formuliert — das Modell soll
        sie sagen, nicht umschreiben (FREISPRECHEN_SUFFIX in roles.py).
        """
        if self._session is None:
            return {"ok": False, "error": "Das geht nur während eines Gesprächs."}
        if self.freisprechen_aktiv:
            return {"ok": True, "hinweis": "Das Gespräch läuft schon über Elektra."}
        if not self._rundfunk_verleih_url:
            return {"ok": False, "error": "Freisprechen ist nicht eingerichtet."}
        netz = self._netz_audio_factory(self._rundfunk_verleih_url, self._rundfunk_agent_token)
        try:
            await self.audio_umschalten(netz)
        except GeraetBesetzt as exc:
            log.info("Freisprechen: Elektra besetzt (%s)", exc.grund)
            return {"ok": False, "error": f"Elektra ist gerade beschäftigt ({exc.grund})."}
        except VerleihFehler:
            log.warning("Freisprechen: Elektra nicht erreichbar", exc_info=True)
            return {"ok": False, "error": "Elektra ist nicht erreichbar."}
        return {
            "ok": True,
            "hinweis": "Das Gespräch läuft jetzt über Elektra. Sag das kurz und sprich "
            "dann normal weiter.",
        }

    async def _handle_hoerer_zurueck(self) -> dict:
        """Werkzeug `hoerer_zurueck`: zurueck in den Hoerer."""
        if not self.freisprechen_aktiv:
            return {"ok": True, "hinweis": "Das Gespräch läuft schon über den Hörer."}
        await self.audio_zurueck()
        return {"ok": True, "hinweis": "Das Gespräch läuft jetzt wieder über den Hörer."}

    async def _audio_stoppen(self, audio: Any) -> None:
        try:
            await self._evtl_warten(audio.stop())
        except Exception:
            log.warning(
                "Audio-Backend %s liess sich nicht stoppen", type(audio).__name__, exc_info=True
            )

    def _gespraech_beendet_melden(self) -> None:
        if self._on_gespraech_beendet is None:
            return

        async def _lauf() -> None:
            try:
                await self._on_gespraech_beendet()  # type: ignore[misc]
            except Exception:
                log.warning("Anrufzaehler nach dem Gespraech fehlgeschlagen", exc_info=True)

        task = asyncio.create_task(_lauf())
        self._hintergrund.add(task)
        task.add_done_callback(self._hintergrund.discard)

    async def _close_session(self, session: SessionLike, run_task: asyncio.Task) -> None:
        # Transkript holen, SOLANGE die Session noch existiert.
        verlauf = list(getattr(session, "verlauf", []))
        self._letzter_verlauf = verlauf
        run_task.cancel()
        await session.close()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run_task
        await self._nachbereiten(verlauf)

    async def _nachbereiten(self, verlauf: list[tuple[str, str]]) -> None:
        """Nach dem Auflegen aufraeumen lassen (z.B. Erinnerung destillieren).

        Der Anrufer hat aufgelegt, hier draengt nichts — aber ein Fehler
        darf das Telefon nicht in einen kaputten Zustand bringen, deshalb
        faengt der Aufruf alles ab.

        Ein leeres Transkript ('nichts wurde gesagt') liefert bei den
        Plauderrollen nichts Destillierbares — da ist der fruehe Ausstieg
        richtig. Bei Rolle 3 waere er falsch: ein sofortiges Auflegen oder
        ein Transport-Fehler wuerde sonst die DB-Sitzung fuer immer offen
        lassen und gepufferte Fehler verlieren. Deshalb gilt die Wache nur
        fuer Nicht-Sprachkurs-Rollen.
        """
        if self._on_call_ended is None or self._current_role is None:
            return
        ist_sprachkurs = bool(self._current_role.get("sprachkurs"))
        if not verlauf and not ist_sprachkurs:
            return
        try:
            await self._on_call_ended(self._current_role, verlauf)
        except Exception as exc:
            log.warning("Nachbereitung des Gesprächs fehlgeschlagen: %s", exc)

    async def _teardown(self) -> None:
        self._transition(CallState.TEARDOWN)
        # Auflegen beendet immer (#38, Spec §10.3): laeuft das Gespraech auf
        # Elektra, wird das Geraet zurueckgegeben UND der Hoerer gestoppt.
        await self._netz_zurueckgeben()
        self._audio.stop()
        self._close_recording()
        self._hangup_event = None
        self._transition(CallState.IDLE)

    def _open_recording(self, role: Role) -> None:
        if not self._record_calls:
            return
        recordings_dir = Path("recordings")
        recordings_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = recordings_dir / f"{ts}_{role.get('name', 'unknown')}.wav"
        wav = wave.open(str(path), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        self._wav = wav
        log.info("recording to %s", path)

    def _close_recording(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None

    async def _play_dial_tone(self) -> None:
        phase = 0.0
        try:
            while True:
                chunk, phase = pcm16_dial_tone_frame(FRAME_SAMPLES, phase)
                await self._audio.play(chunk)
                await asyncio.sleep(FRAME_SAMPLES / RATE)
        except asyncio.CancelledError:
            pass

    def _antwort_ereignis(self, ereignis: str, **info: Any) -> None:
        """Antwortgrenzen an die Messung des Audio-Backends reichen (#113).

        Nur AudioIO misst; NetzAudioIO hat die Methode nicht.
        """
        messen = getattr(self._audio, "antwort_ereignis", None)
        if messen is not None:
            messen(ereignis, **info)

    def _on_audio_from_session(self, pcm: bytes) -> None:
        if not pcm:
            asyncio.create_task(self._audio.clear_playback(anlass=ANLASS_BARGEIN))
            return
        if self._wav is not None:
            try:
                self._wav.writeframes(pcm)
            except Exception:
                log.exception("wav write failed")
        asyncio.create_task(self._audio.play(pcm))

    async def werkzeug_ausfuehren(self, name: str, args: dict) -> dict:
        """Fuer den Werkzeug-Dienst (werkzeugdienst.py): dieselben Handler wie im
        Gespraech, aber ohne Gespraechszustand — ergebnis_per_mail braucht
        einen laufenden Rueckruf und bleibt deshalb dem Telefon vorbehalten."""
        if name == "ergebnis_per_mail":
            return {"ok": False, "error": "nur am Telefon während eines Rückrufs"}
        if name in ("freisprechen", "hoerer_zurueck"):
            return {"ok": False, "error": "nur am Telefon während eines Gesprächs"}
        return await self._tool_handler(name, args)

    async def _tool_handler(self, name: str, args: dict) -> dict:
        if name == "ergebnis_per_mail":
            return self._handle_ergebnis_per_mail(args)
        if name == "freisprechen":
            return await self._handle_freisprechen()
        if name == "hoerer_zurueck":
            return await self._handle_hoerer_zurueck()
        if name == "delegate_task":
            return handle_delegate_task(args, self._queue)
        if name == "zustellung_aendern":
            return handle_zustellung_aendern(args, self._queue)
        if name == "add_shopping_item":
            return await handle_add_shopping_item(
                args, self._mise_en_place_url, self._mise_en_place_secret
            )
        if name == "get_weather":
            return await handle_get_weather(args, wohnort=self._wohnort)
        if name == "spotify_play":
            return await handle_spotify_play(args)
        if name == "spotify_now_playing":
            return await handle_spotify_now_playing(args)
        if name == "suggest_recipe":
            return await handle_suggest_recipe(args)
        if name == "confirm_recipe":
            return await handle_confirm_recipe(args)
        if name == "modify_recipe":
            return await handle_modify_recipe(args)
        if name == "discard_recipe":
            return await handle_discard_recipe(args)
        if name == "web_suche":
            return await handle_web_suche(args, zaehler=self._websuche_zaehler)
        if name in ("merke", "erinnere", "vergiss"):
            return await self._handle_haushalt(name, args)
        return {"ok": False, "error": f"unknown tool: {name}"}

    async def _handle_haushalt(self, name: str, args: dict) -> dict:
        """Haushalts-Gedaechtnis. Nie eine Exception nach aussen: ein defektes
        Gedaechtnis darf kein Telefonat stoeren."""
        if self._haushalt is None:
            return {"ok": False, "error": "Das Haushalts-Gedächtnis ist nicht eingerichtet."}
        try:
            thema = str(args.get("thema") or "")
            if name == "merke":
                neu = args.get("neu")
                neu = neu is True or str(neu).strip().lower() == "true"
                return await self._haushalt.merke_ki(thema, str(args.get("notiz") or ""), neu=neu)
            if name == "erinnere":
                return self._haushalt.erinnere(thema or None)
            return self._haushalt.vergiss(thema, str(args.get("notiz") or "") or None)
        except Exception:  # noqa: BLE001 — sqlite3.Error und Schlimmeres
            log.exception("Haushalts-Gedächtnis: %s fehlgeschlagen", name)
            return {"ok": False, "error": "Das Gedächtnis ist gerade nicht erreichbar."}

    def _handle_ergebnis_per_mail(self, args: dict) -> dict:
        """Das vorgelesene Ergebnis zusaetzlich per Mail nachreichen."""
        if self._laufender_rueckruf is None:
            # Ausserhalb eines Rueckrufs gibt es kein Ergebnis, auf das sich
            # das beziehen koennte. Ehrlich absagen statt still nichts tun.
            return {
                "ok": False,
                "error": "Das geht nur waehrend eines Rueckrufs — hier gibt "
                "es gerade kein Ergebnis zum Verschicken.",
            }
        an = str(args.get("an") or "").strip()
        geschafft = self._queue.nachtraeglich_per_mail(self._laufender_rueckruf.task_id, an)
        if not geschafft:
            return {
                "ok": False,
                "error": "Konnte die Mail nicht in Auftrag geben. Sag das "
                "ehrlich, statt sie zu versprechen.",
            }
        log.info(
            "Mail nachgereicht fuer #%s an '%s'", self._laufender_rueckruf.task_id, an or "Standard"
        )
        return {"ok": True, "hinweis": "Die Mail geht in der naechsten Minute raus."}
