"""Werkzeugaufrufe des Realtime-Modells ohne Telefonanruf pruefen (#111).

Laesst die Rolle "Künstliche Intelligenz" gegen die OpenAI-Realtime-API laufen,
mit TEXT statt Audio: Satz als Texteingabe, Antwort abwarten, auswerten, welches
Werkzeug mit welchen Argumenten gerufen wurde und ob der Text eine Zusage
("notiert") VOR dem Aufruf machte. Gemessen werden Prompt, Werkzeug-Schemas und
Modell - nicht Spracherkennung und Raumakustik.

Ein Fall kann aus mehreren Saetzen (Zuegen) bestehen: Zug 2 wird erst gesendet,
wenn die Antwort auf Zug 1 samt Werkzeugaufrufen und Fake-Ergebnissen vollstaendig
da ist, im selben Gespraech. Ausgewertet wird je Zug und je Fall. `--max-laeufe`
zaehlt Laeufe (Gespraeche), nicht Zuege.

Werkzeuge werden NICHT ausgefuehrt. Jeder Aufruf wird mit einem festen
Fake-Ergebnis beantwortet, damit das Modell weiterspricht.

Von Hand auf dem Mac, nicht in der CI, nicht per Cron (Entscheidung 09.10.2026).
Der Schluessel kommt aus der Umgebung (OPENAI_API_KEY), nie aus dem Repo und
nie in die Ausgabe:

    set -a; . ~/Development/telefon-ai/.env; set +a
    uv run python scripts/werkzeugaufruf_probe.py --wiederholungen 5

Der Lauf kostet Geld (jeder Lauf ist ein paar Antworten des Realtime-Modells);
`--max-laeufe` begrenzt die Gesamtzahl. Es gibt keine harte Schwelle: niedrige
Quoten sind ein Befund, kein Fehler. Exit-Code ungleich 0 nur bei technischen
Fehlern (kein Schluessel, Verbindung, API-Fehler).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from kitelefon.haushalt import Haushalt
from kitelefon.realtime import OPENAI_REALTIME_URL, Transport, WebsocketTransport
from kitelefon.roles import Role, full_instructions, rolle_nach_name
from kitelefon.tools import ALL_TOOL_SCHEMAS

ROLLE = "Künstliche Intelligenz"
STANDARD_MODELL = "gpt-realtime-2.1-mini"  # wie Settings.openai_model
LAUF_TIMEOUT_S = 90.0
RATENLIMIT_VERSUCHE = 4  # Der Prompt ist gross (~11k Tokens je Antwort), TPM-Limit 200k
RATENLIMIT_PAUSE_S = 10.0
schlafe = asyncio.sleep  # austauschbar fuer Tests
MAX_RUNDEN = 4  # Antwort -> Werkzeug -> Antwort ... ; Schutz gegen Endlosschleifen

# Vorhandenes Thema im Prompt, wie es das Telefon aus der Haushalts-DB baut.
STARTTHEMEN = [("Urlaub Italien", ["Hotel in Rimini buchen.", "Sonnenhut einpacken."])]

# Text, der etwas als erledigt zusagt. Bewusst grob und tolerant.
ZUSAGE_MUSTER = re.compile(
    r"notiert|aufgeschrieben|gemerkt|gespeichert|speichere|eingetragen|"
    r"vermerkt|erledigt|hinzugef(ü|ue)gt|schreib(e)? .*\bauf\b|"
    r"setz(e)? ich|hab(e)? ich .*(auf|in) (die|der|dem)|"
    r"ruf(e)? (dich|ich dich) .*zur(ü|ue)ck",
    re.IGNORECASE,
)


def norm(text: str) -> str:
    """Kleinschreibung ohne Umlaute/Akzente/Leerzeichen - fuer tolerante Vergleiche."""
    klein = text.casefold()
    for umlaut, ersatz in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        klein = klein.replace(umlaut, ersatz)
    zerlegt = unicodedata.normalize("NFKD", klein)
    ohne = "".join(c for c in zerlegt if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", ohne)


@dataclass(frozen=True)
class Zug:
    """Ein Satz im Gespraech und was darauf erwartet wird."""

    satz: str
    erwartet: str | None  # Werkzeugname; None = kein Werkzeug (richtig = keins gerufen)
    # Argumentname -> Teilstring, der (normalisiert) im Argument stehen muss.
    argument_enthaelt: dict[str, str] = field(default_factory=dict)
    # Zusagen wie "notiert" nur dort suchen, wo das Werkzeug etwas verspricht.
    zusage_pruefen: bool = True
    # True: Aufruf oder Rueckfrage sind beide zulaessig; wird erfasst, aber nicht bewertet.
    informativ: bool = False
    # True: das erwartete Werkzeug zaehlt auch, wenn es schon in einem frueheren Zug kam.
    auch_frueher: bool = False
    # Ausnahmefaelle (#118): Rueckfrage ohne Aufruf zaehlt als richtig. Kommt ein Aufruf des
    # erwarteten Werkzeugs, muss er argument_enthaelt erfuellen und darf keinen der
    # argument_verboten-Teilstrings (normalisiert) enthalten - sonst wurde still etwas angenommen.
    rueckfrage_ok: bool = False
    argument_verboten: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fall:
    """Ein Fall: entweder ein Satz (str, alte Form) oder eine Folge von Zuegen.

    Mit einem String stehen erwartet/argument_enthaelt/zusage_pruefen am Fall und
    ergeben genau einen Zug. Mit einer Zugfolge gehoert alles in die Zuege.
    """

    name: str
    satz: str | Sequence[Zug]
    erwartet: str | None = None  # nur Einzelsatz-Form
    argument_enthaelt: dict[str, str] = field(default_factory=dict)
    zusage_pruefen: bool = True
    # False: nicht aussagekraeftig, wird nicht bewertet (Hinweistext in der Tabelle).
    aussagekraeftig: bool = True
    hinweis: str = ""
    # Werkzeugname -> festes Ergebnis nur fuer diesen Fall (statt FAKE_ERGEBNISSE).
    fake_ergebnisse: dict[str, dict] = field(default_factory=dict)
    zuege: tuple[Zug, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        if isinstance(self.satz, str):
            zuege = (Zug(self.satz, self.erwartet, self.argument_enthaelt, self.zusage_pruefen),)
        else:
            zuege = tuple(self.satz)
            if not zuege:
                raise ValueError(f"Fall {self.name}: keine Zuege")
            if self.erwartet is not None or self.argument_enthaelt:
                raise ValueError(f"Fall {self.name}: Erwartung gehoert bei Zugfolgen in die Zuege")
        object.__setattr__(self, "zuege", zuege)

    @property
    def erwartet_text(self) -> str:
        if len(self.zuege) == 1:
            return self.zuege[0].erwartet or "(keins)"
        return f"{len(self.zuege)} Züge"


FAELLE: list[Fall] = [
    Fall(
        "merke_italienurlaub",
        "Merk dir für den Italienurlaub, wir brauchen noch eine Sonnencreme.",
        "merke",
        {"thema": "italien"},
    ),
    Fall(
        "merke_aufschreiben",
        "Schreib dir auf: Thema Urlaub Italien, wir brauchen einen Reiseführer.",
        "merke",
        {"thema": "italien"},
    ),
    Fall(
        "merke_neues_thema",
        "Merk dir für Urlaub Spanien einen Reiseführer.",
        "merke",
        {"thema": "spanien"},
    ),
    Fall("erinnere_italien", "Was haben wir für Italien schon?", "erinnere", zusage_pruefen=False),
    Fall(
        "einkaufsliste",
        "Setz Milch auf die Einkaufsliste.",
        "add_shopping_item",
        {"name": "milch"},
    ),
    Fall(
        "wetter",
        "Wie wird das Wetter morgen in Stuttgart?",
        "get_weather",
        {"ort": "stuttgart"},
        zusage_pruefen=False,
    ),
    # #141: verhoerter Ortsname plus "bei uns" — am 01.11. ging das zweimal an
    # web_suche. Erwartet ist nur das Werkzeug, der Ort darf beliebig sein.
    Fall(
        "wetter_bei_uns",
        "Wie ist das Wetter denn bei uns im Conwestheim?",
        "get_weather",
        zusage_pruefen=False,
    ),
    Fall(
        "rueckruf_delegation",
        (
            Zug(
                "Ruf mich zurück, sobald du die Öffnungszeiten der Zahnarztpraxis Dr. Schmidt "
                "in Stuttgart-Mitte rausgesucht hast.",
                None,
                zusage_pruefen=False,
                informativ=True,  # Rueckfrage/Zusammenfassung ODER Aufruf, beides zulaessig
            ),
            Zug(
                "Ja, passt so, ruf mich auf dem Telefon zurück.",
                "delegate_task",
                {"description": "zahnarzt", "deliver_via": "callback"},
                zusage_pruefen=False,
                auch_frueher=True,  # direkter Aufruf schon in Zug 1 ist ebenso richtig
            ),
        ),
    ),
    Fall(
        "vergiss_nach_ja",
        (
            Zug("Vergiss das Thema Urlaub Italien.", None, zusage_pruefen=False),
            Zug(
                "Ja, bitte vergessen.",
                "vergiss",
                {"thema": "italien"},
                zusage_pruefen=False,
            ),
        ),
    ),
    # #122: Elektra delegierte am 11.10. das Aufraeumen doppelter Notizen nach
    # "Kannst du das uebernehmen?" an delegate_task. Richtig ist vergiss oder eine Rueckfrage.
    Fall(
        "gedaechtnis_uebernehmen",
        (
            Zug(
                "Was haben wir für Urlaub Italien? Ich glaube, die Sonnencreme steht doppelt drin.",
                "erinnere",
                {"thema": "italien"},
                zusage_pruefen=False,
            ),
            Zug(
                "Kannst du das übernehmen?",
                "vergiss",
                {"thema": "italien"},
                rueckfrage_ok=True,
            ),
        ),
        fake_ergebnisse={
            "erinnere": {
                "ok": True,
                "thema": "Urlaub Italien",
                "notizen": [
                    "Hotel in Rimini buchen.",
                    "Sonnencreme kaufen.",
                    "Sonnencreme LSF 50 besorgen.",
                ],
            }
        },
    ),
    Fall(
        "web_laenderspiel",
        "Wie ging das Länderspiel gestern aus?",
        "web_suche",
        zusage_pruefen=False,
    ),
    Fall(
        "web_deutschland_gespielt",
        "Wie hat Deutschland gestern gespielt?",
        "web_suche",
        zusage_pruefen=False,
    ),
    Fall(
        "web_deutschlandspiel",
        "Wie war das Deutschlandspiel?",
        "web_suche",
        zusage_pruefen=False,
    ),
    # Ausnahmen (#118): hier darf NICHT still Deutschland angenommen werden. Richtig =
    # Rueckfrage (kein Aufruf) ODER web_suche, deren Frage die genannte Mannschaft/das Land
    # enthaelt und nicht auf "deutsch..." festgelegt ist (einfache Teilstring-Pruefung).
    Fall(
        "ausnahme_frauen",
        [
            Zug(
                "Wie ging das Länderspiel der Frauen gestern aus?",
                "web_suche",
                {"frage": "frauen"},
                zusage_pruefen=False,
                rueckfrage_ok=True,
                argument_verboten=("deutsch",),
            )
        ],
    ),
    Fall(
        "ausnahme_frankreich_spanien",
        [
            Zug(
                "Wie ging das Länderspiel Frankreich gegen Spanien gestern aus?",
                "web_suche",
                {"frage": "frankreich"},
                zusage_pruefen=False,
                rueckfrage_ok=True,
                argument_verboten=("deutsch",),
            )
        ],
    ),
    # Genannter Gegner bleibt ein Spiel Deutschlands: sofort suchen, keine Rueckfrage.
    Fall(
        "web_gegen_frankreich",
        "Wie ging das Länderspiel gegen Frankreich gestern aus?",
        "web_suche",
        {"frage": "frankreich"},
        zusage_pruefen=False,
    ),
    # Gegenprobe (#118): kein Land genannt, Nachfrage ist hier richtig. Aufruf oder
    # Rueckfrage beide zulaessig, nur gezaehlt (Zeile "Antwort" zeigt, was passierte).
    Fall(
        "gegenprobe_spiel_gestern",
        [Zug("Wie ging das Spiel gestern aus?", None, informativ=True)],
    ),
    Fall("gegenprobe_smalltalk", "Hallo, wie geht’s?", None),
]


# Feste Fake-Ergebnisse. Nichts davon wird ausgefuehrt.
def _fake_merke(args: dict) -> dict:
    thema = args.get("thema") or "?"
    return {"ok": True, "ack": f"Unter '{thema}' notiert (2 Notizen)."}


FAKE_ERGEBNISSE: dict[str, Callable[[dict], dict]] = {
    "merke": _fake_merke,
    "erinnere": lambda a: {
        "ok": True,
        "thema": a.get("thema") or "Urlaub Italien",
        "notizen": ["Hotel in Rimini buchen.", "Sonnenhut einpacken."],
    },
    "vergiss": lambda a: {"ok": True, "geloescht": a.get("thema") or "?"},
    "add_shopping_item": lambda a: {"ok": True, "added": a.get("name", "?")},
    "get_weather": lambda a: {
        "ok": True,
        "zusammenfassung": "Morgen in Stuttgart meist sonnig, 17 Grad, kaum Wind.",
    },
    "delegate_task": lambda a: {"ok": True, "task_id": 4711, "status": "queued"},
    "web_suche": lambda a: {
        "ok": True,
        "antwort": "Deutschland gewann gestern 2:1. Quelle: Sportschau.",
    },
    "spotify_play": lambda a: {"ok": True, "spielt": a.get("query", "?")},
    "spotify_now_playing": lambda a: {"ok": True, "titel": "Nichts läuft gerade."},
    "suggest_recipe": lambda a: {"ok": True, "rezept": "Entwurf liegt vor."},
    "confirm_recipe": lambda a: {"ok": True},
    "modify_recipe": lambda a: {"ok": True},
    "discard_recipe": lambda a: {"ok": True},
    "zustellung_aendern": lambda a: {"ok": True},
    "ergebnis_per_mail": lambda a: {"ok": True},
    "freisprechen": lambda a: {"ok": True},
    "hoerer_zurueck": lambda a: {"ok": True},
}


def fake_ergebnis(name: str, args: dict, eigene: dict[str, dict] | None = None) -> dict:
    if eigene and name in eigene:
        return eigene[name]
    fn = FAKE_ERGEBNISSE.get(name)
    return fn(args) if fn else {"ok": True}


# --- Auswertung (rein, ohne Netz) -------------------------------------------


@dataclass
class Lauf:
    """Ergebnis eines Laufs."""

    aufrufe: list[tuple[str, dict]] = field(default_factory=list)
    text_vor_aufruf: str = ""  # alles, was vor dem ersten Aufruf gesprochen wurde
    text_gesamt: str = ""
    tokens_ein: int = 0
    tokens_aus: int = 0
    dauer_s: float = 0.0
    fehler: str | None = None
    fehler_nach_versuchen: int = 0
    # Mehrzug: je Zug ein eigener Lauf; die Felder oben sind dann die Summe ueber alle Zuege.
    zuege: list[Lauf] = field(default_factory=list)

    def zug_laeufe(self) -> list[Lauf]:
        return self.zuege or [self]


def fasse_zuege_zusammen(lauf: Lauf) -> None:
    """Die Gesamtfelder eines Mehrzug-Laufs aus seinen Zuegen fuellen."""
    lauf.aufrufe = [a for z in lauf.zuege for a in z.aufrufe]
    lauf.text_vor_aufruf = lauf.zuege[0].text_vor_aufruf if lauf.zuege else ""
    lauf.text_gesamt = " ".join(z.text_gesamt for z in lauf.zuege if z.text_gesamt)
    lauf.tokens_ein = sum(z.tokens_ein for z in lauf.zuege)
    lauf.tokens_aus = sum(z.tokens_aus for z in lauf.zuege)
    if not lauf.fehler:
        lauf.fehler = next((z.fehler for z in lauf.zuege if z.fehler), None)


def zusage(text: str) -> bool:
    """Sagt der Text etwas zu? Saetze, die als Frage enden, sind Angebote, keine Zusage."""
    saetze = re.split(r"(?<=[.!?])\s+", text)
    return any(ZUSAGE_MUSTER.search(x) for x in saetze if not x.rstrip().endswith("?"))


def lauf_aus_ereignissen(ereignisse: list[dict]) -> Lauf:
    """Eine Ereignisfolge der Server-Seite zu einem Lauf zusammenfassen.

    Text vor dem ersten function_call-Ereignis zaehlt als "vor dem Aufruf".
    """
    lauf = Lauf()
    for ev in ereignisse:
        auswerten_ereignis(lauf, ev)
    return lauf


def auswerten_ereignis(lauf: Lauf, ev: dict) -> None:
    typ = ev.get("type", "")
    if typ in {"response.output_text.done", "response.output_audio_transcript.done"}:
        text = ev.get("text") or ev.get("transcript") or ""
        lauf.text_gesamt = (lauf.text_gesamt + " " + text).strip()
        if not lauf.aufrufe:
            lauf.text_vor_aufruf = (lauf.text_vor_aufruf + " " + text).strip()
    elif typ == "response.function_call_arguments.done":
        try:
            args = json.loads(ev.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        lauf.aufrufe.append((ev.get("name", ""), args))
    elif typ == "response.done":
        antwort = ev.get("response") or {}
        status = antwort.get("status", "completed")
        if status in {"failed", "incomplete"} and not lauf.fehler:
            # Ohne das zaehlte ein Ratenlimit als "Modell hat nichts aufgerufen".
            fehler = (antwort.get("status_details") or {}).get("error") or {}
            lauf.fehler = f"{fehler.get('code') or status}: {fehler.get('message') or status}"[:300]
        usage = antwort.get("usage") or {}
        lauf.tokens_ein += int(usage.get("input_tokens") or 0)
        lauf.tokens_aus += int(usage.get("output_tokens") or 0)


def argumente_passen(zug: Zug | Fall, args: dict) -> bool:
    for schluessel, teil in zug.argument_enthaelt.items():
        if norm(teil) not in norm(str(args.get(schluessel, ""))):
            return False
    return True


def argumente_verboten(zug: Zug, args: dict) -> bool:
    text = norm(" ".join(str(v) for v in args.values()))
    return any(norm(v) in text for v in zug.argument_verboten)


@dataclass(frozen=True)
class Bewertung:
    richtig: (
        bool  # erwartetes Werkzeug (mit passenden Argumenten) gerufen bzw. keins bei Gegenprobe
    )
    werkzeug_gerufen: bool  # erwartetes Werkzeug ueberhaupt, Argumente egal
    falsches_werkzeug: bool  # irgendein anderes Werkzeug als das erwartete
    zusage_ohne_aufruf: bool  # Text sagt zu, aber das erwartete Werkzeug kam nie
    zusage_vor_aufruf: bool  # Text sagt zu, BEVOR das Werkzeug kam (Aufruf kam trotzdem)
    informativ: bool = False  # nur erfasst, in keiner Quote


def bewerte_zug(zug: Zug, lauf: Lauf) -> Bewertung:
    namen = [n for n, _ in lauf.aufrufe]
    if zug.informativ:
        return Bewertung(True, False, False, False, False, informativ=True)
    if zug.erwartet is None:
        return Bewertung(
            richtig=not namen,
            werkzeug_gerufen=False,
            falsches_werkzeug=bool(namen),
            zusage_ohne_aufruf=False,
            zusage_vor_aufruf=False,
        )
    passende = [a for n, a in lauf.aufrufe if n == zug.erwartet]
    gerufen = bool(passende)
    richtig = any(argumente_passen(zug, a) and not argumente_verboten(zug, a) for a in passende)
    if zug.rueckfrage_ok and not namen:
        richtig = True
    return Bewertung(
        richtig=richtig,
        werkzeug_gerufen=gerufen,
        falsches_werkzeug=any(n != zug.erwartet for n in namen),
        zusage_ohne_aufruf=zug.zusage_pruefen and (not gerufen) and zusage(lauf.text_gesamt),
        zusage_vor_aufruf=zug.zusage_pruefen and gerufen and zusage(lauf.text_vor_aufruf),
    )


def bewerte(fall: Fall, lauf: Lauf) -> Bewertung:
    """Einzelsatz-Fall bewerten (bei Mehrzug: der erste Zug); siehe bewerte_zuege."""
    return bewerte_zug(fall.zuege[0], lauf.zug_laeufe()[0])


def bewerte_zuege(fall: Fall, lauf: Lauf) -> list[Bewertung]:
    """Eine Bewertung je Zug. Fehlt ein Zug im Lauf, gilt er als leer."""
    laeufe = lauf.zug_laeufe()
    ergebnis = []
    for i, zug in enumerate(fall.zuege):
        zl = laeufe[i] if i < len(laeufe) else Lauf()
        if zug.auch_frueher and i > 0:
            # Aufrufe aller Zuege bis hier zaehlen; Text (Zusage-Pruefung) bleibt der des Zugs.
            zl = replace(zl, aufrufe=[a for z in laeufe[: i + 1] for a in z.aufrufe])
        ergebnis.append(bewerte_zug(zug, zl))
    return ergebnis


@dataclass
class ZugStatistik:
    zug: Zug
    laeufe: int = 0
    richtig: int = 0
    werkzeug_gerufen: int = 0
    falsches_werkzeug: int = 0
    zusage_ohne_aufruf: int = 0
    zusage_vor_aufruf: int = 0
    fehler: int = 0
    ohne_aufruf: int = 0
    aufrufe: Counter = field(default_factory=Counter)  # Werkzeugname -> Anzahl Aufrufe

    @property
    def quote(self) -> str:
        if self.zug.informativ:
            return "inf."
        return f"{self.richtig}/{self.laeufe - self.fehler}"


@dataclass
class FallStatistik:
    fall: Fall
    laeufe: int = 0
    richtig: int = 0  # alle bewerteten Zuege richtig
    werkzeug_gerufen: int = 0  # in allen Zuegen mit erwartetem Werkzeug gerufen
    falsches_werkzeug: int = 0  # in irgendeinem Zug
    zusage_ohne_aufruf: int = 0
    zusage_vor_aufruf: int = 0
    fehler: int = 0
    tokens_ein: int = 0
    tokens_aus: int = 0
    dauer_s: float = 0.0
    zuege: list[ZugStatistik] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.zuege:
            self.zuege = [ZugStatistik(z) for z in self.fall.zuege]

    @property
    def quote(self) -> str:
        if not self.fall.aussagekraeftig:
            return "n.b."
        if all(z.informativ for z in self.fall.zuege):
            return "inf."
        return f"{self.richtig}/{self.laeufe - self.fehler}"


def statistik(ergebnisse: list[tuple[Fall, Lauf]]) -> list[FallStatistik]:
    """Je Fall (in Reihenfolge des ersten Auftretens) die Zaehler bilden.

    Ein Lauf mit technischem Fehler zaehlt weder im Fall noch in einem seiner Zuege
    als richtig oder falsch (nur als Fehler), auch wenn ein frueher Zug fertig wurde.
    """
    stat: dict[str, FallStatistik] = {}
    for fall, lauf in ergebnisse:
        s = stat.setdefault(fall.name, FallStatistik(fall))
        s.laeufe += 1
        s.tokens_ein += lauf.tokens_ein
        s.tokens_aus += lauf.tokens_aus
        s.dauer_s += lauf.dauer_s
        for zs in s.zuege:
            zs.laeufe += 1
        if lauf.fehler:
            s.fehler += 1
            for zs in s.zuege:
                zs.fehler += 1
            continue
        bewertungen = bewerte_zuege(fall, lauf)
        laeufe = lauf.zug_laeufe()
        for i, (zs, b) in enumerate(zip(s.zuege, bewertungen, strict=True)):
            zl = laeufe[i] if i < len(laeufe) else Lauf()
            zs.aufrufe.update(n for n, _ in zl.aufrufe)
            zs.ohne_aufruf += not zl.aufrufe
            if b.informativ:
                continue
            zs.richtig += b.richtig
            zs.werkzeug_gerufen += b.werkzeug_gerufen
            zs.falsches_werkzeug += b.falsches_werkzeug
            zs.zusage_ohne_aufruf += b.zusage_ohne_aufruf
            zs.zusage_vor_aufruf += b.zusage_vor_aufruf
        bewertet = [
            (z, b) for z, b in zip(fall.zuege, bewertungen, strict=True) if not b.informativ
        ]
        s.richtig += all(b.richtig for _, b in bewertet)
        mit_werkzeug = [b for z, b in bewertet if z.erwartet is not None]
        s.werkzeug_gerufen += bool(mit_werkzeug) and all(b.werkzeug_gerufen for b in mit_werkzeug)
        s.falsches_werkzeug += any(b.falsches_werkzeug for _, b in bewertet)
        s.zusage_ohne_aufruf += any(b.zusage_ohne_aufruf for _, b in bewertet)
        s.zusage_vor_aufruf += any(b.zusage_vor_aufruf for _, b in bewertet)
    return list(stat.values())


def _zeile(name: str, erwartet: str, quote: str, s, tok: str, sek: str) -> str:
    return (
        f"{name:<24}{erwartet:<19}{quote:>6}"
        f"{s.werkzeug_gerufen:>6}{s.falsches_werkzeug:>7}{s.zusage_ohne_aufruf:>9}"
        f"{s.zusage_vor_aufruf:>9}{s.fehler:>7}{tok}{sek}"
    )


def tabelle(stats: list[FallStatistik]) -> str:
    kopf = (
        f"{'Fall / Zug':<24}{'erwartet':<19}{'Quote':>6}{'Wkz.':>6}{'falsch':>7}"
        f"{'Zus.o.A.':>9}{'Zus.v.A.':>9}{'Fehler':>7}{'Tok.ein':>9}{'Tok.aus':>9}{'s/Lauf':>8}"
    )
    zeilen = [kopf, "-" * len(kopf)]
    for s in stats:
        if not s.fall.aussagekraeftig:
            zeilen.append(
                f"{s.fall.name:<24}{s.fall.erwartet_text:<19}{'n.b.':>6}  "
                f"nicht bewertet ({s.fall.hinweis}; {s.laeufe} Läufe, nicht gezählt)"
            )
            continue
        zeilen.append(
            _zeile(
                s.fall.name,
                s.fall.erwartet_text,
                s.quote,
                s,
                f"{s.tokens_ein:>9}{s.tokens_aus:>9}",
                f"{s.dauer_s / max(s.laeufe, 1):>8.1f}",
            )
        )
        if len(s.zuege) > 1 or s.zuege[0].zug.informativ:
            for i, zs in enumerate(s.zuege, 1):
                name = f"  Zug {i}" if len(s.zuege) > 1 else "  Antwort"
                if zs.zug.informativ:
                    gerufen = ", ".join(f"{n}×{c}" for n, c in sorted(zs.aufrufe.items()))
                    zeilen.append(
                        f"{name:<24}{'(informativ)':<19}{'inf.':>6}  "
                        f"{zs.ohne_aufruf}× ohne Aufruf, Aufrufe: {gerufen or '-'}"
                        f" ({zs.laeufe - zs.fehler} Läufe, nicht gezählt)"
                    )
                else:
                    zeilen.append(_zeile(name, zs.zug.erwartet or "(keins)", zs.quote, zs, "", ""))
    bewertet = [s for s in stats if s.fall.aussagekraeftig]
    laeufe = sum(s.laeufe for s in bewertet)
    zuege = sum(s.laeufe * len(s.fall.zuege) for s in bewertet)
    zeilen += [
        "",
        "Legende: Quote = richtig / Läufe ohne technischen Fehler. Ein Lauf = ein Gespräch; bei",
        "Mehrzug-Fällen ist der Fall richtig, wenn ALLE bewerteten Züge richtig sind. Zug-Zeilen (eingerückt)",
        "zeigen dieselbe Quote je Zug. richtig = erwartetes Werkzeug mit passenden Argumenten; erwartet",
        "(keins) = es darf kein Werkzeug kommen. inf. = informativ: Aufruf und Rückfrage beide zulässig,",
        "nur gezählt, in keiner Quote. n.b. = nicht bewertet. Wkz. = erwartetes Werkzeug gerufen,",
        "Argumente egal. falsch = anderes Werkzeug gerufen. Zus.o.A. = Zusage (notiert/erledigt ...)",
        "ohne Aufruf. Zus.v.A. = Zusage VOR dem Aufruf.",
        f"Bewertete Läufe: {laeufe} (Züge: {zuege}). "
        f"Tokens aller Läufe: {sum(s.tokens_ein for s in stats)} ein / "
        f"{sum(s.tokens_aus for s in stats)} aus "
        "(von der API gemeldet; Preis hängt vom Modell ab, hier nicht berechnet).",
    ]
    return "\n".join(zeilen)


def _zeile_aufrufe(lauf: Lauf) -> str:
    aufrufe = "; ".join(f"{n}({json.dumps(a, ensure_ascii=False)})" for n, a in lauf.aufrufe)
    return f"Aufrufe: {aufrufe or '-'} | Text: {lauf.text_gesamt!r}"


def zeile_lauf(fall: Fall, lauf: Lauf) -> str:
    if lauf.fehler:
        return f"[{fall.name}] FEHLER {lauf.fehler}"
    laeufe = lauf.zug_laeufe()
    if len(laeufe) == 1:
        return f"[{fall.name}] {_zeile_aufrufe(laeufe[0])}"
    return "\n".join(f"[{fall.name} Z{i}] {_zeile_aufrufe(zl)}" for i, zl in enumerate(laeufe, 1))


# --- Lauf gegen die API -------------------------------------------------------


def baue_prompt_und_werkzeuge(rolle_name: str = ROLLE) -> tuple[Role, str, list]:
    """Rolle, vollstaendiger Prompt (mit Themenliste wie am Telefon), Schemas."""
    rolle = rolle_nach_name(rolle_name)
    if rolle is None:
        raise SystemExit(f"Rolle '{rolle_name}' nicht gefunden.")
    with tempfile.TemporaryDirectory() as tmp:
        haushalt = Haushalt(Path(tmp) / "haushalt.db")
        for thema, notizen in STARTTHEMEN:
            for notiz in notizen:
                haushalt.merke(thema, notiz)
        gedaechtnis = haushalt.themenzeile()
    # Wohnort wie am Telefon aus der .env (#141); fehlt er, ohne Wohnort-Satz.
    prompt = full_instructions(rolle, gedaechtnis, wohnort=os.environ.get("KITELEFON_WOHNORT", ""))
    return rolle, prompt, rolle.get("tool_schemas", ALL_TOOL_SCHEMAS)


def session_update(modell: str, prompt: str, werkzeuge: list) -> dict:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": modell,
            "instructions": prompt,
            "output_modalities": ["text"],
            "max_output_tokens": 4096,
            "tools": werkzeuge,
            "tool_choice": "auto",
        },
    }


async def _empfange(transport: Transport) -> dict:
    return json.loads(await transport.recv())


async def fuehre_lauf(
    transport: Transport,
    satz: str | Sequence[str],
    modell: str,
    prompt: str,
    werkzeuge: list,
    *,
    fakes: dict[str, dict] | None = None,
) -> Lauf:
    """Ein Gespraech: Saetze nacheinander senden, Werkzeuge mit Fakes beantworten.

    `satz` ist ein Satz oder die Saetze der Zuege. Zug n+1 wird erst gesendet, wenn
    die Antwort auf Zug n samt allen Werkzeugrunden fertig ist. `fakes` ersetzt
    einzelne Fake-Ergebnisse fuer diesen Lauf (Fall.fake_ergebnisse).
    """
    saetze = [satz] if isinstance(satz, str) else list(satz)
    lauf = Lauf()
    start = time.monotonic()
    try:
        await asyncio.wait_for(
            _gespraech(transport, lauf, saetze, modell, prompt, werkzeuge, fakes),
            LAUF_TIMEOUT_S,
        )
    except Exception as exc:  # technischer Fehler gehoert in die Zaehlung, nicht in den Abbruch
        lauf.fehler = f"{type(exc).__name__}: {exc}"
    finally:
        fasse_zuege_zusammen(lauf)
        lauf.dauer_s = time.monotonic() - start
        try:
            await transport.close()
        except Exception:
            pass
    return lauf


async def _gespraech(
    transport: Transport,
    lauf: Lauf,
    saetze: list[str],
    modell: str,
    prompt: str,
    werkzeuge: list,
    fakes: dict[str, dict] | None = None,
) -> None:
    ev = await _empfange(transport)
    while ev.get("type") != "session.created":
        _fehler_pruefen(ev)
        ev = await _empfange(transport)
    await transport.send(json.dumps(session_update(modell, prompt, werkzeuge), ensure_ascii=False))
    while (ev := await _empfange(transport)).get("type") != "session.updated":
        _fehler_pruefen(ev)
    for satz in saetze:
        zug = Lauf()
        lauf.zuege.append(zug)
        await _zug(transport, zug, satz, fakes)
        if zug.fehler:  # z.B. Ratenlimit: spaetere Zuege waeren ohne Kontext sinnlos
            lauf.fehler = zug.fehler
            return


async def _zug(
    transport: Transport, lauf: Lauf, satz: str, fakes: dict[str, dict] | None = None
) -> None:
    """Einen Satz senden und die Antwort samt Werkzeugrunden vollstaendig abwarten."""
    await transport.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": satz}],
                },
            },
            ensure_ascii=False,
        )
    )
    await transport.send(json.dumps({"type": "response.create"}))

    for _ in range(MAX_RUNDEN):
        offene: list[tuple[str, str, dict]] = []  # (call_id, name, args)
        while True:
            ev = await _empfange(transport)
            _fehler_pruefen(ev)
            auswerten_ereignis(lauf, ev)
            if ev.get("type") == "response.function_call_arguments.done":
                args = lauf.aufrufe[-1][1]
                offene.append((ev.get("call_id", ""), ev.get("name", ""), args))
            if ev.get("type") == "response.done":
                break
        if not offene or lauf.fehler:
            return
        for call_id, name, args in offene:
            await transport.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(
                                fake_ergebnis(name, args, fakes), ensure_ascii=False
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
            )
        await transport.send(json.dumps({"type": "response.create"}))


def _fehler_pruefen(ev: dict) -> None:
    if ev.get("type") == "error":
        raise RuntimeError(str(ev.get("error", ev))[:300])


Verbinder = Callable[[str], Awaitable[Transport]]


def api_verbinder(api_key: str) -> Verbinder:
    async def verbinde(modell: str) -> Transport:
        import websockets

        ws = await websockets.connect(
            f"{OPENAI_REALTIME_URL}?model={modell}",
            additional_headers={"Authorization": f"Bearer {api_key}"},
            max_size=None,
        )
        return WebsocketTransport(ws)

    return verbinde


async def probiere(
    faelle: list[Fall],
    wiederholungen: int,
    verbinde: Verbinder,
    *,
    parallel: int = 1,
    modell: str | None = None,
) -> list[tuple[Fall, Lauf]]:
    rolle, prompt, werkzeuge = baue_prompt_und_werkzeuge()
    modell = modell or rolle.get("model") or STANDARD_MODELL
    sem = asyncio.Semaphore(max(parallel, 1))

    async def einer(fall: Fall) -> tuple[Fall, Lauf]:
        async with sem:
            lauf = Lauf()
            for versuch in range(RATENLIMIT_VERSUCHE):
                try:
                    transport = await verbinde(modell)
                except Exception as exc:
                    return fall, Lauf(fehler=f"{type(exc).__name__}: {exc}")
                neu = await fuehre_lauf(
                    transport,
                    [z.satz for z in fall.zuege],
                    modell,
                    prompt,
                    werkzeuge,
                    fakes=fall.fake_ergebnisse,
                )
                # Verbrauchte Tokens und Zeit der abgebrochenen Versuche zaehlen mit.
                neu.tokens_ein += lauf.tokens_ein
                neu.tokens_aus += lauf.tokens_aus
                neu.dauer_s += lauf.dauer_s
                lauf = neu
                if "rate_limit" not in (lauf.fehler or ""):
                    break
                lauf.fehler_nach_versuchen = versuch + 1
                if versuch + 1 < RATENLIMIT_VERSUCHE:
                    await schlafe(RATENLIMIT_PAUSE_S * (versuch + 1))
                    lauf.fehler = None
            return fall, lauf

    auftraege = [einer(f) for f in faelle for _ in range(wiederholungen)]
    return list(await asyncio.gather(*auftraege))


# --- Kommandozeile ------------------------------------------------------------

KEIN_SCHLUESSEL = (
    "OPENAI_API_KEY ist nicht gesetzt. Es wurde nichts gesendet.\n"
    "Schlüssel nur über die Umgebung laden, ohne ihn auszugeben:\n"
    "  set -a; . ~/Development/telefon-ai/.env; set +a"
)


def main(argv: list[str] | None = None, *, verbinder: Verbinder | None = None) -> int:
    p = argparse.ArgumentParser(description="Werkzeugaufruf-Probe gegen die Realtime-API (Text).")
    p.add_argument("--wiederholungen", type=int, default=5, help="Läufe je Fall (Standard 5)")
    p.add_argument(
        "--max-laeufe",
        type=int,
        default=100,
        help="Obergrenze der Gesamtläufe; ein Lauf = ein Gespräch, Züge zählen nicht extra (Standard 100)",
    )
    p.add_argument("--fall", action="append", help="nur diesen Fall (Name, mehrfach möglich)")
    p.add_argument("--parallel", type=int, default=1, help="gleichzeitige Verbindungen")
    p.add_argument("--ausfuehrlich", action="store_true", help="je Lauf Aufrufe und Text zeigen")
    p.add_argument("--modell", help="Modell überschreiben (Standard: das der Rolle)")
    args = p.parse_args(argv)

    schluessel = os.environ.get("OPENAI_API_KEY", "").strip()
    if not schluessel and verbinder is None:
        print(KEIN_SCHLUESSEL, file=sys.stderr)
        return 2

    faelle = FAELLE
    if args.fall:
        namen = {f.name for f in FAELLE}
        unbekannt = [n for n in args.fall if n not in namen]
        if unbekannt:
            print(
                f"Unbekannte Fälle: {', '.join(unbekannt)}. Bekannt: {', '.join(sorted(namen))}",
                file=sys.stderr,
            )
            return 2
        faelle = [f for f in FAELLE if f.name in args.fall]
    if args.wiederholungen < 1:
        print("--wiederholungen muss mindestens 1 sein.", file=sys.stderr)
        return 2
    gesamt = len(faelle) * args.wiederholungen
    if gesamt > args.max_laeufe:
        print(
            f"{len(faelle)} Fälle x {args.wiederholungen} = {gesamt} Läufe, mehr als "
            f"--max-laeufe={args.max_laeufe}. Es wurde nichts gesendet.",
            file=sys.stderr,
        )
        return 2

    verbinde = verbinder or api_verbinder(schluessel)
    start = time.monotonic()
    ergebnisse = asyncio.run(
        probiere(faelle, args.wiederholungen, verbinde, parallel=args.parallel, modell=args.modell)
    )
    if args.ausfuehrlich:
        for fall, lauf in ergebnisse:
            print(zeile_lauf(fall, lauf))
        print()
    print(tabelle(statistik(ergebnisse)))
    print(f"Gesamtlaufzeit: {time.monotonic() - start:.1f} s")
    fehler = [(f.name, lf.fehler) for f, lf in ergebnisse if lf.fehler]
    if fehler:
        print(
            f"\nTechnische Fehler in {len(fehler)} Läufen, erster: {fehler[0][0]}: {fehler[0][1]}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
