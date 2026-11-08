# Verkabelung FeTAp 611-2 ↔ Raspberry Pi

Extrakt aus Make 2/26 (Dirk Wahl) — kombiniert mit unseren Konventionen.
Einen vereinfachten Schaltplan des FeAp 611 gibt es auf Wikimedia Commons.

## FeTAp-611-Klemmen-Legende (aus Schaltplan)

**Externe Klemmen:**
- `(La)` = a-Ader Amtsleitung
- `(Lb)` = b-Ader Amtsleitung
- `(W)` = Zusatzwecker
- `(G)` = Gebührenzähler

**Bauteile / interne Lötpunkte:**
- `F` = Fernhörer (Hörkapsel im Handapparat)
- `M` = Mikrofon (Sprechkapsel im Handapparat)
- `GU` = Gabelumschalter
- `nsi` / `nsa` = Nummernschalter-Kontakte (Impuls / Anlass)
- `W` = Wecker (Original-Klingel)
- `Ü` = Übertrager (Hagenuk-Trafo, grün)
- `GGs` = Gleichrichter Gehörschutz

**Beschriftungen auf der Anschaltleiste** (was wir auf Fotos sehen):
- `AS` = Anschaltleiste (wo das Amtsleitungskabel reingeht)
- `HS` = Handapparat-Schnur (4 Adern vom Hörer)
- `Z1/Z2` = Zusatzwecker-Anschluss
- `ET` = Erdtaste
- `R1/R2` = die Widerstände (Dirk lässt sie drin)
- `C1/C2` = Kondensatoren (C1 lässt Dirk drin)

## Was wir tatsächlich brauchen

Da wir **nur Audio + GPIO** verkabeln und das Amt komplett umgehen, brauchen
wir die `La/Lb/G`-Klemmen NICHT. Wir greifen nur ab:

1. **`F`-Kontakte** → Lautsprecher-Signal → 3,5-mm-Klinkenstecker (Headphone)
2. **`M`-Kontakte** → Mikro-Signal → 3,5-mm-Klinkenstecker (Mic-In)
3. **`GU`-Kontakte** → GPIO 5 + GND
4. **`nsi`-Kontakte** → GPIO 26 + GND
5. **`W`-Kontakte** → H-Brücke OUT1 (Original-Wecker direkt mit 30 V ansteuern)

Die genaue Zuordnung der Lötösen zu den Klemmen pro Hardware-Exemplar am
besten mit Durchgangsprüfer vor dem Löten verifizieren.

## Pinbelegung (BCM)

| Komponente              | Pi-Pin (BCM) | Hinweis                                     |
|-------------------------|--------------|---------------------------------------------|
| Gabelschalter           | GPIO 5 + GND | Pull-Up, HIGH = abgehoben                   |
| Wählscheibe (Puls)      | GPIO 26 + GND| FALLING-Edge zählt Impulse                  |
| H-Brücke IN1            | GPIO 17      | Klingel-Steuerung                           |
| H-Brücke IN2            | GPIO 27      |                                             |
| H-Brücke ENA            | GPIO 22      | PWM 1 kHz                                   |

**Test-Trick (kein Telefon nötig):** Pin 29 (= BCM GPIO 5) und Pin 25 (= GND)
mit Kabel brücken simuliert abgehobenen Hörer.

## Hörer (Mikro + Lautsprecher)

- Originale Steckverbindung **intakt lassen**.
- Auf der **Unterseite** der Telefonplatine 4 Kabel anlöten:
  - **weiß-braun** = Sprechmuschel (Mikrofon)
  - **grün-gelb** = Hörmuschel (Lautsprecher)
- Andere Enden: je ein **3,5-mm-Klinkenstecker** → USB-Soundkarte (Mic-In / Headphone-Out).

## Gabelschalter (GU)

- 2 Kabel (im Make-Artikel blau) **von der Platine** abgreifen.
- → GPIO 5 (Pin 29) und GND (Pin 30, direkt daneben).
- **Wichtig:** Die richtigen Pads liegen **nicht** ganz am Platinenrand!
  Mehrere Pads dort sind über interne Leiterbahnen miteinander verbunden
  und sehen den eigentlichen Schaltkontakt nie. Die korrekten GU-Pads
  liegen weiter mittig — dort wo der mechanische Federkontakt des
  Gabelumschalters direkt drückt. Vor dem endgültigen Löten mit
  Durchgangsprüfer oder `scripts/test_gpio.py` verifizieren:
  Hörer drücken vs. loslassen muss `is_active` umschalten.
- **Prellen:** Der Schalter prellt heftig (5–10 Events pro Sekunde nach
  jedem mechanischen Umschalten). `GpioHookSwitch` setzt deshalb
  `bounce_time=0.2` — sonst startet die Call-State-Machine endlos neue
  Anrufe.
- **Invertierte Logik:** Bei diesem FeTAp 611-Exemplar gilt
  `Kontakt geschlossen = abgehoben` und `Kontakt offen = aufgelegt`
  (mit Pull-Up zu `is_active=True` bzw. `False`).

## Wählscheibe (NSI)

- Aus der Wählscheibe kommen 4 Kabel über Stecker auf die Platine.
- Nur 2 davon zum Pi führen:
  - **gelb** → GND
  - **grün** → GPIO 26

## Klingel — Original-Wecker

- H-Brücke **L298N** schaltet die Klingelspule.
- **Step-up-Wandler** 5 V → ~30 V speist die H-Brücke am **+12V**-Eingang.
- Klingel an **OUT1** der H-Brücke.
- Pi-Steuerung:
  - GPIO 17 → IN1
  - GPIO 27 → IN2
  - GPIO 22 → ENA

**WICHTIG:** Den Jumper auf der H-Brücke **entfernen**, sonst landen die 30 V
nicht an OUT1. Wenn der Jumper raus ist, braucht der Logikbaustein der
H-Brücke **zusätzlich 5 V** am +5V-Eingang (vom Pi).

PWM 1 kHz, bipolare ~10 Hz Wechselspannung (siehe `bell.py` der Referenz)
für authentischen "Drrr-Drrr"-Klang.

## Platinen-Stripping

- Die meisten Bauteile auf der Telefonplatine entfernen.
- Übrig lassen: **2 Widerstände + 1 Kondensator** (für Hörer-Signalpfad).
- **Gabel** mit Dremel etwas einkürzen, damit der Pi ins Gehäuse passt.

## USB-Soundkarte konfigurieren

**Wichtig:** Die Karte kann in Hardware nur 48 kHz, der Code öffnet aber
24 kHz (natives OpenAI-Realtime-Format). Der ALSA-`default` muss deshalb ein
`plug`-Gerät sein, das resampelt — sonst `paInvalidSampleRate` beim
Stream-Öffnen. Auf frisch geflashtem OS ist kein solcher Default vorhanden,
also explizit anlegen:

```
sudo nano /etc/asound.conf      # oder ~/.asoundrc (nur für den User)
```

```
pcm.!default {
    type plug
    slave.pcm "hw:N,0"
}
ctl.!default {
    type hw
    card N
}
```

`N` = Kartennummer aus `aplay -l` / `python scripts/list_audio.py`
(bzw. `hw:N,0`). `plug` rechnet 24 kHz → 48 kHz für Wiedergabe **und**
Aufnahme um. Danach muss `list_audio.py` zusätzlich ein `default`-Gerät
zeigen. Der Code wählt kein Gerät explizit (`audio.py` öffnet ohne
`device=`) — er nimmt dieses `default`, es gibt keine `AUDIO_*`-Env-Var.

### alsamixer-Einstellungen

`alsamixer` öffnen, F6 für Karten-Auswahl → USB-Karte:

- **Speaker** auf 100 (= 0 dB)
- **Mic-Capture** auf ~68
- **AGC aus** (automatic gain control verfälscht Echtzeit-Audio)

### Kohlemikrofon (FeTAp 611 original)

Funktioniert **direkt** mit der UGREEN USB-Soundkarte — kein Elektret-Ersatz
nötig, der Mic-Eingang liefert genug Bias-Spannung.

### „Kalte Lötstelle"-Symptom

Wenn ein Audio-Pfad still ist obwohl die Pipeline läuft, USB-Karte korrekt
ist und alsamixer hochgeregelt ist: **vermutlich kalte Lötstelle am
Klinkenstecker**. Mit Standard-Kopfhörer am Speaker-Out vergleichen — wenn
der laut spielt, der Hörer aber nichts ausgibt, Klinkenstecker neu verlöten.

## Pi 5 / CM5 — Unterschiede zur Referenz

- Make benutzt Pi 4. Wir haben Pi 5 → mehr Reserve, kein Problem.
- **RPi.GPIO ist tot auf Pi 5** (neuer RP1-Chip). Wir nutzen `gpiozero` mit
  `lgpio`-Backend (Bookworm-Default).
- Onboard-Audio fällt beim Pi 5 weg → USB-Soundkarte ist sowieso Pflicht.
- **Aktuelle Zielhardware: Pi 5 Model B.** Ein Umzug auf ein Compute
  Module 5 (Waveshare CM5-NANO-B) wurde im Juli 2026 erprobt, das CM5 war
  jedoch defekt und ging zurück. Die CM5-Notizen in `pi-inbetriebnahme.md`
  §10 (rpiboot-Flashen, Header-Orientierung, `hw:2,0`-asound.conf) sind
  **historisch** — die dortige Kartennummer gilt nicht für den Pi 5,
  hier ist die USB-Karte `hw:0,0`.
