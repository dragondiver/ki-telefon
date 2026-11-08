"""Statischer Klingel-Diagnose-Test.

Schaltet die Spule 2 Sekunden lang fest an (eine Richtung), dann aus.
Erwartung: Hörbares "Klick" beim Einschalten, Klöppel zieht zur Seite
und bleibt dort, bis es ausgeschaltet wird (dann "Klack" zurück).

Wenn nichts zu hören ist, sitzt das Problem nicht in der PWM-Logik,
sondern in: Verkabelung Klingel-Spule, Step-up-Ausgang, oder
+12V-Versorgung der H-Brücke.

    python3 scripts/test_bell_static.py
"""

import time

from gpiozero import OutputDevice, PWMOutputDevice

IN1 = OutputDevice(17)
IN2 = OutputDevice(27)
ENA = PWMOutputDevice(22, frequency=1000)

print("Spule wird in 2s eingeschaltet...")
time.sleep(2)
print("EIN (2 Sekunden)")
IN1.on()
IN2.off()
ENA.value = 1.0  # voll an
time.sleep(2.0)
print("AUS")
ENA.value = 0
IN1.off()
IN2.off()
