"""RFI 'Journal en français facile' — Stoff fuer die Aufgabe (Spec 7.4).

Sprachlich fuer Lernende gemacht; drei Schlagzeilen reichen als
Aufhaenger, mehr wuerde den Prompt fluten.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import urllib.request
import xml.etree.ElementTree as ET
import xml.parsers.expat

log = logging.getLogger(__name__)

# RFI hat den Feed umgezogen: die alte Podcast-URL unter www.rfi.fr
# liefert seit spaetestens 19.08. eine 404-Seite. Die aktuelle Adresse
# stammt aus dem Apple-Podcasts-Eintrag der Sendung (iTunes-Lookup zu
# id=1573764973) und liefert 24 Folgen.
FEED_URL = (
    "https://apis.fle.rfi.fr/products/get_product/"
    "fle_getpodcast_by_nid_author_rfi"
    "?token_application=applepodcast_fle"
    "&program.entrepriseId=WBMZ39-FLE-FR-20220627"
)

# Ohne eigenen User-Agent antwortet RFI mit 403: der Standard von urllib
# ("Python-urllib/3.x") steht auf der Sperrliste. Jeder eigene Name geht
# durch — geprueft am 22.08. vom Pi und vom Mac.
USER_AGENT = "kitelefon/0.1 (Sprachkurs; +https://github.com/dragondiver/ki-telefon)"
MAX_EINTRAEGE = 3
TIMEOUT_S = 5

# Ein gelungenes Ergebnis bleibt zwei Stunden gueltig — deutlich laenger als
# jede Sitzung (Sitzungsuhr: 21 min). Der Vorrat wird beim Anrufstart
# gefuellt (__main__), damit der Tool-Aufruf im Gespraech nicht auf die
# RFI-API wartet. Fehler landen NIE im Vorrat — der Prompt sagt Ausfaelle
# ehrlich an.
NEWS_VORRAT_S = 7200
_vorrat: tuple[float, dict] | None = None


def vorrat_leeren() -> None:
    global _vorrat
    _vorrat = None


def vorrat_altern(sekunden: float) -> None:
    """Nur fuer Tests: den Eintrag um `sekunden` aelter machen."""
    global _vorrat
    if _vorrat is not None:
        t, wert = _vorrat
        _vorrat = (t - sekunden, wert)


def _parse_ohne_dtd(xml_text: str) -> ET.Element:
    """RSS parsen, aber jedes DOCTYPE abweisen.

    xml.etree.ElementTree.fromstring() verarbeitet interne <!ENTITY>-
    Deklarationen mit — ein boesartiger/gekaperter Feed koennte per
    "billion laughs" den Prozessspeicher sprengen, ohne dass irgendwo eine
    Exception das faengt (es gibt keinen Fehler, nur eine riesige
    Zeichenkette). RSS braucht kein DOCTYPE, also reicht es, jedes
    abzulehnen — ueber expat direkt, weil die ElementTree-eigene
    XMLParser-Klasse den zugrundeliegenden expat-Parser in der
    C-beschleunigten Variante nicht mehr als Attribut freigibt.
    """
    builder = ET.TreeBuilder()
    parser = xml.parsers.expat.ParserCreate()
    parser.buffer_text = True

    def _kein_doctype(*_args: object) -> None:
        raise ValueError("DOCTYPE im Feed nicht erlaubt")

    parser.StartDoctypeDeclHandler = _kein_doctype
    parser.StartElementHandler = lambda name, attrs: builder.start(name, attrs)
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.Parse(xml_text, True)
    return builder.close()


async def _fetch() -> str:
    def _laden() -> str:
        anfrage = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(anfrage, timeout=TIMEOUT_S) as antwort:
            return antwort.read().decode("utf-8", errors="replace")

    return await asyncio.to_thread(_laden)


async def get_news_fr() -> dict:
    global _vorrat

    eintrag = _vorrat
    if eintrag is not None:
        alter = time.monotonic() - eintrag[0]
        if alter < NEWS_VORRAT_S:
            log.debug("News aus dem Vorrat (%.0f s alt)", alter)
            # Kopie statt Alias — die Nachrichten-Liste ist veraenderlich,
            # ein mutierender Aufrufer wuerde sonst den Vorrat fuer zwei
            # Stunden vergiften (Finalreview Plan 4, 19.08.).
            return copy.deepcopy(eintrag[1])

    try:
        roh = await _fetch()
        baum = _parse_ohne_dtd(roh)
        eintraege = []
        for item in baum.iter("item"):
            titel = (item.findtext("title") or "").strip()
            teaser = (item.findtext("description") or "").strip()
            if titel:
                eintraege.append({"titel": titel, "teaser": teaser})
            if len(eintraege) >= MAX_EINTRAEGE:
                break
        ergebnis = {"nachrichten": eintraege}
        # Schnappschuss beim SCHREIBEN — siehe weather.py: der erste
        # Aufrufer haelt sonst das Vorrats-Objekt selbst.
        _vorrat = (time.monotonic(), copy.deepcopy(ergebnis))
        return ergebnis
    except Exception as exc:
        log.warning("get_news_fr fehlgeschlagen: %s", exc)
        return {"fehler": "Nachrichten gerade nicht erreichbar."}
