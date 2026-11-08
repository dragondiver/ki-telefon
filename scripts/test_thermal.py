"""Temperatur- und Drossel-Protokoll — reicht die passive Kühlung im Gehäuse?

Auf dem Pi laufen lassen, während das Telefon benutzt wird. Schreibt jede
Messung mit Zeitstempel nach `thermal.log` (mit fsync, überlebt also auch
einen harten Absturz) und zeigt sie gleichzeitig im Terminal.

    python scripts/test_thermal.py                  # 30 min, alle 5 s
    python scripts/test_thermal.py --dauer 60       # 60 Minuten
    python scripts/test_thermal.py --last            # zusätzlich CPU-Last erzeugen

Mit Strg+C jederzeit beenden — die Zusammenfassung kommt trotzdem.

Einordnung der Werte (Pi 5):
  bis ~60 °C   entspannt
  60–70 °C     unauffällig, viel Luft nach oben
  70–80 °C     wird warm, aber noch keine Drosselung
  ab ~80 °C    der Pi drosselt (`throttled` meldet es)

Wichtiger als die Spitze ist, ob sich der Wert **einpendelt**. Steigt er nach
20 Minuten immer noch, staut sich die Wärme im Gehäuse — dann fehlen
Lüftungsschlitze, nicht unbedingt ein Kühler.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from datetime import datetime

LOGFILE = "thermal.log"
THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"

# Bits aus `vcgencmd get_throttled`, die uns interessieren.
THROTTLE_BITS = {
    0: "Unterspannung JETZT",
    1: "Takt gedrosselt JETZT",
    2: "gedrosselt JETZT",
    3: "Temperaturlimit JETZT",
    16: "Unterspannung war",
    17: "Takt war gedrosselt",
    18: "war gedrosselt",
    19: "Temperaturlimit war erreicht",
}


def lies_temperatur() -> float:
    with open(THERMAL_ZONE) as f:
        return int(f.read().strip()) / 1000.0


def lies_throttled() -> tuple[int, list[str]]:
    """(Rohwert, Klartext-Meldungen). Leere Liste = alles in Ordnung."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        wert = int(out.split("=")[1], 16)
    except Exception:
        return 0, []
    return wert, [text for bit, text in THROTTLE_BITS.items() if wert & (1 << bit)]


def lies_takt_mhz() -> int:
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
            return int(f.read().strip()) // 1000
    except Exception:
        return 0


def erzeuge_last() -> list[subprocess.Popen]:
    """Alle Kerne beschäftigen — der ungünstigste Fall, nicht der normale.

    Das Telefon selbst belastet den Pi kaum; diese Option zeigt die Obergrenze.
    """
    n = os.cpu_count() or 4
    print(f"Erzeuge Last auf {n} Kernen (Worst Case, nicht der Telefonbetrieb)")
    return [
        subprocess.Popen(
            ["python3", "-c", "while True: pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(n)
    ]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dauer", type=float, default=30.0, help="Minuten (Default 30)")
    p.add_argument("--intervall", type=float, default=5.0, help="Sekunden (Default 5)")
    p.add_argument("--last", action="store_true", help="CPU-Last erzeugen (Worst Case)")
    args = p.parse_args()

    last_prozesse: list[subprocess.Popen] = erzeuge_last() if args.last else []

    ende = time.monotonic() + args.dauer * 60
    werte: list[float] = []
    warnungen: set[str] = set()

    print(f"Protokolliere {args.dauer:.0f} min nach {LOGFILE} — Strg+C beendet früher.\n")
    print(f"{'Uhrzeit':8}  {'Temp':>7}  {'Takt':>8}  Status")
    print("-" * 52)

    with open(LOGFILE, "w") as log:
        log.write(f"# gestartet {datetime.now().isoformat()}, last={args.last}\n")
        try:
            while time.monotonic() < ende:
                temp = lies_temperatur()
                mhz = lies_takt_mhz()
                roh, meldungen = lies_throttled()
                warnungen.update(meldungen)
                werte.append(temp)

                status = ", ".join(meldungen) if meldungen else "ok"
                zeile = f"{datetime.now():%H:%M:%S}  {temp:5.1f}°C  {mhz:5d}MHz  {status}"
                print(zeile)
                log.write(f"{datetime.now().isoformat()}\t{temp:.1f}\t{mhz}\t{roh:#x}\n")
                log.flush()
                os.fsync(log.fileno())  # überlebt einen harten Absturz

                time.sleep(args.intervall)
        except KeyboardInterrupt:
            print("\nAbgebrochen.")
        finally:
            for proc in last_prozesse:
                proc.terminate()

    if not werte:
        return

    print("\n" + "=" * 52)
    print(f"Messungen:  {len(werte)}")
    print(f"Minimum:    {min(werte):.1f} °C")
    print(f"Maximum:    {max(werte):.1f} °C")
    print(f"Mittel:     {sum(werte) / len(werte):.1f} °C")

    # Pendelt es sich ein? Letztes Fünftel gegen das davor vergleichen.
    if len(werte) >= 10:
        fuenftel = max(2, len(werte) // 5)
        trend = sum(werte[-fuenftel:]) / fuenftel - sum(werte[-2 * fuenftel : -fuenftel]) / fuenftel
        if trend > 1.0:
            print(f"\nTrend:      steigt noch um {trend:+.1f} °C — länger messen!")
            print("            Wärme staut sich; Lüftungsschlitze prüfen.")
        else:
            print(f"\nTrend:      eingependelt ({trend:+.1f} °C)")

    if warnungen:
        print("\n⚠ Aufgetreten: " + ", ".join(sorted(warnungen)))
    else:
        print("\nKeine Drosselung, keine Unterspannung.")

    spitze = max(werte)
    if spitze < 70:
        print(f"\nFazit: {spitze:.0f} °C Spitze — passive Kühlung reicht hier klar.")
    elif spitze < 80:
        print(f"\nFazit: {spitze:.0f} °C Spitze — geht noch, aber wenig Reserve.")
        print("       Lüftungsschlitze oder flacher Kühlkörper empfehlenswert.")
    else:
        print(f"\nFazit: {spitze:.0f} °C Spitze — zu heiß, der Pi drosselt.")
        print("       Kühlkörper und Lüftung nötig.")


if __name__ == "__main__":
    main()
