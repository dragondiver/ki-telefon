"""Namen aus dem Haushalt: aus der Umgebung, nicht fest im Code (#149).

Das Repo soll öffentlich werden, die Namen der Familie gehören aber nicht
hinein. Sie stehen deshalb in der `.env` (1Password-Environment):

- `KITELEFON_PERSONEN`: Mail-Empfänger, die der Haus-Agent unter ihrem
  Kurznamen kennt, kommagetrennt. Der erste ist der Hauptnutzer; im
  Rückruf-Prompt heißt er "dich", die übrigen werden mit Namen angeboten.
- `SPOTIFY_ACCOUNT_1` / `SPOTIFY_ACCOUNT_2`: die beiden Spotify-Konten
  (bestimmen auch die Dateinamen `token_<name>.json`).

Die Textbausteine sind reine Funktionen, damit Tests sie mit festen Namen
prüfen können, unabhängig davon, was in der echten `.env` steht (#56).
Mit den Namen vom Gerät ergeben sie Wort für Wort die Texte von vorher.
"""

import os

from dotenv import load_dotenv

# Wie agents/spotify.py: die Konstanten unten entstehen beim Import, also
# muss die .env vorher im Prozess stehen.
load_dotenv()

SPOTIFY_STANDARD = ("konto1", "konto2")
_ZAHLWORT = {1: "Ein", 2: "Zwei", 3: "Drei", 4: "Vier"}


def namen_liste(wert: str) -> list[str]:
    """'Paula, otto' -> ['paula', 'otto']; Leeres fällt weg, Doppeltes auch."""
    return list(dict.fromkeys(n.strip().lower() for n in wert.split(",") if n.strip()))


def _und(namen: list[str]) -> str:
    if len(namen) <= 1:
        return "".join(namen)
    return ", ".join(namen[:-1]) + " und " + namen[-1]


def empfaenger_auswahl(namen: list[str], rest: str) -> str:
    """ "'paula', 'otto' oder eine Adresse"; ohne Namen nur der Rest."""
    if not namen:
        return rest
    return ", ".join(f"'{n}'" for n in namen) + " oder " + rest


def andere_empfaenger(namen: list[str]) -> str:
    """Für "Ist nicht klar, an wen, frag nach: an dich, an Otto, oder …"."""
    andere = [n.capitalize() for n in namen[1:]]
    if not andere:
        return "an dich oder eine andere Adresse."
    return "an dich, " + ", ".join(f"an {n}" for n in andere) + ", oder eine andere Adresse."


def spotify_konten_satz(konten: list[str]) -> str:
    """ "Zwei getrennte Konten im Haushalt: Paula und Otto." """
    namen = [k.capitalize() for k in dict.fromkeys(konten)]
    zahl = _ZAHLWORT.get(len(namen), str(len(namen)))
    wort = "getrenntes Konto" if len(namen) == 1 else "getrennte Konten"
    return f"{zahl} {wort} im Haushalt: {_und(namen)}."


EMPFAENGER = namen_liste(os.getenv("KITELEFON_PERSONEN", ""))
SPOTIFY_KONTEN = [
    os.getenv("SPOTIFY_ACCOUNT_1", SPOTIFY_STANDARD[0]),
    os.getenv("SPOTIFY_ACCOUNT_2", SPOTIFY_STANDARD[1]),
]
