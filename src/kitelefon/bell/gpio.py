import asyncio
import logging

from gpiozero import OutputDevice, PWMOutputDevice

from kitelefon.bell import race_ring_against_answer

log = logging.getLogger(__name__)

# Werte experimentell ermittelt, siehe docs/stromversorgung.md.
# Abweichung von der Referenz (Dirks bell.py: 10 Hz, 50 % Duty): unser
# MT3608 liefert am Anschlag nur 27 V statt 30 V. Bei 50 % Duty bleiben
# davon effektiv ~13 V, also ~8 mA durch die 1654-Ohm-Weckerspule — zu
# wenig, der Anker bewegt sich nicht. Mit vollem Duty sind es ~16 mA.
# 25 Hz ist die originale Rufwechselspannung der Post, auf die das
# Wecker-Werk mechanisch abgestimmt ist; klang im A/B-Vergleich am besten.
RING_FREQ_HZ = 25.0
DUTY = 1.0
PWM_FREQUENCY = 1000
DEAD_TIME_S = 0.003  # Schutz-Lücke beim Umpolen, sonst Querstrom
SLAG_DURATION_S = 1.0  # Dauer eines Klingel-Schlags
PAUSE_S = 1.5  # Pause zwischen den Schlägen


class GpioBell:
    """Original-Wecker über L298N-H-Brücke.

    Die Spule hängt zwischen OUT1 und OUT2 (Vollbrücke). IN1/IN2 legen die
    Polarität fest, ENA schaltet per PWM die Leistung. Für den polarisierten
    FeTAp-Wecker muss die Polarität wechseln — bei Gleichstrom zieht der
    Anker nur einmal an und bleibt liegen.

    Die +12V-Klemme der Brücke kommt vom MT3608-Step-up (~27 V), die
    5V-Klemme direkt vom Pi. Beide Jumper am L298N sind entfernt.
    """

    def __init__(
        self,
        in1_pin: int = 17,
        in2_pin: int = 27,
        ena_pin: int = 22,
        freq_hz: float = RING_FREQ_HZ,
        duty: float = DUTY,
    ):
        self._in1 = OutputDevice(in1_pin)
        self._in2 = OutputDevice(in2_pin)
        self._ena = PWMOutputDevice(ena_pin, frequency=PWM_FREQUENCY)
        self._freq_hz = freq_hz
        self._duty = duty
        self._closed = False
        log.info(
            "GpioBell ready on IN1=%d IN2=%d ENA=%d (%.0f Hz, %.0f%% Duty)",
            in1_pin,
            in2_pin,
            ena_pin,
            freq_hz,
            duty * 100,
        )

    def _off(self) -> None:
        """Spule stromlos. Muss auch bei Cancellation laufen."""
        self._ena.value = 0
        self._in1.off()
        self._in2.off()

    async def _bipolar_wave(self, duration_s: float) -> None:
        """Polt die Spule mit `self._freq_hz` um."""
        half = 0.5 / self._freq_hz
        loop = asyncio.get_running_loop()
        end = loop.time() + duration_s
        try:
            while loop.time() < end:
                self._ena.value = 0
                await asyncio.sleep(DEAD_TIME_S)
                self._in1.on()
                self._in2.off()
                self._ena.value = self._duty
                await asyncio.sleep(half)

                self._ena.value = 0
                await asyncio.sleep(DEAD_TIME_S)
                self._in1.off()
                self._in2.on()
                self._ena.value = self._duty
                await asyncio.sleep(half)
        finally:
            # Auch bei Auflegen mitten im Schlag: Spule nie bestromt lassen.
            self._off()

    async def ring(self, rings: int = 1) -> None:
        for i in range(rings):
            log.debug("bell strike %d/%d", i + 1, rings)
            await self._bipolar_wave(SLAG_DURATION_S)
            if i < rings - 1:
                await asyncio.sleep(PAUSE_S)

    async def ring_until_answer(self, answered: asyncio.Event, max_rings: int = 5) -> bool:
        """Klingelt, bricht sofort ab sobald `answered` gesetzt wird."""
        try:
            return await race_ring_against_answer(lambda: self.ring(max_rings), answered)
        finally:
            self._off()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._off()
        self._ena.close()
        self._in1.close()
        self._in2.close()
        log.debug("GpioBell closed")
