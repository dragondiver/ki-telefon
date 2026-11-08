# Hardware-Einbau: Technische Dokumentation

> **Stand 2026-08-01 — im Einsatz ist der Raspberry Pi 5** mit aktivem Kühler
> (Active Cooler) und einer C-Media-USB-Soundkarte (Unitek Y-247A, `0d8c:0014`),
> per udev auf den Namen `telefon` gepinnt. Das CM5-NANO-B ist hardware-defekt
> und ausgemustert (§11) — Abschnitte, die es erwähnen, sind historisch.

## 1. Mikrocontroller vs. SBC — Entscheidung

### ESP32 als Alternative (verworfen)

Der ESP32 ist für dieses Projekt **nicht geeignet**. Kernprobleme:

- Kein Python 3.11 (MicroPython ist kein Ersatz)
- Keine offiziellen SDKs: OpenAI, Anthropic, Spotify
- Kein `asyncio` in der benötigten Form
- Kein SQLite

Selbst der ESP32-P4 (leistungsstärkste Variante) würde eine vollständige Neuentwicklung in C erfordern. **Entscheidung: Weiterhin Pi / CM5.**

---

## 2. SBC-Vergleich (Stand Juli 2026)

| Board | Preis (DE) | Bemerkung |
|---|---|---|
| Raspberry Pi 4B 4GB | ~58 € | Sweet Spot, GPIO nativ kompatibel, bei dt. Händlern verfügbar |
| Raspberry Pi 5 4GB | ~75–90 € | Zukunftssicher, lgpio-Shim für RPi.GPIO nötig |
| Raspberry Pi 5 8GB | ~185 € | Durch RAM-Knappheit stark verteuert |
| Orange Pi 5 | ~200 € | Nur Import, GPIO-Portierung nötig |
| Radxa ROCK 5B | ~175–300 € | Kein onboard WiFi, GPIO-Portierung nötig |
| Banana Pi M5 | — | RPi.GPIO-inkompatibel |
| ~~CM5-NANO-B (Waveshare)~~ | — | 55×41 mm, war ideal für den FeTAp-Einbau — **defekt, ausgemustert (§11)** |
| **Raspberry Pi 5 (im Einsatz)** | — | **85×56 mm, mit Active Cooler — Bauhöhe vor dem Bohren prüfen (§3)** |

**Wichtig Pi 5 / CM5:** `RPi.GPIO` ist auf dem neuen RP1-Chip **nicht mehr lauffähig**.
Wir nutzen `gpiozero` mit `lgpio`-Backend (Bookworm-Default). Onboard-Audio entfällt
beim Pi 5 → USB-Soundkarte ist Pflicht.

---

## 3. Einbau ins FeTAp 611-2

### Platzierung

- **Raspberry Pi 5 (85×56 mm): passt eng** — aktuell im Einsatz
- CM5-NANO-B (55×41 mm): passte komfortabel, aber defekt (§11)

**Trockenprobe vor jedem Bohren und Kleben.** Board mit montiertem Kühler an
den geplanten Platz legen und das Gehäuse einmal komplett schließen. Der
Active Cooler baut rund 10 mm nach oben auf, und schon ohne Kühler ist es beim
Pi 5 eng. Die eigentlichen Höhenfresser sind meist nicht der Deckel, sondern
**Wählscheibe und Wecker**, die von oben in den Innenraum ragen.

### Befestigung

- **M3 Messing-Abstandshalter, 3–4 mm Höhe** — Board nie flach auflegen (Wärmestau)
- **M3 Senkkopfschrauben** für minimale Kopfhöhe
- Alternative: selbstklebende Gummi-Abstandspads, 2–3 mm

### Kühlung

**Im Einsatz: offizieller Active Cooler** (Lüfter + Kühlkörper).

- Lüfterkabel an den 4-poligen JST-Anschluss zwischen GPIO-Leiste und USB-C —
  nicht auf GPIO-Pins.
- Push-Pins brauchen spürbar Druck: **Platine beim Eindrücken von unten
  abstützen**, sonst biegt sie sich durch und lockert Steckverbindungen.
- Danach alle GPIO-Drähte nachkontrollieren (§11: Wackler zeigen sich nicht als
  Wackler, sondern als „Pi stürzt ab" oder „Klingel stumm").
- Lüftungsschlitze in den Gehäuseboden bleiben sinnvoll (Luftweg für den Lüfter).

**Kontrolle:**
```bash
vcgencmd measure_temp                       # Leerlauf ~42 °C mit Cooler
ls /sys/class/hwmon/*/pwm1                  # Lüftersteuerung vorhanden?
```

### Kabelführung

- USB-C-Eingang zeigt zur Gehäuserückwand → Loch bohren
- GPIO-Drähte führen nach vorne zur FeTAp-Platine

### CM5-NANO-B: Pin-1-Orientierung

Der 40-Pin-Header ist Pi-kompatibel (gleiche BCM-Nummern wie Pi 4/5).
**Pin 1 (3,3 V) liegt oben, Richtung Audio-Klinke.** Die Signal-GPIOs
(BCM 5, 17, 22, 26, 27) liegen alle in der 3,3-V-Spalte — Doppelcheck
mit `pinout` vor dem Löten.

### CM5: eMMC flashen

```bash
# BOOT-Button auf NANO-B gedrückt halten, dann USB-C einstecken
sudo rpiboot               # erkennt Modul als USB-Massenspeicher
# Dann mit rpi-imager oder dd flashen
```

---

## 4. FeTAp-Platine: Bestückung reduzieren

### Bereits entfernt

- Hagenuk-Übertrager
- Großer Block
- GGs-Position ist leer

### Sicher entfernbar

| Bauteil | Beschreibung | Hinweis |
|---|---|---|
| C2 | 0,1 µF Kondensator | Entfernung bestätigt |
| W2 | Flachstecker-Leiste (Wählscheibe) | **Vor dem Auslöten fotografieren!** Wählscheibe danach direkt verdrahten |
| V1 | Varistor (Leitungsschutz) | Funktion obsolet im Projekt |
| K1 | Blade-Kontakte | Keine Drähte angeschlossen |

### Behalten

| Bauteil | Beschreibung |
|---|---|
| R1, R2, C1 | Hörer-Signalpfad — nicht entfernen |
| Kontakte 2/3/4 | Gabelschalter-Federkontakt |

### Platine komplett entfernen

**Nicht empfohlen.** Der Gabelschalter-Federkontakt drückt mechanisch auf Platinen-Pads — ohne Platine verliert man diesen Kontaktpunkt.

---

## 5. GPIO-Pinbelegung (BCM)

| Komponente | BCM-GPIO | Pi-Header-Pin | Hinweis |
|---|---|---|---|
| Gabelschalter | GPIO 5 | Pin 29 | Pull-Up intern, HIGH = abgehoben |
| GND Gabelschalter | GND | Pin 30 | direkt daneben |
| Wählscheibe (Puls) | GPIO 26 | Pin 37 | FALLING-Edge zählt Impulse |
| GND Wählscheibe | GND | Pin 39 | direkt daneben |
| H-Brücke IN1 | GPIO 17 | Pin 11 | Klingel-Richtung A |
| H-Brücke IN2 | GPIO 27 | Pin 13 | Klingel-Richtung B |
| H-Brücke ENA | GPIO 22 | Pin 15 | PWM 1 kHz |

**Schnell-Test ohne Telefon:** Pin 29 (BCM 5) und Pin 30 (GND) mit Draht
überbrücken → simuliert abgehobenen Hörer.

---

## 6. Gabelschalter (GPIO 5)

- 2 Kabel (blau im Make-Artikel) **von der Platine** abgreifen
- → GPIO 5 (Pin 29) und GND (Pin 30, direkt daneben)

### ⚠ PCB-Pad-Falle

Nicht alle Pads am Platinenrand sind der Schaltkontakt. Benachbarte Pads
können über interne Leiterbahnen permanent miteinander verbunden sein und
sehen den mechanischen Kontakt nie. Die **korrekten GU-Pads liegen weiter
mittig**, wo der Federkontakt des Gabelumschalters direkt drückt.

**Vor dem endgültigen Löten verifizieren:**
```bash
python scripts/test_gpio.py
```
Hörer drücken vs. loslassen muss `is_active` umschalten. Wenn der Wert
nicht kippt → falsche Pads.

### Prellen

Der Schalter prellt heftig (5–10 Events pro Schaltvorgang). `GpioHookSwitch`
setzt deshalb `bounce_time=0.2` (200 ms). Ohne Entprellen startet die
Call-State-Machine Endlosschleifen.

### Invertierte Logik

Bei diesem FeTAp-611-Exemplar: `Kontakt geschlossen = abgehoben`.
Mit internem Pull-Up: `is_active=True` = Hörer oben.

---

## 7. Wählscheibe (GPIO 26)

Aus der Wählscheibe kommen 4 Kabel über Stecker auf die Platine.
Nur 2 davon zum Pi führen:

- **gelb** → GND
- **grün** → GPIO 26

Stecker komplett von der FeTAp-Platine abziehen und die 2 Kabel direkt
zum Pi führen. `bounce_time=0.08` (80 ms) im `GpioDialer` — echte
Wählimpulse liegen ~95–100 ms auseinander, kürzer = Prellen.

---

## 8. Klingel — H-Brücke L298N

### Benötigte Komponenten

- **H-Brücke L298N**
- **Step-up-Wandler MT3608** 5 V → ~27 V (speist die Klingelspule).
  Er hat nur Lötpads, keine Klemmen — sauber verlöten, siehe §11.
- Original-Klingelspule des FeTAp bleibt drin (1654 Ω, **polarisiert**)

### Verdrahtung

```
Pi GPIO 17  →  L298N IN1
Pi GPIO 27  →  L298N IN2
Pi GPIO 22  →  L298N ENA   (PWM 1 kHz)
Pi 5V       →  L298N +5V-Eingang (Logikversorgung)
Step-up 27V →  L298N +12V-Eingang (Leistungsversorgung)
L298N OUT1+OUT2 →  Klingelspule (Kanal A, Spule dazwischen)
```

Der Step-up wird — wie bei Dirk Wahl — **aus den 5 V des Pi** gespeist. Die
Spule zieht dabei nur ~130 mA auf der 5-V-Seite; ein separates Netzteil ist
nicht nötig (die widerlegte Gegenthese steht in `docs/stromversorgung.md`).

### ⚠ Beide Jumper entfernen

- **ENA-Jumper** ab, damit GPIO 22 den Enable-Eingang steuert.
- **5V-EN-Jumper** ab, sonst läuft der Onboard-Regler an den ~27 V am
  +12V-Eingang. Die Logik wird dann separat über den +5V-Eingang versorgt.

### ⚠ Polarisierte Spule + Tastgrad

Der Wecker ist **polarisiert** (Permanentmagnet): Er bewegt sich nur bei
**Umpolung**, nicht bei statischem Gleichstrom — der gibt höchstens ein
einzelnes „Klick".

Und: **volles Duty verwenden.** Bei 50 % PWM bleiben von 27 V effektiv ~13 V,
das sind ~8 mA durch 1654 Ω — zu wenig, die Klingel bleibt *völlig stumm*,
obwohl am Messgerät 26 V anliegen. Mit 100 % Duty fließen ~16 mA und sie
schlägt an.

### Frequenz

**25 Hz** — die originale Rufwechselspannung der Deutschen Post, auf die der
Wecker mechanisch abgestimmt ist. Klingt hörbar besser als die 10 Hz der
Referenzimplementierung. Zum Nachstimmen:
`python scripts/test_bell_sweep.py --freqs 10,20,25 --duty 1.0`.

### Software

PWM 1 kHz, bipolare 25-Hz-Wechselspannung bei vollem Duty → authentischer
„Rrring"-Klang. Implementierung: `src/kitelefon/bell/gpio.py` (`GpioBell`),
mit `ring_until_answer` für den Auto-Anruf. Referenz-Fork zum Vergleich:
`bell.py` in `~/Development/refs/KITELEFON/KI-Telefon/` (dort 10 Hz).

---

## 9. Audio — USB-Soundkarte (C-Media, aktuell Unitek Y-247A)

### Warum USB-Soundkarte

Pi 5 / CM5 hat kein Onboard-Audio. Das Telefon-Handset wird direkt angeschlossen:

- **Sprechmuschel (Mikrofon):** Kabel weiß-braun → 3,5-mm-Klinkenstecker → USB-Karte Mic-In
- **Hörmuschel (Lautsprecher):** Kabel grün-gelb → 3,5-mm-Klinkenstecker → USB-Karte Headphone-Out
- **Kohlemikrofon:** funktioniert direkt mit der C-Media-Karte — kein Elektret-Ersatz nötig

### 24-kHz-Problem und ALSA-Plug

Die Karte kann hardwareseitig nur 44,1/48 kHz. Der Code öffnet aber
**24 kHz** (natives OpenAI-Realtime-Format). Ohne Konfiguration → `paInvalidSampleRate`.

Lösung: Resampelndes `plug`-Gerät als ALSA-Default anlegen:

```bash
sudo nano /etc/asound.conf
```

```
pcm.!default {
    type plug
    slave.pcm "hw:telefon,0"
}
ctl.!default {
    type hw
    card telefon
}
```

**Nie die Kartennummer eintragen, immer den Namen** (s. udev-Regel unten).
Belegt am 2026-08-01: Die Karte war `2`, nach einem Reboot `0`, nach dem
Wiedereinstecken wieder `2`. Eine feste Nummer bedeutet Stille beim nächsten
Start — im zugeschraubten Gehäuse besonders ärgerlich.

Eine vorgefundene Stock-Datei mit `defaults.pcm.card 0` reicht **nicht**: Sie
zeigt auf die falsche Karte *und* legt kein resampelndes `plug`-Gerät an.

**Wichtig:** Datei nach `/etc/asound.conf`, nicht `~/.asoundrc` — bei
aktivem Overlay-FS wird `/home` bei Reboot verworfen (s.u.).

### udev-Regel: Kartennummer pinnen

Ohne udev-Regel bekommt die USB-Karte beim Reboot eine andere Nummer. Die
Regel vergibt stattdessen einen festen **Namen** über `ATTR{id}`:

```bash
sudo nano /etc/udev/rules.d/85-kitelefon-audio.rules
```

```
SUBSYSTEM=="sound", ACTION=="add", ATTRS{idVendor}=="0d8c", ATTRS{idProduct}=="0014", ATTR{id}="telefon"
```

`idVendor`/`idProduct` aus `lsusb` ablesen (hier: C-Media / Unitek Y-247A).

Danach: `sudo udevadm control --reload && sudo udevadm trigger`

**Verifikation nur über einen echten Reboot** — alles andere beweist nichts:

```bash
cat /proc/asound/cards          # muss "[telefon]" zeigen
python scripts/list_audio.py    # muss zusätzlich "default" listen
```

Und der ehrliche Abschluss: ein Loopback aufnehmen und zurückhören. Dass der
Fehler weg ist, heißt noch nicht, dass Ton fließt.

```bash
uv run python -c "import sounddevice as sd; r=sd.rec(int(3*24000),samplerate=24000,channels=1,dtype='int16'); sd.wait(); sd.play(r,samplerate=24000); sd.wait()"
```

**Ist die Geräteliste leer** (`Error querying device -1`), steckt meist schlicht
die USB-Karte nicht — nach jeder mechanischen Arbeit am Gehäuse zuerst das
prüfen, bevor man Software verdächtigt.

### alsamixer-Einstellungen

```bash
alsamixer   # F6 → Karte "telefon" auswählen
```

| Regler | Wert | Hinweis |
|---|---|---|
| Speaker | 100 (0 dB) | Lautsprecher |
| Mic Capture | ~68 | Mikrofon-Eingang |
| Auto Gain Control | **AN** | Für Kohlemikro + C-Media-Chip nötig |

```bash
sudo alsactl store    # Einstellungen persistent speichern
```

**Achtung:** In der ursprünglichen Pi-4-Doku stand „AGC aus". Das gilt
für andere Hardware-Kombinationen. Mit der C-Media-Karte + Kohlemikrofon des FeTAp 611
muss AGC **an** sein.

### „Kalte Lötstelle"-Symptom

Audio-Pfad still obwohl Pipeline läuft, Karte korrekt, alsamixer hochgeregelt?
→ Kalte Lötstelle am Klinkenstecker. Mit Standard-Kopfhörer am Headphone-Out
vergleichen: wenn der laut ist, der Hörer aber nicht → Klinkenstecker neu verlöten.

---

## 10. Overlay-FS-Falle (CM5 / Pi 5)

`raspi-config` → Performance → Overlay File System kann aktiv sein (`get_overlay_now=1`).
Damit werden `/home`-Änderungen bei Reboot verworfen:

- `~/.asoundrc` → weg
- `tasks.db` → weg
- `token_*.json` (Spotify) → weg

**Prüfen:**
```bash
sudo raspi-config nonint get_overlay_now   # 0 = aus, 1 = an
```

**Deaktivieren:** `sudo raspi-config` → Performance → Overlay FS → Nein

Alle persistenten Konfigurationsdateien nach `/etc/` (ALSA-Config) oder
ins Projekt-Verzeichnis auf einer echten Partition schreiben.

---

## 11. Bekannte Hardware-Probleme

### CM5-NANO-B Hardware-Defekt (2026-07-19)

Das erste NANO-B-Board ist nach einem harmlosen Audio-/Config-Befehl
ausgefallen (nur rote LED, kein Boot, `rpiboot` erkennt nichts). Diagnose:
latenter HW-Defekt oder Stromglitch, kein Zusammenhang mit der Klingelschaltung
(war noch nicht angeschlossen). Board wahrscheinlich defekt (nicht das CM5-Modul).

**Code und Konfiguration sind vollständig in git und auf dem alten Pi 5 vorhanden.**

Optionen:
1. Neues NANO-B (~20 €) bestellen, CM5-Modul umsetzen
2. Auf alten Pi 5 zurückbauen (Soundkarte + GPIO-Drähte umstecken)

**Umgesetzt: Option 2.** Garantie/RMA für das Board angemeldet, Arbeit läuft
seither auf dem alten Pi 5.

Wiederaufbau: `docs/pi-inbetriebnahme.md` §10 — alle Schritte dokumentiert.

### Lose Kontakte sehen nie aus wie lose Kontakte

Zweimal führten Wackler zu Symptomen, die woanders hindeuteten:

- **Pi geht komplett aus** (kein Reboot) bei `get_throttled` = `0x0` → das ist
  ein Kontakt- oder Kurzschlussproblem, **kein** Netzteilproblem. Ursache waren
  lose Lötstellen am MT3608-Step-up (nur Pads, keine Klemmen). Details und die
  widerlegte Netzteil-These: `docs/stromversorgung.md`.
- **Audio komplett tot nach Schraubarbeiten** → USB-Karte hatte sich gelöst.

Merke: erst messen und die Steckverbindungen prüfen, dann Teile kaufen.

---

## 12. Bring-up-Checkliste (neue Hardware)

Für schnellen Wiederaufbau nach Boardwechsel — in dieser Reihenfolge:

- [ ] eMMC flashen (BOOT-Button + `rpiboot` + `rpi-imager`)
- [ ] `libportaudio2` per apt installieren (nicht im uv-Lock)
- [ ] `git clone` + `uv sync`
- [ ] `.env` und `token_*.json` vom alten System kopieren
- [ ] `/etc/udev/rules.d/85-kitelefon-audio.rules` anlegen
- [ ] `/etc/asound.conf` mit `plug`-Default anlegen
- [ ] `alsamixer` einstellen + `sudo alsactl store`
- [ ] Overlay-FS deaktivieren (`raspi-config`)
- [ ] Reboot — und danach prüfen, dass die Karte noch `telefon` heißt
- [ ] `python scripts/test_gpio.py` — Gabelschalter-Pads verifizieren
- [ ] `python scripts/test_dial.py` — Wählscheibe zählt richtige Ziffern
- [ ] `python scripts/list_audio.py` + Loopback — Karte, `default`, echter Ton
- [ ] `python scripts/test_bell.py` — Klingel schlägt an (⚠️ laut)
- [ ] `uv run --extra dev pytest -q` — Test-Suite grün
- [ ] Ersten Anruf tätigen

**Nach jeder mechanischen Arbeit** (Kühler montieren, löten, schrumpfen,
Gehäuse schließen) die vier Hardware-Checks wiederholen — sie dauern zwei
Minuten und finden gelockerte Verbindungen, bevor das Gehäuse zu ist.
