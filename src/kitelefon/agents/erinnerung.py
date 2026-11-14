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

# Opus 5.5 denkt immer mit (abschalten geht nicht), deshalb Aufwand "low"
# und max_tokens mit Luft fuers Denken. Messung 14.11.2026 am Hein-Anruf:
# 4.7 speicherte zweimal "Spricht leise." aus einem Tontest, 5.5 keinen
# Fakt; beide ~2,5 s.
MODEL = "claude-opus-5-5"
EFFORT = "low"
# Lehnt das Modell ab, faehrt die API die Anfrage auf einem Ersatzmodell.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# So steht der Anrufer im Verlauf (realtime.py), die Figur steht dort mit
# ihrem Rollennamen.
ANRUFER = "Anrufer"
# Darunter hat der Anrufer nichts gesagt, was man sich merken koennte.
MIN_ANRUFER_ZEICHEN = 40

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
    # Nur die Saetze des Anrufers zaehlen fuer die Mindestlaenge. Vorher
    # zaehlte das ganze Transkript: Holgers Begruessung allein (~150
    # Zeichen, kein Wort vom Anrufer) kam durch, und das Modell lieferte
    # "Heisst Holger." als Fakt ueber den Anrufer (#21).
    anrufer = " ".join(was.strip() for wer, was in verlauf if wer == ANRUFER and was.strip())
    if len(anrufer) < MIN_ANRUFER_ZEICHEN:
        # Ein Gruss und ein Auflegen ergeben keine Erinnerung.
        return None
    text = transkript_text(verlauf)
    try:
        response = await _get_client().beta.messages.parse(
            model=MODEL,
            max_tokens=4096,
            output_config={"effort": EFFORT},
            betas=[FALLBACK_BETA],
            fallbacks="default",
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
        if response.stop_reason == "refusal":
            log.warning("Erinnerung: Modell hat abgelehnt")
            return None
        return response.parsed_output
    except anthropic.APIError as exc:
        log.warning("Erinnerung konnte nicht destilliert werden: %s", exc)
        return None
