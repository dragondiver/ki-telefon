"""Listet alle Audio-Geräte mit Index auf.

Auf dem Pi ausführen, um zu prüfen, dass die USB-Soundkarte gesehen wird und
ein `default`-Gerät existiert. Der Code wählt KEIN Gerät per Env-Var — er
öffnet das PortAudio-Default (`audio.py`, ohne `device=`). Die Gerätewahl
läuft über ALSA (`/etc/asound.conf` bzw. `~/.asoundrc`), nicht über die .env.

Wichtig: Die USB-Karte kann oft nur 48 kHz, der Code öffnet 24 kHz — der
`default` muss deshalb ein `plug`-Gerät sein, das resampelt. Siehe
`docs/verkabelung.md` → „USB-Soundkarte konfigurieren".

    python scripts/list_audio.py

Vergleichswerte vom System: `arecord -l` und `aplay -l`.
"""

from __future__ import annotations

import sounddevice as sd


def main() -> None:
    devices = sd.query_devices()
    print(f"{'Idx':>3}  {'In':>2} {'Out':>3}  {'Rate':>6}  Name")
    print("-" * 60)
    for i, d in enumerate(devices):
        print(
            f"{i:>3}  "
            f"{int(d['max_input_channels']):>2} "
            f"{int(d['max_output_channels']):>3}  "
            f"{int(d['default_samplerate']):>6}  "
            f"{d['name']}"
        )
    print(
        "\nTipp: Gerät mit max_input_channels>0 = Mikrofon, max_output_channels>0 = Lautsprecher."
    )


if __name__ == "__main__":
    main()
