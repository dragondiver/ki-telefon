"""Klingel-Test mit Brownout-Forensik.

Wie test_bell_static.py, aber:
- loggt die 5V-Schiene (PMIC EXT5V_V) vor/während/nach dem Bestromen,
- schreibt jede Zeile sofort mit fsync nach scripts/bell_probe.log —
  überlebt also einen harten Reboot,
- macht erst einen kurzen 100-ms-Puls, dann erst die vollen 2 s.

Auswertung nach einem Crash: einfach `cat scripts/bell_probe.log`.
Die letzte Zeile zeigt, wie weit das Skript kam; die Spannungswerte
zeigen, ob die 5V-Schiene eingebrochen ist (Brownout < ~4,8 V).

    python3 scripts/test_bell_probe.py
"""

import os
import subprocess
import time
from pathlib import Path

from gpiozero import OutputDevice, PWMOutputDevice

LOG = Path(__file__).parent / "bell_probe.log"
_log_fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND)


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    os.write(_log_fd, line.encode())
    os.fsync(_log_fd)  # sofort auf die SD-Karte, überlebt Reboot
    print(line, end="")


def rail_voltage() -> str:
    try:
        out = subprocess.run(
            ["vcgencmd", "pmic_read_adc", "EXT5V_V"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        return out.split("=")[-1] if "=" in out else out
    except Exception as e:  # Messung darf den Test nie abbrechen
        return f"?({e})"


log("=== Neuer Testlauf ===")
log(f"5V-Schiene (Leerlauf): {rail_voltage()}")

IN1 = OutputDevice(17)
IN2 = OutputDevice(27)
ENA = PWMOutputDevice(22, frequency=1000)
log("GPIO initialisiert (IN1=17, IN2=27, ENA=22)")

# Stufe 1: kurzer Puls mit 50 % Duty — wie die Referenz-bell.py,
# wenig Energie, zeigt einen Einbruch trotzdem.
log("Stufe 1: 100-ms-Puls bei 50% Duty (Referenz-Niveau) ...")
IN1.on()
IN2.off()
ENA.value = 0.5
time.sleep(0.05)
log(f"  5V-Schiene unter Last: {rail_voltage()}")
time.sleep(0.05)
ENA.value = 0
log(f"Stufe 1 überlebt. 5V-Schiene danach: {rail_voltage()}")

time.sleep(2)

# Stufe 2: 2 s bei 50 % Duty — mittlere Leistung wie echtes Klingeln.
log("Stufe 2: 2 s Dauer-Bestromung bei 50% Duty ...")
ENA.value = 0.5
for i in range(4):
    time.sleep(0.5)
    log(f"  t={0.5 * (i + 1):.1f}s  5V-Schiene: {rail_voltage()}")
ENA.value = 0
log(f"Stufe 2 überlebt. 5V-Schiene danach: {rail_voltage()}")

time.sleep(2)

# Stufe 3: 2 s bei 100 % Duty — Worst Case, härter als echtes Klingeln.
# Stirbt der Pi erst hier, ist die Versorgung grenzwertig, aber der
# Normalbetrieb (50 %) evtl. schon machbar.
log("Stufe 3: 2 s Dauer-Bestromung bei 100% Duty (Worst Case) ...")
ENA.value = 1.0
for i in range(4):
    time.sleep(0.5)
    log(f"  t={0.5 * (i + 1):.1f}s  5V-Schiene: {rail_voltage()}")
ENA.value = 0
IN1.off()
IN2.off()
log(f"Stufe 3 überlebt. 5V-Schiene danach: {rail_voltage()}")
log("Test komplett durchgelaufen — kein Brownout.")
