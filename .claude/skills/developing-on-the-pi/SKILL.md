---
name: developing-on-the-pi
description: Use when building, testing, deploying, or debugging the telefon-ai KI-Telefon on the Raspberry Pi — SSH connection, uv on the login shell only, syncing code from the Mac, running the pytest suite on the Pi, or exercising GPIO/audio/bell hardware that physically moves parts.
---

# Developing on the Pi (telefon-ai)

Living reference for working on the KI-Telefon's target hardware (Raspberry Pi 5,
`KITELEFON_PLATFORM=pi`). Refine it whenever a new Pi lesson is learned — this file
is meant to grow.

## Connect & environment

- **SSH:** `ssh kitelefon.local` (alias in `~/.ssh/config`, user `florian`,
  key `id_telefonki`). The second alias `telefon-ai.local` currently times out —
  use `kitelefon.local`.
- **Repo path:** `~/telefon-ai` on the Pi (NOT `~/Development/...` like the Mac).
- **`uv` lives at `~/.local/bin/uv` and is ONLY on the login-shell PATH.** A plain
  `ssh host 'uv ...'` fails with `command not found`. Always wrap in a login shell:

  ```bash
  ssh kitelefon.local 'bash -lc "cd ~/telefon-ai && uv run --extra dev pytest -q"'
  ```

## Sync workflow (Mac → Pi)

Develop and commit on the Mac, push, then **`git pull` on the Pi.** Don't edit code
directly on the Pi.

**Before pulling, always `git status` on the Pi first.** A second Claude instance
runs *on* the Pi and may have uncommitted work (e.g. `docs/pi-inbetriebnahme.md`).
Never clobber it — leave uncommitted/foreign changes untouched and only pull once
the tree is clean or the other instance's work is committed.

## Testing

- Full suite on the Pi: `uv run --extra dev pytest -q` (via the login-shell wrapper).
- **On the Mac there is no `uv`** — don't go hunting for it. The checked-out venv
  works directly: `.venv/bin/python -m pytest -q`. Use this when the Pi is powered
  off; the pure-logic suite doesn't need hardware.
- Hardware-dependent tests carry the `pi` marker; the pure-logic suite runs on both
  machines and should stay green on both.

## Hardware safety — the one hard rule

**Scripts that energize hardware physically MOVE real parts.** The bell strikes
(25 Hz, full PWM duty, ~27 V at the coil), the dial pulses, GPIO toggles.

**ALWAYS warn the user and get an explicit go-ahead before running a
hardware-moving script. Never run one unannounced.** (This rule exists because a
sweep was once started without warning — don't repeat it.) For anything that just
reads state (`vcgencmd`, `git status`, pytest), no warning needed.

- GPIO callbacks: only set flags, never do blocking audio work inside them.
- Pins (BCM): Gabelschalter GPIO5 (pull-up, HIGH = abgehoben) · Wählscheibe GPIO26
  (pull-up, falling edge) · Klingel IN1=17, IN2=27, ENA=22 (both L298N jumpers off,
  logic 5 V fed separately).

## Audio gotchas (carried over from the CM5 bring-up)

- USB sound card only does 44.1/48 kHz → a resampling ALSA `plug` default is
  required, else `paInvalidSampleRate`. The code selects no device explicitly (no
  `AUDIO_*` env) and takes PortAudio's `default`.
- **Diagnose the audio stack top-down, one command:**
  `lsusb` → `aplay -l` / `arecord -l` → `ls -l /etc/asound.conf` → `list_audio.py`.
  Read the symptom before fixing:
  - `list_audio.py` prints an **empty table** / `Error querying device -1` → PortAudio
    sees zero devices. Usually the USB card is simply **not plugged in** (check first
    after any hardware rebuild), or the kernel doesn't see it.
  - Card listed as `hw:N,0` but **no `default` row** → no default PCM defined.
  - `default` exists but `paInvalidSampleRate` → the default isn't a `plug` device.
- **`defaults.pcm.card N` is NOT enough.** A stock `/etc/asound.conf` containing only
  `defaults.pcm.card 0` / `defaults.ctl.card 0` fails twice: wrong card number *and*
  no resampling. Replace it (back it up first) with a real `plug` default:

  ```
  pcm.!default { type plug
      slave.pcm "hw:N,0" }
  ctl.!default { type hw
      card N }
  ```

  `N` = card number from `list_audio.py` / `aplay -l`. Verify by re-running
  `list_audio.py` (a `default` row must appear) **and** a record→playback loopback —
  the error disappearing is not proof that audio actually flows.
- Loopback one-liner (records 3 s at 24 kHz, plays it back):
  ```bash
  uv run python -c "import sounddevice as sd; r=sd.rec(int(3*24000),samplerate=24000,channels=1,dtype='int16'); sd.wait(); sd.play(r,samplerate=24000); sd.wait()"
  ```
- `libportaudio2` must be installed via apt — it's not in the uv lock.
- **Overlay-FS trap:** with overlay-fs on, `~/.asoundrc` (and `tasks.db`, tokens,
  recordings) is discarded on reboot. Put ALSA config in `/etc/asound.conf`; better
  yet disable overlay-fs (`raspi-config` → Performance → Overlay FS → off).
- **Pin the card by name, never by number — numbers move.** Observed 2026-08-01 on
  the old Pi 5: the C-Media adapter was card **2**, and after one reboot it was card
  **0**. A hard-coded number in `asound.conf` means silence after the next boot.

  ```
  # /etc/udev/rules.d/85-kitelefon-audio.rules
  SUBSYSTEM=="sound", ACTION=="add", ATTRS{idVendor}=="0d8c", ATTRS{idProduct}=="0014", ATTR{id}="telefon"
  ```

  Then reference `hw:telefon,0` / `card telefon` in `/etc/asound.conf`. Get the
  vendor:product pair from `lsusb`. **Verify across an actual reboot** — that is the
  only proof the pin holds. AGC must be **ON** for the UGREEN + carbon-mic combo
  (`amixer -c ugreen sset 'Auto Gain Control' on`; then `alsactl store`).
- Mic dead but pipeline runs? First suspect a loose 3.5 mm jack.

## Power / crash forensics

- A Pi that goes **fully off** (not a reboot) with `vcgencmd get_throttled` = `0x0`
  is a **contact/short problem, not the PSU.** (The bell silence + crashes traced to
  loose MT3608 solder joints, not undervoltage — proven by probing the 5 V rail.)
- `vcgencmd pmic_read_adc EXT5V_V` reads the 5 V rail; `get_throttled` `0x0` = clean.
- journald is volatile across reboots → use fsync file-logging to survive a crash.
- App file log: `kitelefon.log` in the working dir (`tail -f` it).

## Run the app

```bash
ssh kitelefon.local 'bash -lc "cd ~/telefon-ai && KITELEFON_PLATFORM=pi KITELEFON_HALF_DUPLEX=false uv run kitelefon"'
```

A manual start must set `KITELEFON_PLATFORM=pi` and `KITELEFON_HALF_DUPLEX=false` itself: since
telefon-ai#120 they live in `deploy/kitelefon.service`, not in the `.env`. Without them the app
falls back to the defaults `mac` (keyboard instead of GPIO) and half-duplex.

Lift handset → dial tone → dial 1–9 for a role → talk → hang up ends the call.
Env of note: `KITELEFON_AUTOCALL_DELAY_S=30` arms the auto-call, `KITELEFON_STARTUP_RING`
defaults on, `KITELEFON_BELL_ENABLED`, `KITELEFON_RECORD_CALLS=true` for WAV capture.
