#!/usr/bin/env python3
"""Erzeugt die gesprochenen Stoerungsansagen einmalig per OpenAI-TTS.

Einmal-Job, kein Laufzeitcode: die Ergebnisse liegen als PCM16 im Repo
(``src/kitelefon/ansagen/*.pcm``) und werden von ``kitelefon.ansagen``
geladen. Neu laufen lassen muss man das Skript nur, wenn sich ein Wortlaut
in ``ansagen.TEXTE`` aendert oder die Stimme gewechselt werden soll.

    python scripts/ansagen_erzeugen.py            # alle, nur fehlende
    python scripts/ansagen_erzeugen.py --alle     # alle, auch vorhandene
    python scripts/ansagen_erzeugen.py mikro_tot  # nur diese

Stimme ist ``alloy`` — die Rolle "Kuenstliche Intelligenz" aus ``roles.py``.
Bewusst nicht Camilles Stimme: eine deutsche Stoerungsansage aus dem Mund
der franzoesischen Lehrerin waere ein Bruch, und die KI-Rolle tritt ohnehin
offen als Maschine auf.

Nutzt ``httpx`` statt des openai-SDK, weil das Projekt das SDK nicht als
Abhaengigkeit fuehrt und ein Einmal-Skript kein Grund ist, damit anzufangen.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kitelefon.ansagen import ANSAGEN_DIR, TEXTE, pfad  # noqa: E402

API_URL = "https://api.openai.com/v1/audio/speech"
MODELL = "gpt-4o-mini-tts"
STIMME = "alloy"

#: Klang der KI-Rolle aus ``roles.py``, in Anweisungen uebersetzt. Der
#: Sprechstil laesst sich bei diesem Modell nur so steuern — einen
#: ``speed``-Parameter nimmt es nicht entgegen.
ANWEISUNG = (
    "Sprich Hochdeutsch mit korrekten Umlauten, freundlich, klar und ohne "
    "Getue. Etwas zuegiger als normales Sprechtempo. "
    "Keine uebertriebene Dienstbeflissenheit, "
    "keine Entschuldigungsformeln, kein bedauernder Tonfall — es ist eine "
    "sachliche Information, keine Katastrophe. Ruhig und deutlich genug, "
    "dass eine aeltere Zuhoererin am Telefon jedes Wort versteht."
)

# Der TTS-Endpunkt liefert bei response_format=pcm genau das, was die
# Ausgabe-Pipeline erwartet: 24 kHz, 16 bit, mono, little-endian, ohne
# Header. Passt zu audio.RATE — deshalb keine Konvertierung noetig.
FORMAT = "pcm"


def erzeuge(text: str, key: str, timeout: float = 60.0) -> bytes:
    antwort = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": MODELL,
            "voice": STIMME,
            "input": text,
            "instructions": ANWEISUNG,
            "response_format": FORMAT,
        },
        timeout=timeout,
    )
    antwort.raise_for_status()
    return antwort.content


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("namen", nargs="*", help="nur diese Ansagen (Standard: alle)")
    p.add_argument("--alle", action="store_true", help="auch vorhandene Assets neu erzeugen")
    args = p.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        # .env des Projekts, falls das Skript ausserhalb der App laeuft.
        env = Path(__file__).resolve().parent.parent / ".env"
        if env.exists():
            for zeile in env.read_text().splitlines():
                if zeile.startswith("OPENAI_API_KEY="):
                    key = zeile.split("=", 1)[1].strip().strip("'\"")
                    break
    if not key:
        print("OPENAI_API_KEY fehlt (Umgebung oder .env)", file=sys.stderr)
        return 1

    namen = args.namen or list(TEXTE)
    unbekannt = [n for n in namen if n not in TEXTE]
    if unbekannt:
        print(f"unbekannte Ansage(n): {', '.join(unbekannt)}", file=sys.stderr)
        print(f"bekannt sind: {', '.join(TEXTE)}", file=sys.stderr)
        return 1

    ANSAGEN_DIR.mkdir(parents=True, exist_ok=True)
    for name in namen:
        ziel = pfad(name)
        if ziel.exists() and not args.alle:
            print(f"  uebersprungen (existiert): {ziel.name} — --alle erzwingt neu")
            continue
        text = TEXTE[name]
        print(f"  erzeuge {ziel.name}: {text!r}")
        daten = erzeuge(text, key)
        ziel.write_bytes(daten)
        sekunden = len(daten) / 2 / 24000  # PCM16 mono @ 24 kHz
        print(f"    {len(daten)} Bytes, {sekunden:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
