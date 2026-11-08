# Pi-Inbetriebnahme — Chronik

Diese Datei dokumentiert die Inbetriebnahme des KI-Telefons auf dem
Raspberry Pi 5 (4 GB) — von „Pi ist gerade angekommen" bis zum ersten
echten Gespräch mit Hein, dem hamburger Fischhändler.

Zeitraum: **2026-06-06 bis 2026-06-08**. Hardware: FeTAp 611-2 (orange,
Telekom), Pi 5, UGREEN USB-Soundkarte.

---

## 1. Pi-OS und Werkzeug-Setup (06.06.)

Standardweg: aktuelles Raspberry Pi OS 64-bit (Bookworm) per Imager auf
microSD geflasht, `sudo apt update && sudo apt upgrade -y`, dann:

- **uv** als Python-Toolchain (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- SSH-Key generiert und in GitHub deponiert, Repo nach `~/telefon-ai/`
  geklont.
- `uv sync --extra pi` für die Pi-spezifischen Dependencies (gpiozero +
  lgpio).

### Stolpersteine beim ersten `uv sync --extra pi`

`lgpio` kompiliert C-Code beim Install, dabei zwei Build-Fehler die jeweils
ein Apt-Paket brauchten:

```bash
sudo apt install -y swig liblgpio-dev
```

Danach lief der Build durch.

### `.env` auf dem Pi

Mindestens:

```
OPENAI_API_KEY=…
KITELEFON_PLATFORM=pi
KITELEFON_HALF_DUPLEX=false   # echter Hörer isoliert akustisch
```

Plus optional Anthropic, Spotify, Mise-en-Place wie auf dem Mac.

---

## 2. USB-Soundkarte & Audio-Pipeline (07.06., morgens)

UGREEN USB-Soundkarte angesteckt, ALSA hat sie sofort erkannt:

```bash
python scripts/list_audio.py   # zeigt Index, Channels, Samplerate
arecord -l; aplay -l            # zur Gegenprobe
```

`/etc/asound.conf` so gesetzt, dass die USB-Karte der Default ist:

```
defaults.pcm.card 0
defaults.ctl.card 0
```

`alsamixer` (mit F6 Karte auswählen): **Speaker auf 100/0 dB**, Mic-Capture
auf 68, **AGC aus**. Sonst Geflüster oder durchgeregelter Brei.

### Audio-Test war erst mal still

Aufnahme + Wiedergabe-Test über Headset → kein Ton. Diagnose-Schleife:

1. Volume in `alsamixer` war zu niedrig — angehoben, weiterhin still.
2. Kabelwege geprüft, Soundkarte umgesteckt — keine Änderung.
3. Mit Standard-Kopfhörer am Speaker-Out getestet → **laut und klar**.

Heißt: Soundkarte ist gut, das **Problem lag am Klinkenstecker des
Telefon-Hörers** (siehe nächster Abschnitt).

### Kohlemikrofon-Erkenntnis

Wir hatten erwartet, das Kohle-Mikro im FeTAp 611 müsste durch ein
Elektret-Mikro ersetzt werden (Bias-Versorgung). **Falsch:** das UGREEN-
Mic-In liefert genug Bias-Spannung über die Spitze; das original Kohle-Mikro
funktioniert direkt. Eingespart: Ersatz-Kapsel + Lötarbeit.

---

## 3. Telefon-Platine und Hörer-Verkabelung (07.06., nachmittags)

Den Hagenuk-Übertrager und einen weiteren großen Block von der Platine
entfernt — schafft Platz für den Pi im Gehäuse (Bild 6 im Make-Artikel).
**R1 + R2 + C1 bleiben drin**, sie sind im Hörer-Signalpfad.

### Vier Adern vom Hörer auf 3,5-mm-Klinken gelötet

Auf der **Unterseite** der Telefonplatine, originaler Hörer-Stecker bleibt
intakt (siehe Bild 8 im Make-Artikel):

- **weiß-braun** → Mikrofon-Klinke (Mic-In)
- **grün-gelb** → Lautsprecher-Klinke (Headphone-Out)

Litze: 0,14 mm² war völlig ausreichend.

### Der kalte Klinken-Lötpunkt

Erste Mikro-Aufnahme **leise wie ein Pfeifen**, Lautsprecher-Wiedergabe
über den Hörer **komplett still**. Diagnose:

- alsamixer-Pegel war ok, AGC aus, am USB-Stick alles korrekt.
- Standard-Kopfhörer am Speaker-Out: **laut**. Hörer dran: nichts.

→ **Kalte Lötstelle** an einer der Klinken (innen/außen). Stecker
aufgeschraubt, neu verlötet — und plötzlich „laut und klar". Lehrsatz:
wenn ein einzelner Strang in der Kette tot ist, ist meistens die letzte
Lötstelle schuld.

---

## 4. GPIO-Verkabelung Gabelschalter (08.06., morgens)

Dupont-Jumper F-F sind angekommen. Auf der Pi-Seite:

- **Pin 29** (BCM GPIO 5) ← ein blauer Draht
- **Pin 30** (GND, direkt daneben) ← anderer blauer Draht

Die F-Female-Enden der Jumper lassen sich nicht zuverlässig in den
Buchsen halten, wenn man die blauen Drähte nur reinsteckt — also auf der
einen Seite der Buchse abgeschnitten, blanken Draht verzwirnt und verlötet,
mit Schrumpfschlauch isoliert.

### Diagnose mit `scripts/test_gpio.py`

```bash
python3 scripts/test_gpio.py
```

Zeigt live `abgehoben`/`aufgelegt` per `gpiozero.Button(5, pull_up=True)`.
Erster Lauf: **immer `True`** — egal ob Hörer auf oder ab.

### Falsche Lötpads auf der Platine

Wir hatten beide blaue Drähte auf **direkt benachbarte Pads ganz links am
Platinenrand** gelötet. Test mit komplett abgezogenen Drähten + Pi-seitiges
Brücken von Pin 29 + 30: Wert wechselt korrekt. Also: Pi okay, Jumper okay,
Lötstellen okay — die **zwei Pads auf der Platine sind über interne
Leiterbahnen permanent verbunden** und sehen den Gabelumschalter gar nicht.

Lösung: Beide blauen Drähte abgelötet, einer der Drähte auf einen Pad
weiter rechts versetzt — und plötzlich schaltet der Mechanismus. (Bild 8
des Make-Artikels zeigt rückwirkend genau die richtige Position.)

### Invertierte Logik

Bei diesem FeTAp 611 ist die GU-Logik genau anders rum als naiv erwartet:

- Hörer **aufgelegt** → Kontakt **offen** → `is_active = False`
- Hörer **abgehoben** → Kontakt **geschlossen** → `is_active = True`

Das passt zur typischen Pull-Up-Verdrahtung (Button.is_active = Pin LOW
= mit GND verbunden = Kontakt zu) — und damit zu gpiozero's
Default-Semantik. Das Test-Script benennt entsprechend.

### Prellen — `bounce_time = 200 ms`

Der mechanische Schalter prellt heftig. Im Log:

```
20:54:55,203 released -> HUNG_UP
20:54:55,203 pressed -> LIFTED
20:55:19,822 released -> HUNG_UP
20:55:19,822 pressed -> LIFTED
… 6 weitere Wechsel in der nächsten halben Sekunde
```

Ohne Entprellen interpretiert die Call-State-Machine das als ständiges
Auflegen+Abheben und startet Anrufe in Endlosschleife. Fix in
`hookswitch/gpio.py`:

```python
Button(pin, pull_up=True, bounce_time=0.2)
```

200 ms ist die übliche Größenordnung für mechanische Telefon-Gabel.

---

## 5. GPIO-Verkabelung Wählscheibe (08.06., mittags)

Aus dem Stecker auf der Telefon-Platine kommen vier Adern (gelb, grün,
braun, weiß). Wir brauchen nur **zwei**:

- **gelb** → Pin 39 (GND)
- **grün** → Pin 37 (BCM GPIO 26)

Stecker komplett von der Platine abgezogen, gelb + grün an Jumper gelötet,
braun + weiß abgeknipst. Test:

```bash
python3 scripts/test_dial.py
```

Liefert pro Drehung die korrekte Ziffer (10 Impulse = „0"). 80 ms Debounce
im Code reichen — echte Impulse liegen ~95–100 ms auseinander.

---

## 6. Bug: `__main__` ignorierte die Plattform

Erstes `KITELEFON_PLATFORM=pi uv run kitelefon` lief, aber **kein
Freizeichen, keine Reaktion auf den Hörer**. Logfile (DEBUG-Modus) zeigt:
das Programm wartete auf Stdin (Leertaste!), weil `__main__.py` immer
`KeyboardHookSwitch` und `KeyboardDialer` direkt importiert hat — die
`make_hookswitch(platform)`-Factory wurde gar nicht benutzt.

Fix: `__main__.py` benutzt jetzt die Factories und gibt auf Pi keinen
Stdin-Reader hin. Außerdem: Logs werden zusätzlich nach `kitelefon.log`
geschrieben, damit man bei laufendem Programm per SSH `tail -f` machen kann
(`KITELEFON_LOG_LEVEL=DEBUG` für Trace-Detail).

---

## 7. Erstes Gespräch (08.06., 21:00)

Nach dem Bounce-Fix:

```
2026-06-08 21:02:14 INFO ▶ hookswitch lifted
2026-06-08 21:02:14 INFO call state: idle -> dialing
2026-06-08 21:02:18 DEBUG digit received: 1
2026-06-08 21:02:18 INFO selected role: Hamburger
2026-06-08 21:02:18 INFO call state: dialing -> ringback
2026-06-08 21:02:24 INFO call state: ringback -> in_call
```

> „Moin moin, hier ist Hein…"

Funktioniert.

---

## 8. Spotify auf dem Pi

Die `token_<name>.json`-Dateien liegen nicht im Repo (gitignored, enthalten
Refresh-Tokens). Vom Mac kopiert:

```bash
scp ~/Development/telefon-ai/token_florian.json \
    ~/Development/telefon-ai/token_paula.json \
    ~/Development/telefon-ai/playlists.json \
    kitelefon.local:~/telefon-ai/
```

Außerdem ein Inhaltsfehler korrigiert: `SPOTIFY_SUFFIX` in `roles.py`
hatte zwei Kontonamen fest im Code, und der zweite stimmte nicht.

---

## 9. Neue Feature: WAV-Mitschnitt

Für Familien-Demo-Videos ein opt-in Mitschnitt der KI-Antwort:

```
KITELEFON_RECORD_CALLS=true uv run kitelefon
```

Schreibt jede Session als `recordings/YYYYMMDD_HHMMSS_<Rolle>.wav`
(24 kHz mono PCM16). Default ist `false`, weil sonst jedes Gespräch
ungefragt auf Platte landen würde.

Implementation: `call_manager` öffnet das WAV in `_ringback_and_connect`,
schließt es in `_teardown`, schreibt KI-Frames in `_on_audio_from_session`.

---

## 10. Umzug auf Compute Module 5 (2026-07-19) — HISTORISCH

> **Nachtrag 26.07.:** Das CM5 erwies sich als defekt und wurde
> zurückgeschickt. Das Telefon läuft wieder auf dem Pi 5 (§11). Die
> CM5-Spezifika unten (rpiboot, Header-Orientierung, `hw:2,0`) gelten
> für den Pi 5 **nicht** — dort ist die USB-Karte `hw:0,0`.

Wechsel vom Pi 5 auf ein **Raspberry Pi Compute Module 5** (32 GB eMMC) auf
dem **Waveshare CM5-NANO-B** Carrier-Board — kompakt genug, um sauber ins
Telefongehäuse zu passen. Software-seitig ist der CM5 ein Pi 5 (gleicher
SoC BCM2712): Code, `KITELEFON_PLATFORM=pi`, GPIO-Belegung und lgpio-Backend
bleiben **unverändert**.

### eMMC flashen (statt SD-Karte)

Der CM5 mit eMMC wird per `rpiboot`/usbboot über USB-C geflasht:

- Am Mac: `brew install libusb pkg-config make`, dann `usbboot` bauen
  (`git clone --depth=1 https://github.com/raspberrypi/usbboot && make`).
- CM5 in den Flash-Modus: **BOOT-Button gedrückt halten**, dann USB-C an den
  Mac. Das CM5-NANO-B hat einen BOOT-Button onboard (keinen Jumper wie das
  große IO-Board). Dabei **nichts anderes** an die USB-Ports, genug Strom.
- `sudo ./rpiboot` — die eMMC taucht als USB-Massenspeicher auf, dann mit dem
  Raspberry Pi Imager Bookworm 64-bit draufschreiben.

### Migration statt Neu-Setup

Nur die gitignoreten Dateien vom alten Pi rüberkopiert: `.env`,
`token_*.json`, `tasks.db`. Locale/TZ im Imager übersprungen → nachgezogen:

```bash
sudo timedatectl set-timezone Europe/Berlin
sudo localectl set-keymap de
```

### Fresh-OS-Stolpersteine (System-Libs sind nicht im uv-Lock)

`uv sync` installiert nur die Python-Pakete. System-Bibliotheken müssen aufs
frische OS neu — sonst bricht `list_audio.py` sofort ab
(`OSError: PortAudio library not found`):

```bash
sudo apt install -y libportaudio2
```

(Beim ersten `uv sync --extra pi` wieder `swig liblgpio-dev` wie beim Pi 5.
`uv` selbst ist auf dem frischen OS auch noch nicht da → Installer-Skript.)

### GPIO-Orientierung am CM5-NANO-B per Multimeter bestimmt

Der 40-Pin-Header ist Pi-kompatibel (gleiche BCM-Nummern), aber die Lage von
Pin 1 auf dem kleinen Board ist nicht offensichtlich. Statt zu raten: Board
mit Strom, Multimeter auf V⎓, schwarze Spitze an GND (Metallschirm einer
USB-/Ethernet-Buchse), rote Spitze an die Header-Enden. Das Ende mit **3,3 V
(Pin 1) + zwei 5-V-Pins (Pin 2/4)** ist der Anfang. Ergebnis hier: **Pin 1
oben** (Richtung Audio-Klinke/PWR-BOOT-Buttons), alle Signal-GPIOs in der
„gelben" Spalte. Gabelschalter + Wählscheibe danach 1:1 wie am Pi 5
verkabelt, mit `test_gpio.py`/`test_dial.py` bestätigt.

### Der 24-kHz-ALSA-Fallstrick (Kernpunkt des Umzugs)

Erstes Telefonat brach beim Öffnen des Audio-Streams ab:

```
Expression 'paInvalidSampleRate' failed in pa_linux_alsa.c
sounddevice.PortAudioError: Error opening RawOutputStream: Invalid sample rate
```

Ursache: Die USB-Soundkarte kann in Hardware nur **48000 Hz**, der Code
öffnet aber **24000 Hz** (natives OpenAI-Realtime-Format, bewusst kein
Resampling im Code). Auf einem frisch geflashten OS gibt es **kein
ALSA-`default`-Plug**, das umrechnet — PortAudio öffnet `hw:2,0` direkt und
ALSA lehnt 24 kHz ab.

Warum es auf dem alten Pi lief: dort machte das `/etc/asound.conf`
(`defaults.pcm.card`) die USB-Karte zum ALSA-`default`, und der globale
`default` ist ein `plug` → resampelt automatisch. Diese Config kam beim
Umzug nicht mit (nicht im Repo).

**Fix:** expliziten `plug`-Default anlegen (`~/.asoundrc` oder
`/etc/asound.conf`):

```
pcm.!default {
    type plug
    slave.pcm "hw:2,0"
}
ctl.!default {
    type hw
    card 2
}
```

`plug` resampelt 24 kHz → 48 kHz für Wiedergabe **und** Aufnahme. Danach
zeigt `list_audio.py` zusätzlich ein `default`-Gerät (128/128 ch) und der
Stream öffnet. `hw:2,0` = die per `aplay -l` gemessene Kartennummer; ändert
sich die (anderer USB-Port), Config anpassen.

### Audio-Gerät: keine `AUDIO_*`-Env-Vars

Beim Debuggen bestätigt: `audio.py` öffnet die Streams **ohne** `device=`.
Es gibt **kein** `AUDIO_INPUT_DEVICE`/`AUDIO_OUTPUT_DEVICE` im Code (die
Docstring in `list_audio.py` war irreführend, korrigiert). sounddevice nimmt
das PortAudio-Default-Gerät — also genau das `plug`-`default` von oben. Die
Kanalwahl passiert über ALSA (`asound.conf`), nicht über die `.env`.

---

## 11. Rückkehr zum Pi 5 (2026-07-26)

Das CM5 war defekt und ging zurück. Wiederinbetriebnahme auf dem
**Pi 5 Model B Rev 1.1** mit frischem OS (Debian 13 „trixie").

### Verkabelung wiederhergestellt

Wählscheibe (GPIO 26) und Gabelschalter (GPIO 5) neu gesteckt und mit
`test_dial.py` / `test_gpio.py` verifiziert — beide sofort sauber
(Aderfarben sind bei beiden egal, simple Schaltkontakte).

### ALSA-Mixer stand auf Werkszustand — drei Symptome, eine Ursache

Erster Testlauf: kein Freizeichen. Danach: alles zu leise (Freiton,
Wählton, KI-Stimme) und die KI fühlte sich ständig unterbrochen
(Fehl-Barge-in), obwohl der Anrufer still war. Software-Diagnose war
unauffällig (kein Fehler im Log, Stream öffnet, 4 Gespräche liefen
zwischenzeitlich). Der Code und die `.env` waren seit Wochen unverändert —
**geändert hatte sich nur das OS**, und die Mixer-Level der USB-Karte
standen wieder auf Werkszustand:

- **Speaker 38 % (-23 dB)** statt 100 % → kein/kaum hörbarer Ton.
- **Mic-Capture 100 % (+23 dB)** statt ~68 %.
- **AGC an** statt aus → „durchgeregelter Brei", zu leise trotz 100 %
  Speaker, und vermutlich VAD-Fehltrigger.

Fix (Werte aus §2 / `verkabelung.md`):

```bash
amixer -c 0 sset 'Speaker' 100% unmute
amixer -c 0 sset 'Mic' 68%
amixer -c 0 sset 'Auto Gain Control' off
sudo alsactl store   # persistiert die Level über Reboots
```

**Lehrsatz:** Die Mixer-Level leben im OS, nicht im Repo. Nach jedem
OS-Flash gehören sie zur Checkliste — Symptome „zu leise" oder
„KI unterbricht sich ständig selbst" zuerst hier suchen, nicht im Code.

### Offen (Stand 26.07.)

- **Fehl-Barge-in unter Beobachtung:** Auch nach dem Mixer-Fix meldete
  der Server-VAD teils `speech_started` ~270 ms nach Beginn der
  KI-Ausgabe, obwohl der Anrufer still war (Übersprechen im Hörer?).
  Kandidat: `turn_detection.threshold` in `realtime.py` von 0.5 anheben.

## Lessons Learned (Kondensat)

1. **Erst messen, dann tauschen.** Wir hätten fast die Kohlekapsel
   ersetzt — sie funktioniert direkt.
2. **Eine kalte Lötstelle reicht.** Wenn ein Audio-Pfad still ist, immer
   die letzte Lötstelle prüfen, nicht im Treiber suchen.
3. **PCB-Pads sind nicht der Schalter.** Pads die nebeneinander liegen,
   können über interne Leiterbahnen permanent verbunden sein — den
   eigentlichen GU-Schaltkontakt muss man mechanisch identifizieren
   (Mechanismus drückt direkt drauf).
4. **`bounce_time` bei mechanischen Switches ist Pflicht.** 80 ms für
   Wählscheibe, 200 ms für Gabel.
5. **Plattform-Factories konsequent benutzen.** Direktimporte in `__main__`
   hebeln das Plattform-Switching aus.
6. **Logging-File zusätzlich zur Konsole.** Beim Pi via SSH sieht man
   sonst die Logs nur, solange der Vordergrund-Prozess läuft — eine
   File-Logfile macht `tail -f` und Post-mortem-Diagnose möglich.
7. **System-Libs sind nicht im uv-Lock.** Nach einem OS-Flash fehlen
   `libportaudio2` & Co. — `uv sync` bringt nur die Python-Pakete. Bei
   „läuft auf dem alten Gerät, nicht auf dem neuen" zuerst die
   System-Bibliotheken prüfen.
8. **24 kHz braucht einen ALSA-`plug`-Default.** Die USB-Karte kann nur
   48 kHz; ohne resampelnden `default` scheitert der Stream mit
   `paInvalidSampleRate`. Auf frischem OS explizit `pcm.!default type plug`
   setzen — nicht nur `defaults.pcm.card`.

## Fotos

- `IMG_3107.png` — Pi 5 von oben (zur GPIO-Orientierung)
- `IMG_3108.png` — Platinen-Unterseite, **Wiederfindung** der blauen Drähte
  ganz links → das war die falsche Position
- `IMG_3109.png` — Platinen-Oberseite mit Gabelumschalter-Mechanismus

## Klingel funktioniert (29.07.)

`scripts/test_bell.py` (bipolar 10 Hz, 50 % Duty) klingelt hörbar.
MT3608 auf **12 V** eingestellt (statt anfangs 16–18 V — mehr braucht die
Klingel nicht, Spule misst ~1,8 kΩ, zieht also nur ~7 mA).

**Debugging-Historie / Lessons Learned:**

- Anfangs stürzte der Pi bei `test_bell_static.py` **und beim bloßen
  Messen am bestromten Aufbau** komplett ab (harter Reset). Ursache war
  nicht die Spulenlast (7 mA können nichts crashen), sondern ein Fehler
  im Leistungspfad MT3608 → L298N: Kurzschluss-artige Zustände beim
  Hantieren rissen die Pi-5V-Schiene runter (Netzteil hat 5 A
  ausgehandelt, war also nicht die Ursache).
- **Regel:** Beim Hardware-Debugging den Leistungspfad vom Pi trennen;
  Widerstände nur stromlos messen; Step-up-Poti nur mit separater
  Versorgung (USB-Netzteil) einstellen.
- Diagnose-Helfer im Repo:
  - `scripts/test_bell_probe.py` — 3-stufiger Lasttest (50 %/50 %/100 %
    Duty), loggt die 5V-Schiene (PMIC `EXT5V_V`) crash-fest mit fsync
    nach `scripts/bell_probe.log`.
  - `scripts/test_bell_hold.py` — hält die Spule bestromt (Strg+C zum
    Beenden) für Multimeter-Messungen an VS / 5V-Klemme / OUT1↔OUT2 /
    Spule.
- Referenz (`bell.py` im Original-Repo, auf dem Pi unter
  `/home/florian/KI-Telefon`) fährt ENA **nie statisch auf 100 %**:
  1-kHz-PWM bei 50 % Duty, Polaritätswechsel 10 Hz, 3 ms Totzeit.

## Was noch fehlt (Stand 29.07.)
- **Autostart als systemd-Service** — manuelles `uv run` reicht für jetzt.
- **Auto-Anruf-Feature** (KI ruft selbst an wenn 30 s Stille) — Referenz hat
  es, wir noch nicht.
- **`spotify_list_playlists`-Tool** — die KI weiß nicht, welche Playlists
  es gibt, kann auf „welche Playlists habe ich?" nicht antworten.
