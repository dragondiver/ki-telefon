# ki-telefon — Projekt-Kontext für Claude

## Worum es geht

KI-Telefon nach Vorbild des Make-Magazin-Projekts „KI-Telefon" (Ausgabe 2/26),
erweitert um einen Agenten-Pfad: während eines Realtime-Gesprächs erkennt das
Modell per Function-Calling, dass eine Aufgabe delegiert werden muss, und
schreibt sie in eine lokale Queue (`tasks.db`). Wer diese Queue abarbeitet,
ist bewusst NICHT Teil dieses Repos — siehe Hinweis zum allgemeinen Agenten
weiter unten.

- **Phase 1 (dieses Repo):** Telefon-Grundgerüst — Gabelschalter, Audio,
  Realtime-Gespräch, Tool-Call schreibt in SQLite. Agenten sind Stubs.
- **Phase 2 (vorbereitet):** Agenten in `src/kitelefon/agents/`, Rückkanäle.

### Französischkurs (Ziffer 3, seit 2026-08-17)

Camille ersetzt die Berlinerin. Die gesamte Didaktik (Prompt, Kanon,
Level-Regeln, Lernzustand) lebt im Schwester-Repo `sprachkurs`, eingebunden
als **Git-Abhängigkeit auf festem Commit** (`rev` in `pyproject.toml`, seit
telefon-ai#81). Neue sprachkurs-Stände kommen nur durch Anheben von `rev` an —
siehe README, „sprachkurs anheben". Ein Nachbar-Checkout ist nicht nötig.
Spec und Pläne: `sprachkurs/docs/superpowers/`. Eckpunkte im Code hier:

- `sprachkurs_call.py` — Sitzungskontext, Offline-Schnappschuss, Spool
- `Sitzungsuhr` in `call_manager.py` — Phasen-Events (13/17/21 min, TALK_LESS),
  dazu Stille-Nachfrage (15 s, max. 2×) und, unabhängig davon, der
  Mikro-Wachhund gegen einen toten Input-Stream
- Rolle 3 hat `sprachkurs=True`: eigener Prompt-Bypass in
  `full_instructions`, eigene `tool_schemas` (note_error, get_news_fr,
  get_weather), Modell über `KITELEFON_SPRACHKURS_MODEL`
- Camille ist von Autocall und Rückruf-Botendienst ausgeschlossen (bewusst)
- `KITELEFON_SPRACHKURS_ENQUEUE` bleibt aus, bis Plan 3 (heim-agent) die
  `konsument`-Filter liefert

### Live-Integrationen (zusätzlich zur Delegations-Queue)

Manche Tool-Calls werden direkt im Gespräch ausgeführt, nicht delegiert:

- `add_shopping_item` → Mise-en-Place (REST, `KITELEFON_MISE_EN_PLACE_*`).
- `suggest_recipe` / `confirm_recipe` / `discard_recipe` → Claude API
  (Opus 4.7) generiert strukturiertes Rezept via `messages.parse()` +
  Pydantic-Schema (siehe `src/kitelefon/agents/recipes.py`). Draft im Modul
  gecacht (single-user device). Bei Zustimmung Post an Mise-en-Place
  `POST /api/agent/recipes` (Endpoint im railway-Repo, commit `8867d5b`).
  Braucht `ANTHROPIC_API_KEY` in `.env`.
- `spotify_play` → Spotify Web API. Steuert einen **Pure Evoke Play**
  (Connect-Lautsprecher) per Sprache. **Spotify Duo / zwei Konten**
  (Florian + Anna): Kontowahl nicht über Stimmerkennung, sondern als
  Kaskade — (1) Begrüßung "Wer ist da?", (2) Selbstnennung im Wunsch,
  (3) Fallback: Handler sucht Playlist in beiden Konten und fragt bei
  Mehrdeutigkeit zurück. Auth pro Konto via PKCE-OAuth
  (`scripts/spotify_auth.py --account <name>` → `token_<name>.json`,
  langlebiges Refresh-Token, gitignored). Implementation:
  `src/kitelefon/agents/spotify.py`, System-Prompt-Baustein
  `SPOTIFY_SUFFIX` in `roles.py`.

Entwicklung läuft auf Mac (Tastatur als Hookswitch + Wählscheibe), Ziel ist
der Pi 5. (Ein CM5-Versuch scheiterte an defekter Hardware — Historie in
`docs/pi-inbetriebnahme.md` §10/§11.)

## Architektur-Eckpunkte (in Brainstorming festgelegt)

- Python ≥ 3.11, asyncio, kein Web-Framework. Klein und Pi-freundlich.
- **Delegations-Erkennung:** Function-Calling im Realtime-Modell (Variante A).
  Ein generisches Tool `delegate_task(agent_type, description, deliver_via)`.
  Keine zweite NLU-Schicht.
- **Phase-1-Scope:** Nur Telefon + Tool-Definitionen. Tool-Aufruf schreibt
  ausschließlich in SQLite. Kein Agent-Code, keine Rückkanäle in Phase 1.
- **Der allgemeine Agent ist bewusst KEIN Teil von ki-telefon.** Im
  Brainstorming vom 2026-08-09 stellte sich heraus, dass der ursprünglich
  angedachte „Hermes lokal auf einem Pi" eigentlich ein autonomer,
  mehrere-Provider-fähiger Agent auf einem noch zu kaufenden Mini-PC
  werden soll — nutzbar auch aus anderen Projekten heraus, nicht nur vom
  Telefon. Das ist ein eigenständiges Infrastruktur-Vorhaben mit eigener
  Spec. `tasks.db` bleibt der stabile Vertrag dazwischen — siehe
  `docs/superpowers/specs/2026-08-09-delegations-schnittstelle-design.md`.
- **Hookswitch:** `HookSwitch` Protocol; `KeyboardHookSwitch` (Mac, Leertaste),
  `GpioHookSwitch` (Pi, gpiozero/RPi.GPIO).
- **Queue:** SQLite-Datei (`tasks.db`), eine Tabelle `tasks` mit
  `id, created_at, agent_type, payload_json, status, result, deliver_via`.
  Phase 1 schreibt nur `INSERT`. Stdlib `sqlite3`.
- **Audio:** `sounddevice` (PortAudio) auf Mac + Pi identisch.
  PCM16 mono 24 kHz — natives Format der OpenAI Realtime API, kein Resampling.
- **Zielhardware Pi:** Pi 5 (Model B). Pi Zero 2 W explizit nicht Ziel.
  Ein Umzug auf ein Compute Module 5 (CM5-NANO-B) wurde versucht und wegen
  defekter Hardware rückabgewickelt — `docs/pi-inbetriebnahme.md` §10/§11.
- **Plattform-Switch:** env `KITELEFON_PLATFORM=mac|pi` wählt
  Hookswitch-Implementierung. Keine `if platform.system()`-Verteilung im Code.

## Referenz-Implementierung (Make-Magazin Fork)

Lokal ausgecheckt unter `~/Development/refs/KITELEFON/KI-Telefon/`.
Make-Ausgabe 2/26. Dieser Fork erweitert den ursprünglichen Code um
Automatikmodus, robustere Wählscheiben-Diagnose, Jitter-Puffer und eine
explizite Realtime-State-Machine.

### Hardware (Pi-Pins, BCM)

- **Gabelschalter (Handset):** GPIO 5, Pull-Up, HIGH = abgehoben (`handset.py`).
- **Wählscheibe (Pulse):** GPIO 26, Pull-Up, FALLING edge zählt Impulse
  (`main.py read_rotary_wheel`).
- **Klingel:** H-Brücke an IN1=17, IN2=27, ENA=22; PWM 1 kHz, bipolare
  Wechselspannung ~10 Hz für authentischen Klingel-Sound (`bell.py`).
- **Audio:** USB-Soundkarte (`Find_Soundcard_Index.py`), PCM16 mono 24 kHz.

### Bedienlogik

1. Beim Start: einmal kurz klingeln als Funktionstest.
2. Ruhezustand: warten bis Hörer abgehoben wird.
3. Hörer ab → 425-Hz-Freizeichen lokal (`start_dial_tone`).
4. Nutzer wählt 1–9 → entsprechende KI-Rolle aus `roles.py`.
   - `0` toggelt Automatikmodus (im Fork; original war `0` Shutdown).
5. Während des Freitons (`play_freitone`, ~2× 1 s Ton + 4 s Pause) wird die
   OpenAI-Session schon vorgewärmt (`enable_microphone=False`), inkl.
   `greeting.wav` als initiales Input-Audio. Mikro erst nach Freiton frei.
6. Gespräch läuft, bis Hörer aufgelegt wird (`monitor_handset` Thread).
7. Automatikmodus: nach `AUTOCALL_DELAY=30s` ohne Anruf klingelt das Telefon
   selbst, beim Abheben startet zufällige Rolle.

**Wählscheiben-Filter:** `MIN_PULSE_SEPARATION = 0.08` (80 ms). Echte Impulse
liegen ~95–100 ms auseinander, kürzere sind Prellen/Störungen. Diagnose-Output
(`[DIAL RAW]`, `[DIAL SUMMARY]`) protokolliert jeden Roh-Impuls und
Intervalle, um Filter pro Hardware abzustimmen.

**Wichtige Regel:** Im GPIO-Callback keine blockierenden Audio-Aufrufe. Nur
Flag setzen, Audio-Aktion danach im normalen Programmfluss
(`dial_tone_stop_requested[0] = True`).

### OpenAI Realtime API (Stand GA, `openai_ws.py`)

- Endpoint: `wss://api.openai.com/v1/realtime?model={model}`.
- Auth: `Authorization: Bearer …` Header. Kein `OpenAI-Beta`-Header mehr (GA).
- Default-Modell: `gpt-realtime-2.1-mini` für alle Rollen (seit #131,
  vorher `gpt-realtime-2.1`). Camille (Sprachkurs) hat ein eigenes, separat
  einstellbares Setting (`sprachkurs_model` / `KITELEFON_SPRACHKURS_MODEL`),
  dessen Default ebenfalls `gpt-realtime-2.1-mini` ist. Der Mechanismus für
  ein rollenspezifisches Modell (Feld `model` in `roles.py`) bleibt, wird
  aber seit 2026-08-03 von keiner Rolle genutzt.
- `session.update`-Payload (GA-Shape):
  - `session.type = "realtime"`, `model` in der Session.
  - `output_modalities: ["audio"]` (ersetzt `modalities`).
  - `audio.input.format = {type: "audio/pcm", rate: 24000}`.
  - `audio.input.turn_detection = {type: "server_vad", threshold: 0.5,
    prefix_padding_ms: 300, silence_duration_ms: 500}`.
  - `audio.input.transcription = {model: "gpt-transcribe"}`
    (verschachtelt, GA; whisper-1 halluzinierte am 19.08. ganze Sätze).
  - `audio.output.format = {type: "audio/pcm", rate: 24000}` + `voice`.
  - `max_output_tokens: 4096`. `temperature` ist in GA kein Session-Parameter.
- **Wichtige Server-Events:** `session.created`, `session.updated`,
  `input_audio_buffer.speech_started`/`speech_stopped`/`committed`,
  `response.created`, `response.output_audio.delta` (GA, war
  `response.audio.delta` in Beta), `response.output_audio.done`,
  `response.output_audio_transcript.done`,
  `conversation.item.input_audio_transcription.completed`, `error`.
- **Client-Events:** `input_audio_buffer.append` (base64-PCM),
  `input_audio_buffer.commit`, `response.create`, `response.cancel`,
  `conversation.item.truncate`.
- **Barge-in:** Bei `speech_started` lokalen Audio-Puffer leeren.

### Audio-Pipeline (Referenz)

- Mikro-Callback (`pyaudio`) legt Frames in `mic_queue`. Sende-Thread liest
  und schickt via WebSocket (`input_audio_buffer.append`, base64).
- Empfangs-Thread dekodiert `response.output_audio.delta` und hängt PCM an
  `audio_buffer` (bytearray, Lock-geschützt).
- Lautsprecher-Callback (`pyaudio`) zieht Bytes aus `audio_buffer`.
- **Jitter-Puffer:** Wiedergabe startet erst bei
  `MIN_PLAYBACK_BUFFER_BYTES = 12000` (~250 ms bei 24 kHz × 2 Byte).
  Bei Underrun: Rest mit Stille auffüllen, dann wieder anstauen lassen.
- Vor jedem neuen Gespräch alle Puffer + `playback_started` zurücksetzen.

### Rollen-System (`roles.py`)

Liste von Dicts mit `name, voice_id, gpt_style, speed` (optional `model`).
Stimmen: `alloy, ash, ballad, coral, echo, sage, shimmer, verse`. Drei
gemeinsame Instruktions-Bausteine (`instructions_1/2/3`) werden hinten an
den `gpt_style` gehängt. Gewählte Rolle = `role[role_number - 1]`.

### Gesprächspartner-Erkennung (`gespraechspartner.py`)

Eingabe-Transkript wird auf bekannte Namen geprüft
(`maybe_update_person_from_transcript`). Bei Treffer wird die
Realtime-Session mit zusätzlichen Personeninfos (Alter, Beruf, Hobby) neu
konfiguriert.

## Übernommene Ideen aus der Referenz, die unser Design schärfen

- **State-Machine im Realtime-Client explizit halten** (DISCONNECTED →
  CONNECTING → CONNECTED → SESSION_CREATED → SESSION_CONFIGURED → LISTENING →
  USER_SPEAKING → ASSISTANT_RESPONDING → CLOSING → CLOSED → ERROR). Hilft
  beim Debuggen und beim Pre-Warming.
- **Pre-Warming der Session** während Wähl-/Freiton-Phase. Mikrofon erst nach
  Hörer-Abnahme bzw. Wahl-Abschluss freigeben, sonst sendet man Freiton an
  die KI.
- **Jitter-Puffer** für Audio-Out auf Pi unverzichtbar.
- **Auflegen wird in jeder Phase überwacht** (Ruhe, Wahl, Freiton, Gespräch).
- **Hörer-bereits-abgehoben-beim-Start:** Erst auf Auflegen warten, dann
  State-Machine initialisieren.
- **Wählscheibe-Diagnose** als eigener Logging-Modus für Hardware-Tuning.
- **Definierter Startzustand garantieren.**

## Bewusste Unterschiede zur Referenz

- **Asyncio statt Threading.** Geringere CPU-Last auf Pi, weniger Lock-Wirrwarr,
  bessere Cancellation für Hangup-Mid-Call.
- **`sounddevice` statt `pyaudio`.** Eine Lib für Capture + Playback, läuft auf
  Mac und Pi identisch, kein PortAudio-Mix.
- **Plattform-Abstraktion explizit.** Hookswitch/Dialer als Protocol mit zwei
  Implementierungen (Keyboard für Mac, GPIO für Pi).
- **Function-Calling für Delegation.** Die Referenz hat keinen Agenten-Pfad —
  das ist unser zentraler Mehrwert.
- **SQLite-Queue** statt nur ephemeres Gespräch. Tasks überleben Anruf-Ende.

## Mac-Dev-Stolpersteine

- **Mikrofon-Berechtigung:** Wenn KI nichts hört obwohl die Pipeline läuft
  (`input_audio_buffer.append` Frames gehen raus, aber als Null-Bytes,
  Server-VAD triggert nie) → macOS gibt dem Terminal-Prozess (Ghostty etc.)
  keinen Mic-Zugriff. Systemeinstellungen → Datenschutz → Mikrofon → Terminal
  aktivieren. Notfalls erstmal mit `python -c "import sounddevice as sd;
  sd.rec(24000,24000,1,'int16'); sd.wait()"` den OS-Dialog provozieren.
- **`.env` wird nicht überschrieben:** python-dotenv + pydantic-settings
  überschreiben per Default keine bestehenden Env-Vars. Wenn `.env` korrekt
  ist aber Auth trotzdem failt: `env | grep <VARNAME>` checken, ggf. unsetten.

## Konventionen

- Sprache im Code/Doku: Deutsch im Repo (UX-Strings, Doku, README).
  Identifier-Namen und API-Begriffe in Englisch.
- Tests mit pytest. Hardware-abhängige Tests laufen nur mit `pi`-Marker.
- Keine geheimen Daten ins Repo: `.env`-File für `OPENAI_API_KEY`.

## Referenz-Repo-Pfad

`~/Development/refs/KITELEFON/KI-Telefon/` — vor allem `main.py`,
`openai_ws.py`, `handset.py`, `roles.py`, `bell.py` lesen, wenn
hardware-/protokoll-spezifische Fragen auftauchen.
