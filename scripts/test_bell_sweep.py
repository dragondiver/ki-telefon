"""Klingel-Sweep: volle Leistung, mehrere Umpol-Frequenzen.

Hintergrund: `test_bell.py` fährt mit 50 % PWM-Duty (wie Dirks Referenz).
Bei 26 V an der Spule liegen damit effektiv nur ~13 V an, also ~8 mA durch
eine 1654-Ohm-Wecker-Spule. Das ist schwächer als der statische Halte-Test,
bei dem sich schon nichts bewegt hat.

Dieses Skript fährt deshalb **100 % Duty** und probiert mehrere Frequenzen
durch. 25 Hz ist die originale Rufwechselspannung der Post — darauf ist das
Wecker-Werk mechanisch abgestimmt, es kann dort deutlich williger ansprechen
als bei Dirks 10 Hz.

Elektrisch unkritisch: `test_bell_probe.py` hat nachgewiesen, dass 100 % Duty
die 5-V-Schiene des Pi nicht einbrechen lässt (Stufe 3, 5,13 V stabil).

Beim Laufen einen Finger auf das Wecker-Werk legen — ein minimales Zucken
spürt man, auch wenn man es nicht hört.

    python3 scripts/test_bell_sweep.py
    python3 scripts/test_bell_sweep.py --freqs 25 --duty 1.0 --dauer 5

Pinbelegung (BCM):
    GPIO 17 -> IN1
    GPIO 27 -> IN2
    GPIO 22 -> ENA  (PWM)
"""

import argparse
import time

from gpiozero import OutputDevice, PWMOutputDevice

IN1_PIN = 17
IN2_PIN = 27
ENA_PIN = 22

PWM_FREQUENCY = 1000
DEAD_TIME_S = 0.003  # Schutz-Lücke beim Umpolen
DEFAULT_FREQS = (10, 20, 25)
DEFAULT_DURATION_S = 2.0
DEFAULT_DUTY = 1.0
PAUSE_S = 1.5


def bipolar_wave(in1, in2, ena, freq_hz: float, duration_s: float, duty: float) -> None:
    """Polt die Spule mit `freq_hz` um, bei `duty` PWM-Tastgrad."""
    half = 0.5 / freq_hz
    end = time.time() + duration_s
    while time.time() < end:
        ena.value = 0
        time.sleep(DEAD_TIME_S)
        in1.on()
        in2.off()
        ena.value = duty
        time.sleep(half)

        ena.value = 0
        time.sleep(DEAD_TIME_S)
        in1.off()
        in2.on()
        ena.value = duty
        time.sleep(half)

    ena.value = 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Klingel-Sweep über mehrere Umpol-Frequenzen")
    parser.add_argument(
        "--freqs",
        type=float,
        nargs="+",
        default=list(DEFAULT_FREQS),
        metavar="HZ",
        help="Umpol-Frequenzen in Hz (Default: 10 20 25)",
    )
    parser.add_argument(
        "--duty",
        type=float,
        default=DEFAULT_DUTY,
        help="PWM-Tastgrad 0.0-1.0 (Default: 1.0 = volle Leistung)",
    )
    parser.add_argument(
        "--dauer",
        type=float,
        default=DEFAULT_DURATION_S,
        help="Dauer je Frequenz in Sekunden (Default: 2.0)",
    )
    args = parser.parse_args()

    if not 0.0 < args.duty <= 1.0:
        parser.error("--duty muss zwischen 0.0 und 1.0 liegen")

    in1 = OutputDevice(IN1_PIN)
    in2 = OutputDevice(IN2_PIN)
    ena = PWMOutputDevice(ENA_PIN, frequency=PWM_FREQUENCY)

    print(f"Sweep bei {args.duty * 100:.0f} % Duty, {args.dauer:.1f}s je Frequenz.")
    print("Finger auf das Wecker-Werk legen — auch ein Zucken zaehlt.\n")
    try:
        for i, hz in enumerate(args.freqs):
            print(f"  {hz:g} Hz ...")
            bipolar_wave(in1, in2, ena, hz, args.dauer, args.duty)
            if i < len(args.freqs) - 1:
                time.sleep(PAUSE_S)
        print("\nFertig.")
    finally:
        ena.value = 0
        in1.off()
        in2.off()


if __name__ == "__main__":
    main()
