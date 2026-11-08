# kitelefon

Ein altes analoges Tischtelefon wird zum sprechenden Haushalts-Assistenten.
Hörer abheben, Rolle wählen, reden — die Stimme am anderen Ende erkennt, was
du willst, und erledigt es: Musik abspielen, Einkaufsliste pflegen, Rezepte
vorschlagen, Aufgaben für später notieren.

Inspiriert vom Make-Magazin-Projekt „KI-Telefon" (Ausgabe 2/26), aber
eigenständig implementiert und auf einen agentischen Pfad ausgebaut.

```
┌──────────────┐    PCM16 24kHz    ┌─────────────────────────┐
│  Wählscheibe │ ─────────────────▶│  OpenAI Realtime API    │
│   + Hörer    │ ◀──────────────── │ (gpt-realtime-2.1-mini) │
└──────────────┘                   └─────────┬───────────────┘
                                             │ Function-Calling
                       ┌─────────────────────┼─────────────────────┐
                       ▼                     ▼                     ▼
                ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
                │   Spotify    │     │ Mise-en-Place│     │  Claude API  │
                │  Connect →   │     │  Einkaufs-   │     │ (Opus 4.7) → │
                │  Evoke Play  │     │   liste +    │     │   Rezept     │
                │              │     │  Rezepte     │     │  Generator   │
                └──────────────┘     └──────────────┘     └──────────────┘
                                             ▲
                                             └── confirm_recipe
                                                 (POST /api/agent/recipes)
```

## Was du am Telefon machen kannst

| Hörer ab, dann sagen…                                  | Was passiert                                                       |
| ------------------------------------------------------- | ------------------------------------------------------------------ |
| „Spiel mir die Daylist."                                | Spotify-Daylist startet auf dem Evoke Play (per Connect).          |
| „Spiel was von Queen."                                  | Künstler-Top-Tracks via Search-API.                                |
| „Wer singt das gerade?"                                 | Liest Titel + Künstler des laufenden Stücks vor.                   |
| „Brötchen auf die Einkaufsliste, sechs Stück."          | Landet sofort in Mise-en-Place.                                    |
| „Schlag mir was für Pasta mit Spinat vor."              | Claude generiert ein vollständiges Rezept, KI liest die Zusammen­fassung vor. „Mit Knoblauch" → verfeinert. „Ja, speicher" → in Mise-en-Place. |
| „Erinnere mich morgen früh ans Brot, per Email an X."   | In die lokale SQLite-Queue → asynchroner Agent (Phase 2).          |

Auf dem Mac als Entwicklungsmaschine: Leertaste = Hörer ab/auflegen,
1–9 = Rolle. Auf dem Pi: echter Gabelschalter und echte Wählscheibe.

## Architektur

- **Python ≥ 3.11, asyncio** — eine Event-Loop, klare Cancellation bei
  Mid-Call-Hangup, niedrige CPU-Last auf dem Pi.
- **OpenAI Realtime API** (GA, WebSocket, PCM16 mono 24 kHz, Server-VAD).
  Function-Calling-Schema bringt **sieben Tools** mit, der Realtime-Loop
  routet eingehende Calls an die passenden Handler.
- **Plattform-Abstraktion via Protocol** — `HookSwitch` und `Dialer` haben
  jeweils eine Keyboard- (Mac) und eine GPIO-Implementation (Pi).
  `KITELEFON_PLATFORM=mac|pi` schaltet.
- **Live-Tools laufen synchron im Gespräch** (Musik, Einkaufsliste, Rezepte
  generieren + speichern). **Lange Aufgaben werden via `delegate_task` in
  SQLite-Queue** geschrieben — Phase-2-Agenten holen sie dort ab.
- **State-Machine im Realtime-Client explizit** (CONNECTING → SESSION_CREATED
  → LISTENING → USER_SPEAKING → ASSISTANT_RESPONDING → …) — debugbar, und
  Pre-Warming der Session läuft während des Freitons.
- **Jitter-Puffer für Audio-Out**, Half-Duplex auf dem Mac gegen Echo
  (`KITELEFON_HALF_DUPLEX=true`), Voll-Duplex auf dem Pi (echter Hörer
  isoliert Lautsprecher/Mikrofon).

## Live-Integrationen

### Spotify (Duo-Konto-Kaskade)

Steuert per Spotify Connect den **Pure Evoke Play** (Vorgabe) oder die
**Anlage**: das Gerät kommt als Parameter `geraet` (`evoke` | `anlage`) mit,
das Konto folgt der Person (telefon-ai#60). Der Connect-Name je Gerät steht
in `SPOTIFY_DEVICE_NAME` bzw. `SPOTIFY_DEVICE_NAME_ANLAGE`. Mit Spotify Duo
(zwei Konten) erkennt das Telefon **nicht** an der Stimme, sondern in einer
sauberen Kaskade, wer dran ist:

1. Beim Abheben: „Wer ist da?" → Name fürs ganze Gespräch.
2. Selbstnennung im Wunsch: „Hier Florian, spiel meine Abendliste" → Name
   wird direkt mitgegeben.
3. Unbekannt: Handler sucht die Playlist in **beiden** Konten. Genau ein
   Treffer → spielt. Mehrere → KI fragt gezielt zurück. Keiner → KI fragt
   nach dem Konto.

PKCE-OAuth pro Konto via `scripts/spotify_auth.py --account <name>`. Die
erzeugten `token_<name>.json` enthalten langlebige Refresh-Tokens
(automatische Rotation wird persistiert). Für Playlists, die nicht in
`/me/playlists` auftauchen (z. B. von anderen geteilte Listen), gibt's eine
kleine `playlists.json` als Name→URI-Shortcut; beim ersten Musikwunsch prüft
der Dienst jede ID einmal und meldet tote Einträge im Journal. Spotifys
algorithmische Listen (Daylist, Discover Weekly, Release Radar) sind seit
Nov 2024 für Drittanbieter-Apps gar nicht mehr erreichbar — `spotify_play`
sagt das direkt, statt zu suchen (telefon-ai#66).

#### Token erneuern (abgelaufen)

Erkennbar im Journal an `Spotify-Token-Refresh für 'konto2' fehlgeschlagen
(HTTP 400): invalid_grant`; am Telefon sagt die KI, die Anmeldung sei
abgelaufen. Der Flow läuft auf dem Mac, weil der Pi keinen Browser hat:

```bash
# 1. Im Browser bei Spotify ausloggen oder ein privates Fenster nehmen,
#    damit die Anmeldung mit dem richtigen Konto passiert.
cd ~/Development/telefon-ai && uv run python scripts/spotify_auth.py --account konto2
# 2. Datei auf den Pi (Rechte 600 bleiben erhalten, Datei ist gitignored):
scp token_konto2.json kitelefon:~/telefon-ai/token_konto2.json
# 3. Dienst neu starten — die Konten werden beim Start geladen, nicht pro Aufruf:
ssh kitelefon 'sudo systemctl restart kitelefon.service'
```

Prüfen ohne Anruf, über den Werkzeug-Dienst auf dem Pi (Token aus `.env`):

```bash
curl -s -X POST http://127.0.0.1:8081/werkzeuge/spotify_now_playing \
  -H "x-agent-token: $TOKEN" -H 'content-type: application/json' -d '{"wer":"konto2"}'
```

Im Journal muss danach `POST https://accounts.spotify.com/api/token "HTTP/1.1 200 OK"`
stehen. Ein `204 No Content` von `currently-playing` heißt nur „nichts läuft“.

Stand 16.09.2026: `/me/player/devices` listet je Konto nur die Geräte, die
dieses Konto zuletzt angesprochen hat (Konto 1: Evoke, Konto 2: Anlage). Beide
Konten dürfen beide Geräte nutzen, sehen sie aber nicht gleichzeitig. Fehlt
das gewünschte Gerät in der Liste des gewählten Kontos, sagt das Telefon,
was zu tun ist („Die Anlage kennt das Konto von Paula noch nicht. Bitte
verbinde dich einmalig mit deinem iPhone in der Spotify-App mit der Anlage
…") — eine Übernahme ist weder per Web-API noch per Home Assistant noch per
Zeroconf-`addUser` mit unserer App-ID möglich (gemessen 16./18.09.2026), also
wird sie nicht versucht.

### Mise-en-Place (Familien-Haushalts-App)

- **Einkaufsliste:** `add_shopping_item` postet direkt an
  `POST /api/agent/shopping/items` (Bearer-Auth, `AGENT_FAMILY_EMAIL`
  bestimmt die Familie).
- **Rezepte:** `suggest_recipe` ruft die **Claude API** auf der
  Telefon-Seite (Opus 4.7 via `messages.parse()` mit Pydantic-Schema,
  Prompt-Caching für den System-Prompt). Der Entwurf wird im Modul
  zwischen­ge­speichert, die Realtime-KI bekommt eine Zusammenfassung zum
  Vorlesen. Bei Verfeinerungen („weniger Sahne") wird neu generiert.
  Bei Zustimmung postet `confirm_recipe` an
  `POST /api/agent/recipes` — Mise-en-Place ergänzt Nährwerte fire-and-forget.

### Delegation (Phase 2, vorbereitet)

`delegate_task(agent_type, description, deliver_via, deliver_to)` schreibt
nur in SQLite (`tasks.db`). Wer die Queue abarbeitet, ist bewusst kein Teil
dieses Repos — ein allgemeiner, mehrere-Provider-fähiger Agent auf eigener
Hardware ist als separates Vorhaben geplant, `tasks.db` bleibt der stabile
Vertrag dazwischen. Details:
`docs/superpowers/specs/2026-08-09-delegations-schnittstelle-design.md`.

### Wetter und Wohnort

`get_weather` fragt Open-Meteo (ohne Schlüssel). Wetterfragen laufen laut Prompt
immer darüber, nie über `web_suche` (#141). Mit `KITELEFON_WOHNORT=<Ort>` gilt
dieser Ort, wenn der Anrufer keinen nennt oder „hier“, „bei uns“, „zuhause“,
„bei mir“ sagt; der Prompt nennt ihn dann auch. Leer (Standard) bleibt alles wie
vorher: ohne Ort fragt die Figur nach. Der Wert steht wie alle anderen im
1Password-Environment des Repos.

### Websuche und Haushalts-Gedächtnis

- `web_suche` recherchiert im Netz (Claude API mit Websuche) und liefert eine kurze Antwort zum Vorlesen.
- `merke` legt eine Notiz unter einem Thema ab (z. B. „Urlaub Italien“); das Thema steht danach mit Anzahl im Prompt.
- `erinnere` liest die Notizen eines Themas vor.
- `vergiss` löscht Notizen eines Themas, bei mehreren Treffern nichts.

Elektra bekommt alle vier über den Werkzeug-Dienst (unten). Das Tageslimit für Suchen steht in
`KITELEFON_WEBSUCHE_MAX_PRO_TAG` (Standard 50, `0` sperrt die Suche). Dafür muss `ANTHROPIC_API_KEY` gesetzt sein.

### Werkzeug-Dienst für Elektra (rundfunk)

Das Sprachgerät im Wohnzimmer soll alles können, was das Telefon kann
(rundfunk#31). Statt Spotify-Tokens, Mise-en-Place-Schlüssel und Queue-Zugang
zu kopieren, stellt das Telefon seine Werkzeuge über HTTP bereit
(`src/kitelefon/werkzeugdienst.py`): `GET /werkzeuge` liefert die
Function-Calling-Schemas, `POST /werkzeuge/<name>` führt eines aus — mit
denselben Handlern wie im Gespräch. Nicht dabei: `ergebnis_per_mail` (braucht
einen laufenden Rückruf) und der Sprachkurs. Einschalten in der `.env`:

```
KITELEFON_WERKZEUG_PORT=8081
KITELEFON_WERKZEUG_TOKEN=<geheim>     # derselbe Wert als RUNDFUNK_TELEFON_TOKEN auf agent-pi
```

Ohne Token startet der Dienst nicht — er führt Werkzeuge mit unseren
Geheimnissen aus.

### Freisprechen über Elektra (rundfunk, #38)

Der umgekehrte Weg: das Gespräch bleibt beim Telefon (dieselbe OpenAI-Session,
Rolle, Gedächtnis, Werkzeuge), nur der Audioweg wandert aufs Sprachgerät im
Wohnzimmer. Die rundfunk-Bridge verleiht das Gerät über
`ws://<bridge>:8000/verleih` als Netz-Audiogerät — PCM16 mono 24 kHz in
beide Richtungen, der Vertrag steht in `rundfunk/src/rundfunk/verleih.py`.
Hier übernimmt `src/kitelefon/netz_audio.py` (`NetzAudioIO`) mit derselben
Schnittstelle wie `AudioIO`; der `CallManager` tauscht das Backend im
laufenden Gespräch (`audio_umschalten` / `audio_zurueck`). Verliert die
Bridge das Gerät, geht es von selbst zurück in den Hörer; Auflegen beendet
immer und gibt das Gerät zurück. Das Gerät ist halbduplex: während es
spricht, hört es nicht. Einschalten in der `.env`:

```
KITELEFON_RUNDFUNK_VERLEIH_URL=ws://agent-pi.local:8000/verleih
KITELEFON_RUNDFUNK_AGENT_TOKEN=<geheim>   # derselbe Wert wie RUNDFUNK_AGENT_TOKEN auf agent-pi
```

Beides leer = kein Freisprechen: die Werkzeuge antworten dann mit „nicht
eingerichtet", und die Figuren bekommen den Prompt-Abschnitt dazu gar nicht
erst.

**Auslösen**, zwei Wege, beide nur im laufenden Gespräch:

- **Per Sprache:** „schalte auf laut", „Freisprecheinrichtung ein", „mach mal
  laut" → die Figur ruft das Werkzeug `freisprechen` auf und sagt kurz, dass
  ihr jetzt über Elektra redet. „Mach wieder leise" / „zurück zum Hörer" →
  `hoerer_zurueck`. Ist Elektra gerade beschäftigt (Radio, eigenes Gespräch),
  sagt die Figur das mit dem Grund; ist die Bridge nicht erreichbar, „Elektra
  ist nicht erreichbar" — das Gespräch bleibt in beiden Fällen im Hörer.
- **Per Wählscheibe:** die **Ziffer 1** während des Gesprächs schaltet um —
  einmal hin zu Elektra, noch einmal zurück in den Hörer. Alle anderen Ziffern
  tun im Gespräch nichts (die 0 fährt weiterhin nur *vor* dem Gespräch
  herunter). Ziffern, die vor dem Gespräch noch in der Warteschlange lagen,
  werden beim Gesprächsbeginn verworfen. Das gilt nur für die echte
  Wählscheibe am Pi; auf dem Mac liest die Tastatur nur vor dem Gespräch.

Auflegen beendet immer und gibt das Gerät zurück, egal wo das Audio gerade
läuft. Der Werkzeug-Dienst (oben) bietet `freisprechen`/`hoerer_zurueck`
nicht an — von Elektra aus ergeben sie keinen Sinn.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Mindestens OPENAI_API_KEY eintragen.
# Optional: ANTHROPIC_API_KEY (Rezepte), SPOTIFY_* + token_*.json (Musik),
#           KITELEFON_MISE_EN_PLACE_SECRET (Einkaufsliste/Rezepte).

# Spotify einmalig pro Konto (öffnet Browser):
python scripts/spotify_auth.py --account <name>
```

Starten:

```bash
python -m kitelefon
```

**Tasten (Mac-Dev):**

- Leertaste — Hörer abheben / auflegen
- 1–9 — Rolle wählen (siehe `roles.py`: Hamburger, Schwabe, **Camille
  (Französischkurs)**, Verrückter Professor, Gärtnerin, Französischer Koch,
  Tech-Enthusiast, Java-Entwickler aus Indien, Künstliche Intelligenz). Die 8
  spricht Englisch, die 3 Französisch, alle anderen Deutsch.
- Ctrl+C — Beenden

## Auf den Pi

Hardware: Pi 5, USB-Soundkarte, originaler Gabelschalter (GPIO 5),
Wählscheibe (GPIO 26), optional H-Brücke für die echte Klingel.
(Ein CM5-Umzug wurde erprobt und wegen defekter Hardware rückgängig
gemacht — `docs/pi-inbetriebnahme.md` §10/§11.)

```bash
# auf dem Pi:
sudo apt install -y libportaudio2   # System-Lib für sounddevice (nicht im uv-Lock)
uv sync --extra pi                  # gpiozero + lgpio
python scripts/list_audio.py        # prüfen: USB-Karte + default-Gerät sichtbar
```

`KITELEFON_PLATFORM=pi` und `KITELEFON_HALF_DUPLEX=false` (der echte Hörer
isoliert akustisch) setzt `deploy/kitelefon.service` per `Environment=`, nicht
die `.env`. Die `.env` ist auf Mac und Pi dieselbe (1Password-Environment
`telefon-ai/.env`, heim-agent#87).
Wer die App auf dem Pi von Hand startet statt über systemd, setzt beide
Werte selbst (`KITELEFON_PLATFORM=pi KITELEFON_HALF_DUPLEX=false uv run kitelefon`),
sonst gelten die Standardwerte `mac` und Halbduplex.

Audio-Gerät wird **nicht** per Env-Var gewählt — der Code nimmt das
ALSA-`default`. Auf frischem OS einen resampelnden `plug`-Default anlegen
(24 kHz → 48 kHz), sonst `paInvalidSampleRate`. Siehe
`docs/verkabelung.md` → „USB-Soundkarte konfigurieren".

Diagnose-Skripte für die Hardware:

```bash
python scripts/test_gpio.py         # Gabelschalter live (GPIO 5)
python scripts/test_dial.py         # Wählscheibe live (GPIO 26) — zeigt Ziffer
python scripts/test_bell.py         # Klingel: 2 Schläge (GPIO 17/27/22)
python scripts/test_bell_sweep.py   # Klingel-Klang tunen (Frequenz/Duty)
python scripts/test_bell_probe.py   # 5V-Schiene beim Klingeln protokollieren
```

### Werkzeugaufrufe des Modells prüfen (ohne Anruf)

`scripts/werkzeugaufruf_probe.py` lässt die Rolle „Künstliche Intelligenz“
(vollständiger Prompt mit Themenliste, `ALL_TOOL_SCHEMAS`, Modell der Rolle)
gegen die Realtime-API laufen, mit **Text statt Audio**: Satz rein, prüfen,
welches Werkzeug mit welchen Argumenten gerufen wurde und ob der Text „notiert“
sagt, ohne dass ein Aufruf kam. Werkzeuge werden nicht ausgeführt, sie
bekommen feste Fake-Ergebnisse. Gemessen werden Prompt, Schemas und Modell,
nicht Spracherkennung. Nach jeder Änderung daran von Hand fahren, nicht in der
CI und nicht per Cron (Entscheidung in #111). Der Schlüssel kommt aus der
Umgebung, nie in Repo oder Ausgabe:

```bash
set -a; . ~/Development/telefon-ai/.env; set +a
uv run python scripts/werkzeugaufruf_probe.py --wiederholungen 5
# --fall NAME (mehrfach), --ausfuehrlich (Aufrufe und Text je Lauf), --max-laeufe 100
```

Die Fälle stehen als Liste `FAELLE` im Skript. Ausgabe: Quote je Fall und je Zug,
„Zusage ohne Aufruf“, Tokens und Laufzeit; keine Schwelle, Exit-Code nur bei
technischen Fehlern. Ein Lauf kostet rund 10 000 Eingabe-Tokens je Antwort
(der Prompt ist groß); das Konto-Limit von 200 000 Tokens/min fängt das Skript
mit Wiederholung nach Pause ab.

**Mehrzug-Fälle (#116).** Ein Fall ist entweder ein Satz (String, wie bisher)
oder eine Folge von `Zug(satz, erwartet, argument_enthaelt, ...)`. Zug 2 geht
erst raus, wenn die Antwort auf Zug 1 samt Werkzeugaufrufen und Fake-Ergebnissen
vollständig da ist, im selben Gespräch. Ausgewertet wird je Zug, die Fall-Quote
zählt einen Lauf nur als richtig, wenn alle bewerteten Züge richtig sind. Die
Tabelle zeigt unter jedem Mehrzug-Fall eine Zeile je Zug. Ein Zug mit
`informativ=True` (Aufruf und Rückfrage sind beide zulässig) wird erfasst
(„n× ohne Aufruf, Aufrufe: …“), steht aber in keiner Quote. `erwartet=None`
heißt: es darf **kein** Werkzeug kommen (Zug 1 von `vergiss_nach_ja`). Ein
technischer Fehler in einem Zug (z. B. Ratenlimit) bricht das Gespräch ab und
zählt den ganzen Lauf als Fehler; das Ratenlimit wiederholt den ganzen Lauf.
`--max-laeufe` und `--wiederholungen` zählen **Läufe (Gespräche)**, nicht Züge:
5 Wiederholungen von `vergiss_nach_ja` sind 5 Läufe mit 10 Zügen. Mehrzug-Fälle
sind `rueckruf_delegation` (Zug 1 informativ, Zug 2 erwartet `delegate_task`
mit `deliver_via` callback) und `vergiss_nach_ja` (Zug 1 kein `vergiss`, Zug 2
`vergiss` mit Thema Italien). Der Prompt wird ohne Freisprech-Teil gebaut
(`freisprechen=False`), die Produktion übergibt `self._freisprechen`.

## Klingel

Original-Wecker über L298N-H-Brücke, gespeist aus einem MT3608-Step-up
(~27 V) an den 5-V-Pins des Pi. Verkabelung und Messwerte:
`docs/stromversorgung.md`.

```bash
KITELEFON_BELL_ENABLED=true    # Default; auf dem Mac nur Konsolen-Ausgabe
KITELEFON_STARTUP_RING=true    # Default: beim Start einmal klingeln
```

### Auto-Anruf

Nach N Sekunden Ruhe ruft die KI von selbst an — das Telefon klingelt, und
beim Abheben startet eine zufällige Rolle (ohne Wählen). Die Session wird
schon während des Klingelns vorgewärmt, damit das Mikrofon sofort frei ist.

```bash
KITELEFON_AUTOCALL_DELAY_S=30    # 0 = aus (Default)
KITELEFON_AUTOCALL_MAX_RINGS=5
```

## Herunterfahren per Wählscheibe

Sitzt der Pi im Gehäuse, gibt es keine Tastatur mehr. Mit aktivierter Option
fährt **die Ziffer 0** ihn sauber herunter: Abschaltton (drei absteigende
Töne), dann eine Gnadenfrist — **Auflegen bricht ab**.

```bash
KITELEFON_SHUTDOWN_DIGIT_ENABLED=true    # Default: false
KITELEFON_SHUTDOWN_GRACE_S=5
KITELEFON_SHUTDOWN_COMMAND="sudo shutdown -h now"
```

Die 0 ist auf der Wählscheibe der längste Zug, wird also kaum versehentlich
getroffen; die Gnadenfrist ist die zweite Sicherung.

**Passwortloses sudo einrichten** — sonst wartet `shutdown` auf eine
Passworteingabe, die es am Telefon nicht gibt, und es passiert scheinbar
nichts (das Log sagt es dann immerhin):

```bash
echo "$USER ALL=(root) NOPASSWD: /sbin/shutdown" | sudo tee /etc/sudoers.d/kitelefon-shutdown
sudo chmod 440 /etc/sudoers.d/kitelefon-shutdown
```

**Wieder einschalten geht nur über die Stromzufuhr** — ein heruntergefahrener
Pi lässt sich nicht per Wählscheibe wecken. Fürs Umstellen des Telefons ist
genau das der richtige Ablauf.

Der sichere Rückweg bleibt immer:

```bash
ssh kitelefon.local 'sudo shutdown -h now'
```

Default ist **aus** — ein Telefon, das unerwartet von selbst klingelt, will
man bewusst einschalten.

## Autostart (systemd)

Im geschlossenen Gehäuse muss das Telefon nach jedem Stromausfall von selbst
hochkommen. Unit-Datei: `deploy/kitelefon.service`.

```bash
sudo cp ~/telefon-ai/deploy/kitelefon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kitelefon
```

Bedienung:

```bash
systemctl status kitelefon
journalctl -u kitelefon -f          # Live-Log
sudo systemctl restart kitelefon    # nach git pull
sudo systemctl stop kitelefon       # zum Debuggen von Hand
```

Drei Details in der Unit, die man beim Nachbauen leicht übersieht:

- **Voller Pfad zu `uv`.** systemd hat nicht den PATH der Login-Shell,
  `ExecStart=uv …` scheitert mit „No such file or directory".
- **`KillSignal=SIGINT`.** `__main__.py` fängt `KeyboardInterrupt` ab und
  räumt in seinen `finally`-Blöcken auf — vor allem `bell.close()`. Bei
  SIGTERM stirbt Python sofort, und die Klingelspule könnte unter ~27 V
  bestromt stehenbleiben.
- **`StartLimitIntervalSec=0` — und zwar in `[Unit]`.** Die USB-Soundkarte ist beim Boot ein paar
  Sekunden später da als der Dienst; der erste Start darf scheitern. Ohne
  diese Zeile gibt systemd nach fünf Versuchen endgültig auf — und im
  Wohnzimmer sitzt niemand, der `systemctl reset-failed` tippt. In
  `[Service]` wird der Schlüssel kommentarlos ignoriert (nur eine Zeile im
  Journal) — der Dienst läuft dann scheinbar richtig, ohne die Absicherung.

Läuft der Dienst, ist **kein** manuelles `uv run` mehr nötig — beides
gleichzeitig streiten sich um Soundkarte und GPIO-Pins.

Logs in eine Datei tailbar:

```bash
KITELEFON_LOG_LEVEL=DEBUG uv run kitelefon
tail -f kitelefon.log
```

### Detaillierte Doku

- **`docs/pi-inbetriebnahme.md`** — Chronik der Pi-Inbetriebnahme inkl.
  Lessons Learned (Kohlemikrofon, kalte Lötstelle, falsche GU-Pads,
  Prellen, Plattform-Factory-Bug).
- **`docs/verkabelung.md`** — Pinbelegung, Klemmen-Legende FeTAp 611-2,
  Hörer-Anlöten, Klingel über H-Brücke.

## Fernzugriff (Tailscale)

Damit der Pi auch ausser Haus erreichbar ist — `kitelefon.local` geht nur
im selben Netz:

```bash
# einmalig auf dem Pi:
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --hostname=kitelefon
# gibt eine URL aus, im Browser mit dem Tailscale-Konto bestaetigen
```

Danach vom Mac aus `ssh kitelefon` (Eintrag in `~/.ssh/config` mit
`HostName kitelefon.<tailnet>.ts.net`). Der Dienst startet von selbst
mit, das erledigt das Installationsskript.

**Tailscale ersetzt kein Netz.** Es baut nur den Tunnel darueber — der Pi
braucht am fremden Ort weiterhin Internet, also entweder das dortige WLAN
oder den Handy-Hotspot (siehe naechster Abschnitt).

### Neues WLAN unterwegs eintragen

Vor Ort, wenn kein bekanntes Netz da ist — Henne-und-Ei: ohne Netz kein
Tailscale, also muss das direkt am Pi passieren:

```bash
sudo nmcli dev wifi connect "<SSID>" password "<Passwort>"
```

## Telefon mitnehmen (zweites WLAN)

Für Vorführungen ausser Haus lernt der Pi ein zweites WLAN — z.B. den
Hotspot des Handys. NetworkManager nimmt einfach das Netz, das er gerade
findet; `autoconnect-priority` entscheidet nur, wenn beide da sind.

```bash
# Passwort nicht abtippen — sofern es dasselbe ist, holt der Befehl es
# selbst aus dem bestehenden Profil (lesen darf das nur root).
PSK=$(sudo nmcli -s -g 802-11-wireless-security.psk con show "<Heim-WLAN>") \
&& sudo nmcli con add type wifi con-name "<Hotspot>" ifname wlan0 ssid "<Hotspot>" \
     wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" \
     connection.autoconnect yes connection.autoconnect-priority 5 \
&& sudo nmcli con modify "<Heim-WLAN>" connection.autoconnect-priority 10
```

Am iPhone muss **„Für andere zugänglich"** an sein (sonst wird gar keine
SSID gesendet) und **„Kompatibilität maximieren"** ebenfalls — dann funkt
der Hotspot auf 2,4 GHz statt 5 GHz, was durchs Bakelit-Gehäuse deutlich
besser durchkommt.

**Reihenfolge am Vorführort:** erst Hotspot einschalten, dann den Pi
einstecken. Der systemd-Service wartet auf `network-online.target`.

### Stolperstein: `nmcli dev wifi list` lügt ohne root

Als normaler Nutzer liefert die Liste nur den Zwischenspeicher — ein
frisch eingeschalteter Hotspot fehlt darin und sieht aus, als würde er
nicht funken. Das erzwungene Neu-Scannen wehrt polkit mit
`not authorized` ab. Für eine belastbare Aussage immer:

```bash
sudo nmcli dev wifi rescan; sleep 10; nmcli -f SSID,SIGNAL,FREQ dev wifi list
```

Dasselbe gilt für `nmcli con up`: über eine nicht-interaktive SSH-Sitzung
scheitert es an `Not authorized to control networking`.

## Gedächtnis pro Rolle

```bash
KITELEFON_ROLE_MEMORY=true uv run kitelefon
```

Jede Figur ist ein eigener Bekannter: **was Hein weiß, weiß Marlene
nicht.** Das ist Absicht, keine Bequemlichkeit — eine gemeinsame Ablage
wäre nützlicher, würde aber die Fiktion zerstören.

Zwei Haltbarkeiten:

- **Fakten** (Vorlieben, ein paar Personendaten) bleiben, höchstens acht
  pro Rolle. Sie sind der Grund, warum sich ein Wiedersehen wie eines
  anfühlt.
- **Das letzte Gespräch** verfällt nach 14 Tagen. Wer sich nach einem
  Monat an ein beiläufiges Detail erinnert, wirkt nicht vertraut, sondern
  unheimlich.

Nach dem Auflegen destilliert Claude aus dem Transkript beides und legt
es in `tasks.db` (Tabelle `role_memory`) ab. Beim nächsten Anruf steht es
als eigener Abschnitt im System-Prompt, mit der ausdrücklichen Auflage,
es beiläufig zu nutzen und **nicht aufzuzählen**.

Die Obergrenzen sind knapp: das Gedächtnis wandert in den System-Prompt,
und ein aufgeblähter Prompt verwässert genau die Verhaltensregeln
(Antwortlänge, eine Frage pro Antwort, Dialekt-Zurückhaltung).

Default ist `false`, weil es nach jedem Anruf einen Claude-Aufruf kostet.

## Demo-Mitschnitt

Optionaler WAV-Mitschnitt der KI-Antwort jedes Gesprächs (nicht der
Anrufer-Seite):

```bash
KITELEFON_RECORD_CALLS=true uv run kitelefon
# → recordings/YYYYMMDD_HHMMSS_<Rolle>.wav (24 kHz mono PCM16)
```

Default ist `false`. Praktisch für Familien-Videos: Handy filmt das Telefon,
WAV wird parallel mitgeschnitten und später in den Schnitt drübergelegt.

## Französischkurs (Ziffer 3)

Ziffer 3 der Wählscheibe startet eine Gesprächs-Session mit **Camille**,
dem Französisch-Sprachkurs-Assistenten. Der Assistent nutzt das Paket
`sprachkurs` als **Git-Abhängigkeit auf festem Commit** und verwaltet
Fortschritt, Grammatik und Auswertungen in einer eigenen Datenbank.

Die Eröffnung der Figur löst der CallManager erst bei `in_call` aus
(`session.begruessen()`) — vorher lief sie ins Freizeichen und wurde vom
ersten Barge-in abgeräumt. Werkzeuge in `STILLE_WERKZEUGE` (`note_error`)
stoßen nach ihrem Ergebnis keine Antwort an; sprechende Werkzeuge
(`get_weather`, `get_news_fr`) behalten den Nachhol-Mechanismus. Beim
Start eines Sprachkurs-Anrufs werden Wetter (Rennes) und News vorab in
einen Vorrat geholt (30 min bzw. 2 h gültig); die Tool-Aufrufe im
Gespräch antworten dann aus dem Speicher, Fehler werden nie gecacht.

Ein Mikro-Wachhund läuft bei jedem Anruf mit: bleibt der Frame-Nachschub
des Input-Streams länger als `MIC_TOT_S` aus (der Stream liefert auch bei
Stille kontinuierlich — nichts heißt tot), wird er bis zu zweimal neu
geöffnet; PortAudio-Statuswechsel landen jetzt im Log. Schweigt die Lernende im
Sprachkurs länger als 15 s, fragt Camille bis zu zweimal freundlich nach
(„Tu es toujours là ?"), danach wird das Schweigen akzeptiert. Die
Transkriptions-Spur läuft auf `gpt-transcribe` (Whisper-1
halluzinierte englische Sätze ins Protokoll); bewusst ohne
Französisch-Pin, weil Deutsch auf niedrigen Levels erlaubt ist. Seit dem 06.09. prüft er zusätzlich den **Inhalt**: Ein Gerät kann im Takt Frames liefern, in denen nur Nullen stehen — für die Anruferin ist das dasselbe wie ein toter Stream (Sitzung 11). Bleibt das Signal länger als `MIC_STILL_S` aus, wird ebenso neu geöffnet. Die Schwelle `SIGNAL_SCHWELLE` liegt bewusst sehr niedrig: Sie trennt ein totes Gerät von einem stillen Raum, nicht Sprechen von Nachdenken.

### Voraussetzungen

- **`SPRACHKURS_DATABASE_URL`:** in `.env` eintragen (z.B.
  `postgresql://...`). Die Datenbank ist eine separate, von Camille
  verwaltete Instanz.
- **Einmalig bootstrap:** Bevor die erste Session läuft:
  ```bash
  uv run sprachkurs-bootstrap   # im telefon-ai-Verzeichnis; kommt mit sprachkurs
  ```
  Das Skript initialisiert die Tabellen gegen die produktive DB.

### Feature-Flag `KITELEFON_SPRACHKURS_ENQUEUE` und Spool

Jede Sitzung landet normal in **Postgres** (der sprachkurs-DB, siehe
`SPRACHKURS_DATABASE_URL` oben) — Lernstand, notierte Fehler, Transkript.
Der Spool-Ordner (`sprachkurs_spool/`) ist **nur der Fehlerpfad**: schlägt
der DB-Zugriff beim Abschluss fehl (kein Netz, Postgres nicht erreichbar),
schreibt `sprachkurs_call.py` stattdessen eine JSON-Datei dorthin — beim
Scheitern einer einzelnen `note_error`-Schreibung landen dort auch nur die
noch nicht geschriebenen Fehler, nicht die ganze Sitzung erneut. Eine
Logzeile markiert das jeweils (`log.exception` beim DB-Fehler, `log.info`
beim Abschluss inkl. Spool-Pfad). Ein Replay dieser Spool-Dateien ist
**Plan 3**, noch nicht gebaut — bis dahin sammeln sie sich im Ordner.

- **`KITELEFON_SPRACHKURS_ENQUEUE`** (Default: `false`) betrifft nur die
  **Auswertungs-Aufgabe** nach der Sitzung (Mail an die Lernende mit einer
  Zusammenfassung), nicht die Sitzung selbst. Ist `false`, wird diese
  Aufgabe gar nicht erst angelegt. Erst wenn **Plan 3** (heim-agent:
  `konsument`-Spalte, Remote-Auswertungs-Routine) aktiv ist, wird das Flag
  auf `true` gesetzt — dann geht die Auswertungs-Aufgabe an die
  Remote-Queue (oder lokal in `tasks.db`, falls die gerade nicht
  erreichbar ist).

### Kontext-Schnappschuss (Offline-Netz)

Schnappschuss und Fehlerton gehören **zu telefon-ai**, nicht zum
sprachkurs-Paket: `sprachkurs_call.py` schreibt nach jedem erfolgreichen
Sitzungsstart die Datei `sprachkurs_kontext.txt` im Telefon-Verzeichnis neu
(Lernfortschritts-Metadaten aus Postgres). Ist beim nächsten Anruf weder
die Datenbankverbindung noch eine der drei Startabfragen erreichbar, liest
`start_sprachkurs_call()` stattdessen diesen Schnappschuss und startet
**offline**: kein `session_id`, keine Fehler-Persistierung während des
Gesprächs (nur im Speicher gepuffert), beim Auflegen geht alles in den
Spool. Existiert auch kein Schnappschuss, bricht der Anruf mit
`SprachkursNichtVerfuegbar` ab — statt einer stillen Leitung spielt das
Telefon interimsweise einen Fehlerton (`pcm16_shutdown_tone`, wiederverwendet
aus dem Herunterfahr-Signal); eine gesprochene Fehleransage ist noch offen
(Spec §7.6).

### Modell wechseln

Das Realtime-Modell nur für Camille ist über **`KITELEFON_SPRACHKURS_MODEL`**
getrennt einstellbar (Default: `gpt-realtime-2.1-mini`, wie für alle Rollen).
Alle anderen Rollen nutzen `KITELEFON_OPENAI_MODEL`.

### Deploy auf den Pi

Der Pi zieht den Code direkt von GitHub. `sprachkurs` ist eine
Git-Abhängigkeit auf festem Commit (`rev` in `pyproject.toml`); `uv sync`
holt genau diesen Stand, ein Nachbar-Checkout ist nicht nötig.

Einmalig (Git-Zugriff auf das private sprachkurs plus Datenbank-Zugang):

```bash
ssh kitelefon.local
git config --global url."git@github.com:dragondiver/".insteadOf "https://github.com/dragondiver/"
echo 'SPRACHKURS_DATABASE_URL=…' >> ~/telefon-ai/.env  # Wert wie auf dem Mac
```

Dieselbe `insteadOf`-Zeile braucht jeder Mac, auf dem `uv sync` läuft: uv
holt sprachkurs über die https-URL, die Zeile lenkt sie auf den SSH-Key um.

### sprachkurs anheben

Eine neue Camille (Prompt, Kanon, Lernzustand) erreicht das Telefon erst,
wenn telefon-ai den neuen Commit einträgt:

```bash
git -C ../sprachkurs fetch && git -C ../sprachkurs rev-parse origin/main
# den Hash als rev in pyproject.toml [tool.uv.sources] eintragen, dann:
uv lock && uv run --extra dev python -m pytest -q
```

Als PR in telefon-ai, danach `ZIEL=<host> deploy/push.sh`. Wer an beiden Repos zugleich
arbeitet, bindet den lokalen Stand vorübergehend editierbar ein —
`uv pip install -e ../sprachkurs`, danach `uv run --no-sync …` — und
committet das nicht.

Jedes Update:

```bash
ssh -t kitelefon.local 'bash -lc "cd ~/telefon-ai && git pull && uv sync --extra pi && \
  sudo systemctl restart kitelefon && systemctl is-active kitelefon"'
```

(`bash -lc`, weil eine nicht-interaktive SSH-Sitzung sonst `uv` nicht im
PATH hat; `-t`, damit sudo nach dem Passwort fragen kann.)

`deploy/push.sh` macht genau das in einem Aufruf vom Mac aus — mit
Sicherung: Hat telefon-ai auf dem Pi uncommittete Änderungen, bricht es
ab, statt sie zu überschreiben. `KLINGELN=0 ZIEL=<host> deploy/push.sh`
unterdrückt das Funktionsklingeln beim Neustart.

Kein rsync mehr: Ein gespiegelter Stand sieht auf dem Pi wie Dutzende
uncommittete Änderungen aus und blockiert jedes spätere `git pull`.
Ungepushte Zwischenstände testet man über einen gepushten Branch, den man
auf dem Pi auscheckt.

## Tests

```bash
pytest                  # Unit-Tests, schnell
pytest -m live          # echte OpenAI Realtime API (default-skipped)
pytest -m pi            # nur auf Pi-Hardware
```

## Konventionen

- Sprache im Repo: **Deutsch** für UX-Strings, Doku, README. Identifier und
  API-Begriffe in Englisch.
- Keine Secrets im Repo: `.env` und `token_*.json` sind in `.gitignore`.
- Hardware-abhängiger Code immer hinter einer Protocol-Schnittstelle, damit
  am Mac voll entwickelt werden kann.

## Mac-Dev-Stolpersteine

Beide sind in `CLAUDE.md` ausführlich dokumentiert — wenn du beim ersten Test
darüber stolperst:

1. **Mikrofon-Berechtigung fürs Terminal.** Wenn die KI nichts hört obwohl
   Frames rausgehen, hat macOS dem Terminal-Prozess den Mic-Zugriff
   verweigert. Systemeinstellungen → Datenschutz → Mikrofon.
2. **OPENAI_API_KEY in der Shell überschreibt `.env`.** `python-dotenv`
   überschreibt per Default keine bestehenden Env-Vars. Wenn die Auth
   trotzdem failt: `env | grep OPENAI_API_KEY` checken und ggf. `unset`.

## Referenzen

- Make-Magazin „KI-Telefon", Ausgabe 2/26, Dirk Wahl
- Referenz-Fork lokal: `~/Development/refs/KITELEFON/KI-Telefon/`
- Architektur-Details, Hardware-Pinbelegung, OpenAI-GA-Eventnamen: `CLAUDE.md`
