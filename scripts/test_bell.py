"""Klingel-Test mit L298N-H-Brücke.

Auf dem Pi ausführen — schlägt die Klingel zwei Mal an. Bipolare
Wechselspannung ~10 Hz für den authentischen "Drrr"-Klang.

    python3 scripts/test_bell.py

WICHTIG: Step-up-Wandler muss zuerst auf Minimum stehen (Trimm-Poti
~25 Umdrehungen gegen den Uhrzeigersinn). Beim Klingel-Versuch das Poti
langsam IM Uhrzeigersinn hochdrehen bis die Klingel kräftig schlägt.

Abweichung von der Referenz (Dirks bell.py): Wir fahren 25 Hz statt 10 Hz
und 100 % statt 50 % PWM-Duty. Grund: Unser MT3608 liefert am Anschlag nur
27 V statt der 30 V der Referenz. Bei 50 % Duty liegen davon effektiv ~13 V
an der Spule, was bei 1654 Ohm nur ~8 mA ergibt — zu wenig, der Wecker blieb
komplett stumm. Mit vollem Duty sind es ~16 mA und er schlägt sauber an.
25 Hz ist die originale Rufwechselspannung der Post, auf die das Wecker-Werk
mechanisch abgestimmt ist; es klang deutlich besser als 10 oder 20 Hz.

Elektrisch abgesichert: test_bell_probe.py hat nachgewiesen, dass 100 % Duty
die 5-V-Schiene des Pi nicht einbrechen lässt (stabil bei 5,13 V).

Pinbelegung (BCM):
    GPIO 17 -> IN1
    GPIO 27 -> IN2
    GPIO 22 -> ENA  (PWM)
"""

import time

from gpiozero import OutputDevice, PWMOutputDevice

IN1 = OutputDevice(17)
IN2 = OutputDevice(27)
ENA = PWMOutputDevice(22, frequency=1000)

RING_FREQ_HZ = 25  # Wechsel-Frequenz (Original-Rufwechselspannung)
DUTY = 1.0  # volle Leistung — 50 % reicht bei 27 V nicht
SLAG_DURATION_S = 1.0  # ein Klingel-Schlag
PAUSE_S = 1.5  # Pause zwischen Schlägen
DEAD_TIME_S = 0.003  # Schutz-Lücke beim Umpolen
NUM_RINGS = 2


def bipolar_wave(duration_s: float, freq_hz: float = RING_FREQ_HZ) -> None:
    period = 1.0 / freq_hz
    half = period / 2.0
    end = time.time() + duration_s
    while time.time() < end:
        ENA.value = 0
        time.sleep(DEAD_TIME_S)
        IN1.on()
        IN2.off()
        ENA.value = DUTY
        time.sleep(half)

        ENA.value = 0
        time.sleep(DEAD_TIME_S)
        IN1.off()
        IN2.on()
        ENA.value = DUTY
        time.sleep(half)

    ENA.value = 0
    IN1.off()
    IN2.off()


def main() -> None:
    print(f"Klingel-Test - {NUM_RINGS} Schläge à {SLAG_DURATION_S}s")
    print("Step-up jetzt langsam hochdrehen wenn die Klingel zu leise ist.")
    try:
        for i in range(NUM_RINGS):
            print(f"  Schlag {i + 1}...")
            bipolar_wave(SLAG_DURATION_S)
            if i < NUM_RINGS - 1:
                time.sleep(PAUSE_S)
        print("Fertig.")
    finally:
        ENA.value = 0
        IN1.off()
        IN2.off()


if __name__ == "__main__":
    main()
