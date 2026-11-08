import asyncio
import base64
import inspect
import json
import logging
from collections.abc import Callable
from enum import Enum
from typing import Any, Protocol

from kitelefon.roles import Role, full_instructions
from kitelefon.tools import ALL_TOOL_SCHEMAS

log = logging.getLogger(__name__)

# Werkzeuge, deren Ergebnis KEINE Antwort anstossen darf. note_error ist
# als stilles Notieren entworfen (sprachkurs-Spec §4.3: "das unterbricht
# nichts") — der Nachhol-Mechanismus fuer sprechende Werkzeuge (get_weather,
# 17.08.: "Camille geht nicht ran") erzeugte hier Antworten auf nichts:
# Sitzung 8 am 18.08., "Très bien, et Filou a apprécié la promenade ?"
# 0,3 s nach der eigenen Antwort, ohne jedes VAD-Event.
STILLE_WERKZEUGE: frozenset[str] = frozenset({"note_error"})

# Transkriptions-Spur (nur Protokoll/Auswertung — Camille hoert das Audio
# direkt). Whisper-1 halluzinierte am 19.08. "We're just having a good
# time. Thank you for watching." in Sitzung 9 und lieferte leere
# Transkriptionen echter Aeusserungen. Kein language-Pin auf fr: Deutsch ist
# bei Level <= 5 ausdruecklich erlaubt (Spec Level-Regler).
TRANSKRIPTIONS_MODELL = "gpt-transcribe"


def transkriptions_einstellung(role: dict) -> dict:
    """Modell und, wo es passt, die Sprache der Transkriptions-Spur.

    Am 03.10.2026 stand im Protokoll deutscher Gespraeche Chinesisch,
    Russisch und Portugiesisch — ohne Sprachangabe rät das Modell. Gepinnt
    wird auf Deutsch, ausser im Sprachkurs und beim englischen Entwickler,
    wo die Anrufer ohnehin die Sprache wechseln.
    """
    einstellung = {"model": TRANSKRIPTIONS_MODELL}
    if not role.get("sprachkurs") and role.get("sprache", "DEUTSCH") != "ENGLISCH":
        einstellung["language"] = "de"
    return einstellung


class SessionState(str, Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    SESSION_CREATED = "session_created"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    ASSISTANT_RESPONDING = "assistant_responding"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"


class Transport(Protocol):
    async def send(self, msg: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


from collections.abc import Awaitable

# Tool-Handler dürfen synchron ODER asynchron sein. Synchrone Handler
# (z.B. handle_delegate_task) liefern direkt ein dict; asynchrone
# (z.B. handle_add_shopping_item) ein Awaitable. _dispatch_function_call
# behandelt beide Fälle via inspect.isawaitable.
ToolHandler = Callable[[str, dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]
AudioCallback = Callable[[bytes], None]


class RealtimeSession:
    def __init__(
        self,
        *,
        transport: Transport,
        role: Role,
        on_audio: AudioCallback,
        tool_handler: ToolHandler,
        model: str = "gpt-realtime-2.1-mini",
        gedaechtnis: str = "",
        vad_threshold: float = 0.6,
        vad_prefix_padding_ms: int = 500,
        vad_silence_ms: int = 500,
        reanchor_every: int = 5,
        freisprechen: bool = False,
        wohnort: str = "",
    ):
        self._transport = transport
        self._role = role
        self._gedaechtnis = gedaechtnis
        # Freisprechen (#38) eingerichtet? Entscheidet nur ueber den
        # Prompt-Abschnitt, siehe roles.full_instructions.
        self._freisprechen = freisprechen
        # Wohnort (#141) fuer den Wetter-Abschnitt des Prompts, leer = keiner.
        self._wohnort = wohnort
        self._vad_threshold = vad_threshold
        self._vad_prefix_padding_ms = vad_prefix_padding_ms
        self._vad_silence_ms = vad_silence_ms
        # Je laenger das Gespraech, desto weiter rutschen die Anweisungen im
        # Kontext nach hinten — Jean-Lucs Akzent verschwand nach einiger
        # Zeit. Alle N Antworten wird die Session-Konfiguration erneut
        # geschickt, damit sie wieder vorne steht.
        self._reanchor_every = reanchor_every
        self._antworten = 0
        # Beide Seiten mitschreiben — daraus wird nach dem Auflegen die
        # Erinnerung fuer den naechsten Anruf destilliert.
        self._verlauf: list[tuple[str, str]] = []
        self._on_audio = on_audio
        # Optional: Antwortgrenzen fuer die Messung der Wiedergabekette (#113).
        # Kein eigener Konstruktorparameter, damit die Fabriken (open_session)
        # unveraendert bleiben — der Empfaenger haengt es an sein on_audio.
        self._on_antwort = getattr(on_audio, "antwort", None)
        self._tool_handler = tool_handler
        self._model = role.get("model", model)
        self._state = SessionState.CONNECTING
        self._mic_enabled = asyncio.Event()
        self._closed = asyncio.Event()
        # Vorgemerktes response.create (siehe _antworten_lassen).
        self._antwort_faellig = False
        # begruessen() kam an, bevor die Session konfiguriert war —
        # session.updated holt die Eroeffnung dann nach.
        self._begruessung_ausstehend = False
        # Redeanteil (Spec 4.1): live gezaehlt, nicht aus dem Transkript
        # rekonstruiert — Wortzahl misst Textlaenge, nicht Zeit.
        self._anrufer_sprech_s = 0.0
        self._ki_sprech_s = 0.0
        self._anrufer_sprach_ab: float | None = None

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def verlauf(self) -> list[tuple[str, str]]:
        """Transkript beider Seiten, in der Reihenfolge des Gesprächs."""
        return list(self._verlauf)

    def _jetzt(self) -> float:
        return asyncio.get_running_loop().time()

    @property
    def sprechzeiten(self) -> tuple[float, float]:
        """(Sekunden Anrufer, Sekunden KI) — fuer TALK_LESS und Abschluss."""
        return self._anrufer_sprech_s, self._ki_sprech_s

    def _antwort_melden(self, ereignis: str, **info: Any) -> None:
        """Messung darf die Sitzung nie stoeren — Fehler hier werden nur notiert."""
        if self._on_antwort is None:
            return
        try:
            self._on_antwort(ereignis, **info)
        except Exception:
            log.debug("Antwortmessung fehlgeschlagen", exc_info=True)

    def _transition(self, new: SessionState) -> None:
        if new is self._state:
            return
        log.info("realtime state: %s -> %s", self._state.value, new.value)
        self._state = new

    def enable_mic(self) -> None:
        self._mic_enabled.set()

    def disable_mic(self) -> None:
        self._mic_enabled.clear()

    async def run(self) -> None:
        self._transition(SessionState.CONNECTED)
        try:
            while not self._closed.is_set():
                try:
                    raw = await self._transport.recv()
                except Exception as exc:
                    log.error("realtime recv closed: %s", exc)
                    break
                await self._handle_server_event(json.loads(raw))
        finally:
            self._transition(SessionState.CLOSED)

    async def send_mic_frame(self, pcm: bytes) -> None:
        if not self._mic_enabled.is_set():
            return
        if self._state not in {
            SessionState.SESSION_CREATED,
            SessionState.LISTENING,
            SessionState.USER_SPEAKING,
            SessionState.ASSISTANT_RESPONDING,
        }:
            return
        await self._transport.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }
            )
        )

    async def close(self) -> None:
        self._closed.set()
        if self._state not in {SessionState.CLOSING, SessionState.CLOSED}:
            self._transition(SessionState.CLOSING)
        await self._transport.close()

    async def _send_session_update(self) -> None:
        payload = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self._model,
                "instructions": full_instructions(
                    self._role,
                    self._gedaechtnis,
                    freisprechen=self._freisprechen,
                    wohnort=self._wohnort,
                ),
                "output_modalities": ["audio"],
                "max_output_tokens": 4096,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "turn_detection": {
                            "type": "server_vad",
                            # Gratwanderung: zu niedrig und die KI hört ihr
                            # eigenes Übersprechen im Hörer als Anrufer
                            # (Fehl-Barge-in), zu hoch und leise oder tiefe
                            # Stimmen werden gar nicht erst erkannt. Stand
                            # bis 2026-08-05 auf 0.85 — im Büro verstand
                            # das Telefon damit mehrere Kollegen schlecht.
                            # Per KITELEFON_VAD_* am Gerät abstimmbar.
                            "threshold": self._vad_threshold,
                            # Puffer VOR dem erkannten Sprechbeginn. Rettet
                            # die ersten Silben, wenn jemand leise anfängt.
                            "prefix_padding_ms": self._vad_prefix_padding_ms,
                            "silence_duration_ms": self._vad_silence_ms,
                        },
                        "transcription": transkriptions_einstellung(self._role),
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": self._role["voice_id"],
                        # Nachbearbeitung des fertigen Audios (0.25–1.5,
                        # Vorgabe 1.0). Wurde bis 2026-08-03 nie gesendet —
                        # alle Rollen sprachen gleich schnell.
                        "speed": self._role.get("speed", 1.0),
                    },
                },
                "tools": self._role.get("tool_schemas", ALL_TOOL_SCHEMAS),
                "tool_choice": "auto",
            },
        }
        await self._transport.send(json.dumps(payload, ensure_ascii=False))

    async def _handle_server_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype == "session.created":
            self._transition(SessionState.SESSION_CREATED)
            await self._send_session_update()
        elif etype == "session.updated":
            self._transition(SessionState.LISTENING)
            if self._begruessung_ausstehend:
                self._begruessung_ausstehend = False
                await self._antworten_lassen()
        elif etype == "input_audio_buffer.speech_started":
            spricht_gerade = self._state is SessionState.ASSISTANT_RESPONDING
            # Wer jetzt spricht, bekommt seine Antwort ueber die normale
            # Sprecherkennung — ein vorgemerktes response.create wuerde ihm
            # sonst beim naechsten response.done ins Wort fahren.
            self._antwort_faellig = False
            self._transition(SessionState.USER_SPEAKING)
            self._anrufer_sprach_ab = self._jetzt()
            self._on_audio(b"")  # signals clear for barge-in
            if spricht_gerade:
                # Den lokalen Puffer zu leeren reicht NICHT: der Server
                # erzeugt weiter, die naechsten Pakete werden wieder
                # angehaengt und die Stimme setzt nach kurzer Pause fort.
                # Genau deshalb liess sich das Telefon nicht unterbrechen.
                await self._transport.send(json.dumps({"type": "response.cancel"}))
        elif etype in {"input_audio_buffer.speech_stopped", "input_audio_buffer.committed"}:
            self._transition(SessionState.LISTENING)
            if self._anrufer_sprach_ab is not None:
                self._anrufer_sprech_s += self._jetzt() - self._anrufer_sprach_ab
                self._anrufer_sprach_ab = None
        elif etype in {"response.created", "response.output_item.added"}:
            self._transition(SessionState.ASSISTANT_RESPONDING)
            if etype == "response.created":
                self._antwort_melden("beginn")
        elif etype == "response.output_audio.done":
            self._transition(SessionState.LISTENING)
        elif etype == "response.done":
            self._transition(SessionState.LISTENING)
            antwort = event.get("response")
            status = antwort.get("status", "") if isinstance(antwort, dict) else ""
            self._antwort_melden("ende", status=status)
            if self._antwort_faellig:
                # Waehrend der letzten Antwort kam ein Werkzeugergebnis (oder
                # ein nachfassen()) an — jetzt ist der Turn frei, also holen
                # wir das unterdrueckte response.create nach.
                await self._antworten_lassen()
            self._antworten += 1
            if self._reanchor_every and self._antworten % self._reanchor_every == 0:
                # Zwischen den Turns, nie mitten in einer Antwort — 'speed'
                # darf laut API nur dann geaendert werden.
                log.debug("Anweisungen nach %d Antworten aufgefrischt", self._antworten)
                await self._send_session_update()
        elif etype in {"response.output_audio.delta", "response.audio.delta"}:
            pcm = base64.b64decode(event["delta"])
            self._ki_sprech_s += len(pcm) / 48000.0
            self._on_audio(pcm)
        elif etype == "conversation.item.input_audio_transcription.completed":
            text = (event.get("transcript") or "").strip()
            log.info("Anrufer: %s", text)
            if text:
                self._verlauf.append(("Anrufer", text))
        elif etype == "response.output_audio_transcript.done":
            text = (event.get("transcript") or "").strip()
            log.info("%s: %s", self._role["name"], text)
            if text:
                self._verlauf.append((self._role["name"], text))
        elif etype == "response.function_call_arguments.done":
            await self._dispatch_function_call(event)
        elif etype == "error":
            fehler = event.get("error", event)
            # Beim Unterbrechen kann die Antwort schon fertig sein, wenn
            # unser response.cancel eintrifft. Das ist ein Wettlauf, kein
            # Defekt — die Session deswegen in ERROR zu werfen wuerde das
            # Gespraech beenden.
            text = str(fehler).lower()
            if "cancel" in text and "active response" in text:
                log.debug("response.cancel kam zu spät, Antwort war schon fertig")
                return
            if "already has an active response" in text:
                # Finalreview 19.08.: dieselbe Art von Wettlauf wie oben,
                # nur ausgeloest durch ein manuelles response.create statt
                # durch response.cancel (siehe nachfassen()-Fix). Am 14.08.
                # ueberlebte die Session diesen Fehler noch klaglos — der
                # seither strengere Handler unten warf sie in ERROR, und
                # send_mic_frame() gatet ERROR komplett aus: das Mikro blieb
                # fuer den Rest des Anrufs taub. Gutartig loggen und
                # weiterleben, die laufende Antwort beantwortet den Turn.
                log.debug("realtime error (gutartig, Wettlauf um response.create): %s", fehler)
                return
            log.error("realtime error: %s", fehler)
            self._antwort_melden("ende", status="fehler")
            self._transition(SessionState.ERROR)

    async def _dispatch_function_call(self, event: dict[str, Any]) -> None:
        name = event.get("name", "")
        call_id = event.get("call_id", "")
        try:
            args = json.loads(event.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        # Ohne diese beiden Zeilen sieht man im Protokoll nur, DASS gesucht
        # wurde, nicht wonach — das machte die Fehlersuche zum Ratespiel.
        log.info("Tool-Aufruf %s%s", name, args)
        result = self._tool_handler(name, args)
        if inspect.isawaitable(result):
            result = await result
        log.info("Tool-Ergebnis %s: %s", name, result)

        await self._transport.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    },
                }
            )
        )
        if name in STILLE_WERKZEUGE:
            # Still heisst still: Ergebnis ist beim Server, das Gespraech
            # laeuft ohne unsere Einmischung weiter.
            return
        # Sprechende Werkzeuge: nur anstossen, wenn nicht schon eine Antwort
        # laeuft. Der Server setzt nach einem Werkzeugergebnis oft von selbst
        # fort, und die Sprecherkennung kann waehrenddessen eine weitere
        # Antwort ausgeloest haben. Bedingungslos gesendet ergab das dreimal
        # am 14.08. "conversation_already_has_active_response" — und im
        # Gespraech redete die Figur ueber sich selbst.
        await self._antworten_lassen()

    async def _antworten_lassen(self) -> None:
        if self._state is SessionState.ASSISTANT_RESPONDING:
            # Nicht verwerfen, sondern vormerken: gpt-realtime-mini setzt
            # nach einem Werkzeugergebnis NICHT von selbst fort. Ohne das
            # Nachholen beim naechsten response.done blieb Camille nach
            # ihrem get_weather-Aufruf einfach stumm — "geht nicht ran"
            # (Pi-Log vom 17.08., Sitzungen 2 und 3).
            log.debug("response.create vorgemerkt — es laeuft schon eine Antwort")
            self._antwort_faellig = True
            return
        self._antwort_faellig = False
        await self._transport.send(json.dumps({"type": "response.create"}))

    async def begruessen(self) -> None:
        """Die Eroeffnung anstossen — erst wenn die Leitung wirklich steht.

        Bis 18.08. ging das response.create automatisch bei session.updated
        raus. Beim gewaehlten Anruf lief die Begruessung damit ins
        Freizeichen (Sitzung 8: Begruessung 20:50:17, verbunden 20:50:25)
        und wurde vom ersten Barge-in abgeraeumt, bevor Paula sie hoerte.
        Jetzt entscheidet der CallManager, wann begruesst wird.
        """
        if self._state in {
            SessionState.LISTENING,
            SessionState.USER_SPEAKING,
            SessionState.ASSISTANT_RESPONDING,
        }:
            await self._antworten_lassen()
            return
        self._begruessung_ausstehend = True

    async def nachfassen(self, hinweis: str) -> None:
        """Das Modell anstossen, wenn der Anrufer schweigt.

        Am 14.08. fragte ein Rueckruf "Hast du 'nen Moment?" und wartete dann
        57 Sekunden auf eine Antwort, die nie erkannt wurde. Das Ergebnis —
        2435 Zeichen Recherche — wurde nie vorgelesen. Die Rueckfrage ist
        eine Hoeflichkeit, das Ergebnis ist der Zweck.
        """
        await self._transport.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": hinweis}],
                    },
                }
            )
        )
        # Finalreview 19.08.: Genau in diesem Fenster (Latenz des awaits
        # oben) kann Paula zu sprechen anfangen — server_vad meldet dann
        # noch waehrend dieser Funktion input_audio_buffer.speech_started,
        # der Zustand springt auf USER_SPEAKING. Ein jetzt trotzdem
        # gesendetes response.create laeuft der VAD-eigenen Antwort auf
        # Paulas Aeusserung in die Quere -> conversation_already_has_active_
        # response, siehe Fehlerzweig unten. Der Hinweistext liegt bereits
        # in der Conversation, die VAD-Antwort liest ihn mit — wir muessen
        # hier nichts extra anstossen. Bewusst NUR hier geprueft, nicht in
        # _antworten_lassen() generell: das wuerde den Tool-Nachhol-Pfad
        # (get_weather, 17.08.) kaputt machen, der genau dann antworten
        # soll, wenn der Anrufer inzwischen wieder zuhoert.
        if self._state is SessionState.USER_SPEAKING:
            log.debug("nachfassen: Anrufer spricht inzwischen — kein eigenes response.create")
            return
        await self._antworten_lassen()


class WebsocketTransport:
    """Adapts `websockets` client to the Transport protocol."""

    def __init__(self, ws: Any):
        self._ws = ws

    async def send(self, msg: str) -> None:
        await self._ws.send(msg)

    async def recv(self) -> str:
        return await self._ws.recv()

    async def close(self) -> None:
        await self._ws.close()


OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


async def open_websocket(
    model: str, api_key: str, base_url: str = OPENAI_REALTIME_URL
) -> WebsocketTransport:
    """Verbindet mit der Realtime-API. `base_url` ist nur fuer Tests (#93) da."""
    import websockets

    url = f"{base_url}?model={model}"
    ws = await websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        max_size=None,
    )
    return WebsocketTransport(ws)
