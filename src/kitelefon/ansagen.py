"""Vorgefertigte gesprochene Ansagen fuer Stoerungsfaelle.

Freizeichen, Rufton und Abschaltton entstehen in ``audio.py`` zur Laufzeit
als Sinus — dafuer braucht es keine Datei. Gesprochene Saetze lassen sich so
nicht erzeugen: sie werden einmalig per TTS produziert und als rohe
PCM16-Frames neben diesem Modul abgelegt (``scripts/ansagen_erzeugen.py``).

Format ist bewusst das der Ausgabe-Pipeline — PCM16 mono mit ``audio.RATE``,
little-endian, ohne Header. Damit gehen die Bytes ohne Konvertierung in
``AudioIO.play()``, genau wie die synthetisierten Toene.

Fehlt ein Asset (frischer Checkout ohne die Binaerdateien, kaputtes Paket),
liefert :func:`lade` leere Bytes statt zu werfen. Eine Stoerungsansage, die
ihrerseits den Anruf abbricht, waere schlimmer als die Stille, die sie
ersetzen soll — der Aufrufer spielt dann eben nichts ab.
"""

import logging
from functools import cache
from pathlib import Path

log = logging.getLogger("kitelefon.ansagen")

ANSAGEN_DIR = Path(__file__).parent / "ansagen"

#: Kurs nicht startbar — Spec sec. 7.6 (sprachkurs-Design vom 14.08.2026).
KURS_NICHT_ERREICHBAR = "kurs_nicht_erreichbar"

#: Mikrofon bleibt nach allen Neustartversuchen tot (siehe #20).
MIKRO_TOT = "mikro_tot"

#: Wortlaute — zugleich die Quelle fuer ``scripts/ansagen_erzeugen.py``.
#: Wer hier etwas aendert, muss das Asset neu erzeugen; der Text allein
#: aendert nichts an dem, was Paula hoert.
#:
#: Anders als Kommentare und Bezeichner im uebrigen Code stehen hier
#: **echte Umlaute**: das ist gesprochener Text, und "spaeter" liest ein
#: TTS-Modell womoeglich buchstabengetreu statt als "später".
TEXTE: dict[str, str] = {
    KURS_NICHT_ERREICHBAR: (
        "Der Kurs ist gerade nicht erreichbar. Bitte versuch es später noch einmal."
    ),
    MIKRO_TOT: ("Ich höre dich gerade nicht mehr. Bitte leg auf und ruf noch einmal an."),
}


def pfad(name: str) -> Path:
    """Ablageort des Assets zu ``name`` — auch wenn es (noch) nicht existiert."""
    return ANSAGEN_DIR / f"{name}.pcm"


@cache
def lade(name: str) -> bytes:
    """PCM16-Bytes der Ansage ``name``; leere Bytes, wenn sie fehlt.

    Das Ergebnis wird gecacht: die Ansagen sind klein und aendern sich zur
    Laufzeit nicht, und im Stoerungsfall soll kein Dateizugriff zwischen
    Erkennung und Ausgabe liegen.
    """
    p = pfad(name)
    try:
        daten = p.read_bytes()
    except OSError:
        log.error(
            "Ansage '%s' fehlt oder ist unlesbar (%s) — es bleibt still. "
            "Neu erzeugen mit scripts/ansagen_erzeugen.py",
            name,
            p,
        )
        return b""
    if not daten:
        log.error("Ansage '%s' ist leer (%s)", name, p)
    return daten
