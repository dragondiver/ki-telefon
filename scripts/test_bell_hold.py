"""Halte-Test zum Messen: bestromt die Spule 30 s in einer Richtung.

Währenddessen mit dem Multimeter (DC-Volt) messen:
  1. L298N VS-Klemme gegen GND  -> erwartet: ~12 V
  2. L298N 5V-Klemme gegen GND  -> erwartet: ~5 V (Logik-Versorgung!)
  3. OUT1 gegen OUT2            -> erwartet: ~10-11 V
  4. Direkt an den Spulen-Anschlüssen -> wie OUT1/OUT2

Ist (2) = 0 V  -> L298N-Logik unversorgt: 5V-EN-Jumper fehlt und
                  5V-Klemme unbeschaltet. Dann schaltet nichts.
Ist (3) = 0 V bei vorhandener Logik -> ENA/IN-Verkabelung oder GND
                  zwischen Pi und L298N fehlt (gemeinsame Masse!).
Ist (3) ok, aber (4) = 0 V -> Kabelbruch/Klemme zur Spule.
Ist (4) ok und trotzdem still -> Mechanik (Klöppel klemmt/Justage).

    python3 scripts/test_bell_hold.py
"""

import time

from gpiozero import OutputDevice, PWMOutputDevice

IN1 = OutputDevice(17)
IN2 = OutputDevice(27)
ENA = PWMOutputDevice(22, frequency=1000)

print("Spule wird jetzt dauerhaft bestromt (IN1=HIGH, ENA=100%).")
print("In Ruhe messen — beenden mit Strg+C.")
try:
    IN1.on()
    IN2.off()
    ENA.value = 1.0
    while True:
        time.sleep(5)
        print("  ... hält (Strg+C zum Beenden)")
except KeyboardInterrupt:
    pass
finally:
    ENA.value = 0
    IN1.off()
    IN2.off()
    print("Aus.")
