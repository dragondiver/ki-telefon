"""Aus einem Telefonat wird ein Satz Erinnerung.

Laeuft nach dem Auflegen, nicht waehrend des Gespraechs — der Anrufer soll
davon nichts merken. Claude bekommt das Transkript und gibt zurueck, was
eine Bekanntschaft davon behalten wuerde: ein paar Vorlieben, ein paar
Personendaten, und einen Satz darueber, worum es ging.

Bewusst dasselbe Muster wie recipes.py (messages.parse + Pydantic), damit
es im Projekt nur eine Art gibt, Claude strukturiert zu befragen.
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

from kitelefon.memory import MAX_FAKTEN

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """Du destillierst aus einem Telefongespräch, was sich ein \
Bekannter davon merken würde. Nicht mehr.

Fakten:
- Nur was der ANRUFER über sich preisgibt: Vorlieben, Abneigungen, Beruf,
  Wohnort, Familie, laufende Vorhaben.
- Formuliere jeden Fakt als kurzen, für sich verständlichen Satz in der
  dritten Person: "Mag keinen Koriander." statt "er sagte, dass …".
- KEINE Fakten über die Kunstfigur selbst, keine Wetter- oder
  Musikabfragen, nichts Beiläufiges, nichts Erfundenes.
- Lieber gar keinen Fakt als einen unsicheren. Eine leere Liste ist eine
  richtige Antwort.

Zusammenfassung:
- EIN Satz, worum es in diesem Gespräch ging. Höchstens 20 Wörter.
- Keine Anrede, kein "Der Anrufer …" — nur die Sache selbst.
"""


class Erinnerung(BaseModel):
    fakten: list[str] = Field(default_factory=list, max_length=MAX_FAKTEN)
    zusammenfassung: str = Field(default="", max_length=300)


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def transkript_text(verlauf: list[tuple[str, str]]) -> str:
    return "\n".join(f"{wer}: {was}" for wer, was in verlauf if was.strip())


async def destilliere(verlauf: list[tuple[str, str]]) -> Erinnerung | None:
    """None, wenn nichts zu holen ist oder Claude nicht antwortet.

    Ein Fehler hier darf nie ein Telefonat beeintraechtigen — das Gespraech
    ist zu diesem Zeitpunkt ohnehin schon vorbei.
    """
    text = transkript_text(verlauf)
    if len(text) < 80:
        # Ein Gruss und ein Auflegen ergeben keine Erinnerung.
        return None
    try:
        response = await _get_client().messages.parse(
            model=MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": text}],
            output_format=Erinnerung,
        )
        return response.parsed_output
    except anthropic.APIError as exc:
        log.warning("Erinnerung konnte nicht destilliert werden: %s", exc)
        return None
