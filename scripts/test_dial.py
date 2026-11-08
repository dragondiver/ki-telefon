"""Wählscheiben-Test (GPIO 26). Impulse zählen, Ziffer ausgeben.

Auf dem Pi ausführen, dann an der Wählscheibe drehen. 10 Impulse = "0".
Mit Ctrl+C beenden.

    python3 scripts/test_dial.py
"""

import time

from gpiozero import Button

MIN_PULSE_SEPARATION = 0.08  # 80 ms, gegen Prellen
INTER_DIGIT_TIMEOUT = 0.6  # nach 600 ms Stille = Ziffer fertig

dial = Button(26, pull_up=True)
pulses = 0
last_pulse = 0.0


def on_press():
    global pulses, last_pulse
    now = time.monotonic()
    if now - last_pulse > MIN_PULSE_SEPARATION:
        pulses += 1
        last_pulse = now
        print(f"  [RAW] Impuls #{pulses}")


dial.when_pressed = on_press

print("Wählscheiben-Test (GPIO 26)")
print("Drehe die Wählscheibe. Strg+C zum Beenden.\n")

while True:
    time.sleep(0.05)
    if pulses > 0 and time.monotonic() - last_pulse > INTER_DIGIT_TIMEOUT:
        digit = pulses if pulses < 10 else 0
        print(f"==> Ziffer: {digit}\n")
        pulses = 0
