# Stromversorgung – telefon-ai

> **Status 2026-07-29: Problem gelöst, ohne zusätzliche Hardware.**
> Die ursprüngliche Annahme dieses Dokuments — die Klingelschaltung ziehe zu
> viel Strom und bringe den Pi 5 zum Absturz — ist **durch Messung widerlegt**.
> Die Abstürze hatten eine andere Ursache. Es muss **nichts gekauft werden**.
> Details unten unter „Messergebnis" und „Tatsächliche Ursache".

## Aufbau (funktionierend)

Wie in der Referenz (Make 2/26, Dirk Wahl):

```
Pi-5-Netzteil (offiziell, 5V/5A)
        │
        ├──► Raspberry Pi 5
        │
        └──► GPIO-Pin 2/4 (5V) ──┬──► MT3608 Step-up ──► ~27 V ──► L298N +12V
                                 │
                                 └──► L298N +5V (Logik)
```

Gemeinsame Masse durchgehend: Pi GND = MT3608 VIN− = MT3608 VOUT− = L298N GND.

Steuerung über GPIO 17 → IN1, GPIO 27 → IN2, GPIO 22 → ENA.
Beide Jumper am L298N entfernt (ENA-Jumper, damit GPIO 22 den Enable steuert;
5V-EN-Jumper, damit der Onboard-Regler bei 27 V am +12V-Eingang nicht arbeitet —
deshalb muss die Logik separat mit 5 V gespeist werden).

## Anforderungen (tatsächlich gemessen)

| Komponente | Spannung | Strom |
|---|---|---|
| Raspberry Pi 5 | 5V | bis 5A (Peak) |
| L298N Logik (+5V) | 5V | ~40 mA |
| MT3608 + Klingelspule | 5V (Eingang) | **~130 mA** |

Die FeTAp-611-Weckerspule hat **1654 Ω** (gemessen). Bei 27 V fließen daraus
**~16 mA**, also ~0,43 W. Auf der 5-V-Seite sind das inklusive
Wandlerverlusten rund 130 mA. Das ist für ein 5-A-Netzteil vernachlässigbar.

Wichtig: Es handelt sich **nicht um einen Motor**, sondern um eine
hochohmige, polarisierte Weckerspule. Die ursprünglich hier vermutete
„hohe Anlaufstromspitze" existiert nicht.

## Messergebnis (test_bell_probe.py, 2026-07-29)

`scripts/test_bell_probe.py` protokolliert die 5-V-Schiene des Pi über
`vcgencmd pmic_read_adc EXT5V_V` und schreibt mit `fsync`, überlebt also
auch einen harten Reboot.

```
5V-Schiene (Leerlauf):                    5,119 V
Stufe 1 — 100-ms-Puls bei  50 % Duty:     5,123 V
Stufe 2 — 2 s Dauerlast bei 50 % Duty:    5,124 / 5,112 / 5,119 / 5,111 V
Stufe 3 — 2 s Dauerlast bei 100 % Duty:   5,140 / 5,071 / 4,967 / 5,138 V
Test komplett durchgelaufen — kein Brownout.
```

Die Schiene bleibt selbst im Worst Case (100 % Duty, härter als echtes
Klingeln) stabil. Tiefster Einzelwert 4,97 V — normales Messrauschen, weit
über der Brownout-Schwelle. **Es gibt kein Versorgungsproblem.**

## Tatsächliche Ursache der Abstürze

**Lose Lötstellen am MT3608.** Das Board hat nur Lötpads, keine Klemmen. Die
Drähte waren zunächst nur eingehängt. Jede Berührung am Aufbau — Poti drehen,
Test starten — konnte den GND- oder 5-V-Kontakt kurz unterbrechen bzw.
funken, was den Pi sofort abschaltete. Beide beobachteten Abstürze passierten
genau währenddessen.

Nach dem Festlöten der vier Drähte (VIN+, VIN−, VOUT+, VOUT−): **kein
einziger Absturz mehr**, auch nicht bei 100 % Duty.

**Lehre:** Ein Pi 5, der komplett ausgeht statt zu rebooten, bei
`vcgencmd get_throttled` = `0x0` — das ist ein Kontaktproblem, kein
Versorgungsproblem. Erst die Mechanik prüfen, bevor Hardware gekauft wird.

## Zweiter Fehler: die Klingel blieb stumm

Unabhängig von den Abstürzen schlug der Wecker nicht an, obwohl 26 V an den
Spulenklemmen anlagen. Ursache: **PWM-Tastgrad**.

Dirks `bell.py` fährt 50 % Duty bei 30 V. Unser MT3608 schafft am Anschlag
nur **27 V** (Bauteilgrenze, ca. 28 V laut Datenblatt). Bei 50 % Duty bleiben
davon effektiv ~13 V, also nur ~8 mA — zu wenig, um den Anker zu bewegen.

Behoben in `scripts/test_bell.py`: **100 % Duty und 25 Hz** statt 10 Hz.
25 Hz ist die originale Rufwechselspannung der Post, auf die das Wecker-Werk
mechanisch abgestimmt ist; im A/B-Vergleich (`scripts/test_bell_sweep.py`,
10/20/25 Hz) klang sie deutlich am besten.

---

## Verworfene Lösungen

### ZY12PDN PD-Trigger + Buck Converter (~31–36 €)

War die ursprünglich hier empfohlene Lösung, begründet mit einem
Stromversorgungsproblem, das **nicht existiert** (siehe Messergebnis).

Sie hätte die Abstürze auch nicht behoben, denn die Ursache lag in der
Verdrahtung. Hinzu kam ein Denkfehler im Konzept: Pi und L298N sollten von
**derselben** Buck-Ausgangsklemme gespeist werden — damit hängen sie wieder an
einem gemeinsamen 5-V-Knoten, genau wie an den GPIO-Pins. Das ist keine
Entkopplung, nur eine Verlagerung. („Galvanisch getrennt", wie es hier stand,
wäre es ohnehin nicht gewesen.)

**Als Komfort-Lösung fürs fertige Gehäuse** bleibt die Idee reizvoll: ein
einziges USB-C-Kabel ins Gehäuse statt zwei Netzteile. Falls sie je gebaut
wird, zwei Punkte beachten:

- **Pi 5 drosselt seine USB-Ports auf 600 mA**, wenn er kein PD-fähiges
  Netzteil erkennt. Ein Buck Converter verhandelt kein PD. Die USB-Soundkarte
  hängt daran → `usb_max_current_enable=1` in `/boot/firmware/config.txt`.
- **„5V/5A" ist bei LM2596/MP2307 optimistisch** (real ~2 bzw. ~3 A). Für
  Pi 5 + Soundkarte + L298N wäre ein echter 5-A-Wandler mit Kühlkörper nötig.

### Zwei separate Netzteile

Elektrisch die sauberste Entkopplung, aber nach obiger Messung schlicht
unnötig. Zwei Kabel und zwei Gehäusedurchführungen ohne Gegenwert.
