"""Gabelschalter-Test auf GPIO 5.

Auf dem Pi ausführen — zeigt live ob der Hörer als abgehoben/aufgelegt
erkannt wird. Mit Ctrl+C beenden.

    python3 scripts/test_gpio.py

Hinweis: Beim FeTAp 611 ist die GU-Logik invertiert gegenüber dem,
was man naiv erwartet — Kontakt geschlossen heißt "abgehoben",
Kontakt offen heißt "aufgelegt".
"""

import time

from gpiozero import Button

b = Button(5, pull_up=True)
print("Gabelschalter-Test (GPIO 5)")
print("Start - Hörer:", "abgehoben" if b.is_active else "aufgelegt")
print("Jetzt Hörer bewegen...")

prev = b.is_active
while True:
    time.sleep(0.05)
    if b.is_active != prev:
        prev = b.is_active
        print("Hörer:", "abgehoben" if b.is_active else "aufgelegt")
