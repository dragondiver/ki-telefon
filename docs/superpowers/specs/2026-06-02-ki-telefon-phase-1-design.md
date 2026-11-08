# KI-Telefon Phase 1 — Design

**Datum:** 2026-06-02
**Status:** Spec (vor Implementierungsplan)
**Vorbild:** [Make-Magazin KI-Telefon (Fork)](https://github.com/MakeMagazinDE/KI-Telefon),
lokal unter `~/Development/refs/KITELEFON/KI-Telefon/`.

## Ziel

Ein altes Wählscheiben-Tischtelefon spricht mit einer KI. Der Nutzer hebt ab,
wählt eine Ziffer, hört einen Freiton, und führt dann ein Gespräch über die
OpenAI Realtime API. Während des Gesprächs erkennt das Modell selbst, wenn
eine Aufgabe asynchron erledigt werden sollte (Recherche, Termin, Erinnerung,
Buchung), und delegiert sie per Tool-Call. Die delegierte Aufgabe wird lokal
in einer Queue (SQLite) abgelegt. Phase 2 (nicht Teil dieses Specs) holt sie
ab und liefert das Ergebnis per Email oder Rückruf.

Entwicklung läuft auf Mac (Tastatur ersetzt Gabelschalter und Wählscheibe),
Zielplattform ist ein Raspberry Pi 4 oder 5 in einem Telekom-611-Telefon.

## Scope

**Phase 1 (dieses Spec):**

- Komplettes Telefon-Verhalten: Hörer ab → Wählton → Wahl → Freiton → KI-Gespräch → Hörer auf.
- Personas wie im Vorbild (`1`–`9` = je eine KI-Rolle aus `roles.py`).
- Tool-Definition `delegate_task` in der Realtime-Session.
- Tool-Aufruf schreibt ausschließlich in SQLite (`tasks.db`).
- Plattform-Abstraktion: ein Code-Pfad, zwei Implementierungen für Hookswitch
  und Dialer (Mac via Tastatur, Pi via GPIO).

**Nicht in Phase 1:**

- Agenten, die die delegierten Aufgaben bearbeiten (`src/kitelefon/agents/` bleibt leer).
- Rückkanäle (Email, Rückruf).
- Automatikmodus / eingehende Anrufe (`0` ist Reserviert, keine Funktion).
- Klingel-Hardware-Ansteuerung (Bell). Wird in Phase 2 ergänzt, wenn Rückrufe relevant werden.
- Gesprächspartner-Erkennung über Namens-Lookup (`gespraechspartner.py`-Äquivalent).

## Architektur-Überblick

```
┌─────────────────┐      ┌─────────────────┐
│  HookSwitch     │─────▶│                 │
│ (Keyboard/GPIO) │      │                 │      ┌──────────────┐
└─────────────────┘      │                 │◀────▶│ OpenAI       │
                         │   CallManager   │      │ Realtime API │
┌─────────────────┐      │   (asyncio)     │      └──────────────┘
│  Dialer         │─────▶│                 │
│ (Keyboard/GPIO) │      │                 │
└─────────────────┘      │                 │
                         │                 │
┌─────────────────┐      │                 │
│  AudioIO        │◀────▶│                 │
│ (sounddevice)   │      │                 │
└─────────────────┘      └────────┬────────┘
                                  │ tool call
                                  ▼
                         ┌─────────────────┐
                         │  TaskQueue      │
                         │  (SQLite)       │
                         └─────────────────┘

                         agents/  ← Phase 2 (leer)
```

Sechs Bausteine, jeder mit einer klaren Aufgabe und einem schmalen Interface:

- **HookSwitch** — meldet „Hörer abgehoben/aufgelegt" als Event-Stream.
- **Dialer** — meldet eine erfasste Ziffer (1–9, 0). Auf Mac via Tastatur,
  auf Pi via GPIO-Pulserkennung.
- **AudioIO** — Mikrofon-Frames raus, Lautsprecher-Frames rein. PCM16 24 kHz mono.
- **RealtimeSession** — WebSocket zur OpenAI Realtime API, registriert das
  `delegate_task`-Tool, leitet Audio bidirektional, kennt eine explizite
  State-Machine (übernommen aus dem Vorbild).
- **TaskQueue** — SQLite-Kapsel, eine Tabelle. Phase 1 nutzt nur `enqueue`.
- **CallManager** — verdrahtet alles, hält die State-Machine des Anrufs.

## Projekt-Layout

```
kitelefon/
├── pyproject.toml
├── README.md
├── .env.example              # OPENAI_API_KEY, KITELEFON_PLATFORM=mac|pi
├── src/kitelefon/
│   ├── __init__.py
│   ├── __main__.py           # python -m kitelefon
│   ├── config.py             # Settings aus env (pydantic-settings)
│   ├── call_manager.py       # State-Machine, Hauptloop
│   ├── hookswitch/
│   │   ├── __init__.py       # HookSwitch Protocol
│   │   ├── keyboard.py       # Mac
│   │   └── gpio.py           # Pi
│   ├── dialer/
│   │   ├── __init__.py       # Dialer Protocol
│   │   ├── keyboard.py       # Mac
│   │   └── gpio.py           # Pi (Pulszählung)
│   ├── audio.py              # SoundDeviceAudio + lokale Töne (Wählton/Freiton)
│   ├── realtime.py           # RealtimeSession + State-Machine
│   ├── tools.py              # delegate_task Schema + Handler
│   ├── tasks.py              # TaskQueue (SQLite)
│   ├── roles.py              # Personas (übernommen vom Vorbild)
│   └── agents/               # Phase 2 Stubs
│       └── __init__.py
└── tests/
    ├── test_tasks.py
    ├── test_tools.py
    ├── test_hookswitch.py
    ├── test_dialer.py
    ├── test_call_manager.py  # mit Fakes
    └── pi/                   # nur mit -m pi
```

**Modul-Verantwortlichkeiten:**

- `hookswitch/`, `dialer/`, `audio.py` → keine Realtime-/Queue-Abhängigkeit.
- `realtime.py` → kennt weder Audio-Hardware noch SQLite. Liefert Audio-Frames
  per `async for` und Tool-Calls per Callback.
- `tools.py` → bekommt `TaskQueue` per Dependency Injection.
- `call_manager.py` → einziger Ort, an dem alles zusammengeführt wird.
- Plattform-Auswahl per env-Variable `KITELEFON_PLATFORM=mac|pi`. Keine
  `if platform.system()`-Verteilung im Code.

Python ≥ 3.11, asyncio, kein Web-Framework.

## Session-Lebenszyklus

Fünf Zustände im `CallManager`:

```
                  ┌── hung_up ──────────────────────────────┐
                  ▼                                         │
              ┌────────┐  lifted  ┌─────────┐               │
              │  IDLE  │─────────▶│ DIALING │               │
              └────────┘          └────┬────┘               │
                  ▲                    │ digit              │
                  │            (Persona gewählt)            │
                  │                    ▼                    │
              ┌─────────┐         ┌──────────┐              │
              │TEARDOWN │◀────────│ RINGBACK │ hung_up      │
              └─────────┘ hung_up │ (Freiton)│──────────────┤
                  ▲               └────┬─────┘              │
                  │                    │ (Ton fertig)       │
                  │                    ▼                    │
                  │              ┌──────────┐               │
                  └──────────────│ IN_CALL  │ hung_up ──────┘
                                 └──────────┘
```

**IDLE** — `HookSwitch` aktiv, sonst nichts offen.

**IDLE → DIALING (Hörer ab):**

1. `AudioIO.start_output()` — nur Lautsprecher öffnen.
2. 425-Hz-Wählton lokal generieren (Sinus per numpy, sounddevice OutputStream).
3. `Dialer.read_digit()` läuft (Mac: Tastatur; Pi: GPIO-Pulszähler mit
   `MIN_PULSE_SEPARATION = 0.08` wie im Vorbild).
4. Bei erster Ziffer Wählton sofort stoppen.

**DIALING → RINGBACK (Persona-Ziffer 1–9 erfasst):**

1. Persona aus `roles.py` per Index aussuchen (`roles[digit - 1]`).
2. **Pre-Warming startet parallel:**
   - `RealtimeSession.connect(persona)` — WebSocket, `session.update` mit
     Persona-Instructions, Voice, Tools (`delegate_task`), PCM16 24 kHz,
     `server_vad`.
   - Mikrofon bleibt noch deaktiviert (`mic_enabled = False`).
3. **Parallel** Freiton lokal abspielen (2× 1 s Ton + 4 s Pause).
4. Beide Tasks racen: ist der Freiton fertig **und** Session ready → `IN_CALL`.

**DIALING — Sonderziffer `0`:** Phase 1: keine Funktion. Wählton stoppt,
warten auf Auflegen, zurück zu IDLE.

**RINGBACK → IN_CALL:**

1. Freiton-Stream stoppen.
2. `AudioIO.start_input()` — Mikrofon öffnen.
3. `mic_enabled = True`.
4. Drei asyncio-Tasks aktiv: Uplink (mic→ws), Downlink (ws→speaker), Events.

**IN_CALL — Tool-Call-Pfad:**

1. Server-Event `response.function_call_arguments.done` mit Name `delegate_task`.
2. Handler ruft `TaskQueue.enqueue(...)` (SQLite INSERT, `status='pending'`).
3. `conversation.item.create` mit `function_call_output`
   `{ok: true, task_id: …}` + `response.create` zurück ans Modell.
4. Modell spricht Bestätigung.

**Jeder Zustand → TEARDOWN (Hörer aufgelegt):** alle Tasks canceln, WebSocket
schließen, Audio-Streams schließen, Puffer leeren, zurück zu IDLE.

**Startup-Invariante:** Wenn Hörer beim Programmstart schon abgehoben ist,
erst auf Auflegen warten, dann IDLE betreten.

**Im CallManager-State lebt:** nur die aktive Persona-Wahl und die
Session-Referenz. Keine Audio-Puffer, keine Transkripte zwischen Anrufen.

## Datenmodell

SQLite-Datei `tasks.db`, eine Tabelle:

```sql
CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,                       -- ISO-8601 UTC
    agent_type   TEXT NOT NULL,                       -- 'research'|'reminder'|'booking'|'other'
    description  TEXT NOT NULL,
    deliver_via  TEXT NOT NULL,                       -- 'email'|'callback'
    deliver_to   TEXT,                                -- Email oder Kennung, optional
    payload_json TEXT,                                -- ganzer Tool-Call
    status       TEXT NOT NULL DEFAULT 'pending',     -- 'pending'|'running'|'done'|'failed'
    result       TEXT,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE INDEX idx_tasks_status_agent ON tasks(status, agent_type);
```

`TaskQueue`-API (Phase 1):

```python
class TaskQueue:
    def __init__(self, db_path: Path): ...
    def enqueue(
        self,
        *,
        agent_type: str,
        description: str,
        deliver_via: Literal["email", "callback"],
        deliver_to: str | None,
        payload: dict,
    ) -> int:
        """Returns task_id."""
```

Phase 2 ergänzt `claim_pending`, `mark_done`, `mark_failed`. In Phase 1 nicht.

## Tool-Schema

Genau ein Tool in der Realtime-Session:

```json
{
  "type": "function",
  "name": "delegate_task",
  "description": "Delegiere eine Aufgabe an einen asynchronen Agenten. Verwende dies, wenn der Nutzer um etwas bittet, das Recherche, Buchung, Erinnerung oder eine andere Hintergrundarbeit erfordert, die nicht im Gespräch erledigt werden kann. Bestätige dem Nutzer kurz, dass die Aufgabe notiert ist, und wie das Ergebnis ankommen wird.",
  "parameters": {
    "type": "object",
    "properties": {
      "agent_type": {
        "type": "string",
        "enum": ["research", "reminder", "booking", "other"]
      },
      "description": { "type": "string" },
      "deliver_via": {
        "type": "string",
        "enum": ["email", "callback"]
      },
      "deliver_to": { "type": "string" }
    },
    "required": ["agent_type", "description", "deliver_via"]
  }
}
```

Im `session.update`-Payload als `tools: [...]`, `tool_choice: "auto"`.

Handler-Fluss:

1. Args validieren (pydantic). Fehler → `function_call_output {ok: false, error: "…"}`.
2. `TaskQueue.enqueue(...)` → `task_id`.
3. `function_call_output {ok: true, task_id, ack: "…"}` + `response.create`.

System-Prompt-Erweiterung pro Persona (an `gpt_style` angehängt):

> „Wenn der Anrufer um etwas bittet, das du nicht direkt im Gespräch erledigen kannst (Recherche, Termin, Buchung, Erinnerung), rufe das Tool `delegate_task` auf. Frage vorher kurz nach, wie das Ergebnis ankommen soll (Email oder Rückruf), und wohin. Bestätige dann die Notiz und mach normal weiter."

## Fehlerbehandlung

| Fehler | Reaktion |
|---|---|
| Kein API-Key / 401 (RINGBACK) | Wählton-Stopp, Fehlerton, IDLE. |
| WS-Connect fehlgeschlagen (RINGBACK, 5 s Timeout) | wie oben. |
| WS bricht mid-call | TEARDOWN. Lokaler kurzer Beep. Kein Auto-Reconnect. |
| Audio-Device weg | Streams werfen, CallManager fängt → TEARDOWN, IDLE. |
| Tool-Call ungültig | `function_call_output {ok: false, error}`. Gespräch läuft weiter. |
| SQLite-Fehler | wie ungültiger Tool-Call. Log mit Stacktrace. |
| Hangup mid-flight | `asyncio.Event` cancelt alle Tasks, Cleanup im `finally`. |
| Wahl-Timeout (5 s ohne Ziffer nach Lift) | Wählton aus, warten auf Auflegen. |
| Audio-Underrun | Stille auffüllen (wie Vorbild). Kein Anruf-Abbruch. |
| Barge-in | Bei `speech_started` lokalen Speaker-Puffer leeren. |

**Logging:** stdlib `logging`. State-Übergänge auf INFO, Detail auf DEBUG.
Kein Auto-Reconnect.

## Tests

**Unit (Mac, ohne Hardware, ohne Netz):**

| Modul | Was |
|---|---|
| `tasks.py` | `enqueue` Roundtrip mit `:memory:` SQLite. |
| `tools.py` | Args-Validation gut/falsch, JSON-Roundtrip. |
| `hookswitch/keyboard.py` | Events bei Tastendruck (Fake-Input). |
| `dialer/keyboard.py` | Events bei Ziffern, Timeout-Verhalten. |
| `audio.py` | Frame-Größen, PCM-Konvertierung. |
| `realtime.py` | Tool-Call-Dispatch über Fake-WebSocket. |
| `call_manager.py` | State-Transitions mit Fakes: Lift→Digit→Ringback→InCall→Hangup; Hangup in DIALING; Hangup in RINGBACK; Tool-Call schreibt Row. |

**Integration (Live, `-m live`, default skipped):** Ein Test, der die echte
API anspricht, einen WAV-Schnipsel mit Delegations-Bitte schickt und prüft,
dass eine SQLite-Row entsteht.

**Manuelle Mac-Akzeptanz-Liste** in der README:

1. Programm starten → keine Fehler.
2. Leertaste → Wählton hörbar.
3. `1` → Wählton aus, Freiton, danach Persona 1 spricht.
4. Bitte um Erinnerung per Email → `sqlite3 tasks.db 'SELECT * FROM tasks'` zeigt die Row.
5. Leertaste (Auflegen) → Gespräch endet binnen 1 s.
6. Strg+C → sauberes Beenden.

**Pi-Hardware-Tests** (`tests/pi/`, `-m pi`): Wählscheiben-Pulsfilter,
Hookswitch-Entprellung, Audio-Roundtrip mit USB-Soundkarte.

## Konventionen

- Sprache: Deutsche Doku/UX-Strings im Repo. Identifier und API-Begriffe Englisch.
- Tests mit pytest, Marker `live` und `pi`.
- `OPENAI_API_KEY` über `.env` (nie ins Repo).

## Offene Punkte für Phase 2 (nicht hier zu lösen)

- Agenten-Implementierungen (`research` via Claude API + MCP, `reminder` lokal,
  `booking` über externe APIs).
- Rückkanäle: SMTP für Email; Telefon-Rückruf via Klingel-Hardware + Pre-Warming.
- Persistente Konversations-History (über Anrufe hinweg).
- Eingehende Anrufe / Automatikmodus (Klingel-H-Brücke).
