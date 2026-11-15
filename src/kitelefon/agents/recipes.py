"""Rezept-Vorschläge via Claude API + Save-in-mise-en-place.

Flow:
  1. suggest_recipe(query): Claude generiert strukturiertes Rezept,
     in _draft gespeichert. Rückgabe = kurze, vorlesbare Zusammenfassung
     für die Realtime-KI.
  2. Nutzer diskutiert mit Realtime-KI → ggf. suggest_recipe erneut
     (überschreibt _draft).
  3. confirm_recipe(): _draft an mise-en-place posten. Die zurückgegebene
     id merken wir uns in _saved.
  4. modify_recipe(change): ändert den Entwurf — oder, wenn schon
     gespeichert wurde, das gespeicherte Rezept per PUT. Ohne das legte
     jede Verfeinerung nach dem Speichern ein zweites, fast identisches
     Rezept an.
  5. discard_recipe(): _draft verwerfen.

Single-User-Device (ein Telefonat zur Zeit) → globale _draft/_saved reichen.
"""

from __future__ import annotations

import logging
import os
from typing import Literal
from urllib.parse import quote

import anthropic
import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
log = logging.getLogger(__name__)

# Opus 5.5 denkt immer mit, deshalb Aufwand "low" und mehr max_tokens.
# Messung 14.11.2026: 8,5-11 s statt 13-17 s mit Opus 4.7, gleichwertiges Rezept.
MODEL = "claude-opus-5-5"
EFFORT = "low"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
UNITS = Literal[
    "g", "kg", "ml", "l", "stueck", "el", "tl", "prise", "bund", "dose", "packung", "handvoll"
]


class Ingredient(BaseModel):
    name: str = Field(..., min_length=1)
    amount: float = Field(default=1, ge=0)
    unit: UNITS = "stueck"
    category: str = "Sonstiges"


class Recipe(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    servings: int = Field(default=4, ge=1, le=100)
    prep_time_minutes: int | None = Field(default=None, ge=0)
    cook_time_minutes: int | None = Field(default=None, ge=0)
    ingredients: list[Ingredient] = Field(..., min_length=1)
    instructions: list[str] = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)


SYSTEM_PROMPT = """Du bist ein erfahrener Hobbykoch und entwirfst alltagstaugliche \
Familien-Rezepte für Mitteleuropa. Antworten knapp, präzise und gut umsetzbar.

Regeln:
- Mengen für die angegebene Anzahl Portionen
- Zutaten mit Mengen + Einheit (g, kg, ml, l, stueck, el, tl, prise, bund, dose, packung, handvoll)
- 4–10 Zubereitungsschritte, jeweils ein klarer Satz
- Tags als kurze Stichworte (z.B. 'vegetarisch', 'schnell', 'pasta')
- Zeitangaben in Minuten, realistisch
- Wenn unklar: vernünftige Annahmen treffen, nicht zurückfragen
- Antwortsprache: Deutsch"""


_draft: Recipe | None = None
# (id in mise-en-place, zuletzt gespeicherte Fassung) — Grundlage fürs Ändern
# nach dem Speichern.
_saved: tuple[str, Recipe] | None = None
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def _format_summary(recipe: Recipe) -> str:
    """Kurze, vorlesbare Zusammenfassung für die Realtime-KI."""
    times = []
    if recipe.prep_time_minutes:
        times.append(f"{recipe.prep_time_minutes} Min Vorbereitung")
    if recipe.cook_time_minutes:
        times.append(f"{recipe.cook_time_minutes} Min Kochen")
    zeit = ", ".join(times) if times else "Zeit nicht angegeben"
    top_zutaten = ", ".join(i.name for i in recipe.ingredients[:5])
    extra = f" und {len(recipe.ingredients) - 5} weitere" if len(recipe.ingredients) > 5 else ""
    return (
        f"{recipe.title} für {recipe.servings} Portionen. "
        f"Zutaten: {top_zutaten}{extra}. "
        f"{zeit}. "
        f"{len(recipe.instructions)} Schritte."
    )


_EINHEIT_LANG = {
    "g": "Gramm",
    "kg": "Kilo",
    "ml": "Milliliter",
    "l": "Liter",
    "stueck": "Stück",
    "el": "Esslöffel",
    "tl": "Teelöffel",
    "prise": "Prise",
    "bund": "Bund",
    "dose": "Dose",
    "packung": "Packung",
    "handvoll": "Handvoll",
}


def _menge(zutat: Ingredient) -> str:
    """Vorlesbare Menge: '200 Gramm Mehl', nicht '200.0 g Mehl'."""
    zahl = int(zutat.amount) if zutat.amount == int(zutat.amount) else zutat.amount
    einheit = _EINHEIT_LANG.get(zutat.unit, zutat.unit)
    if zutat.unit == "stueck":
        return f"{zahl} {zutat.name}"
    return f"{zahl} {einheit} {zutat.name}"


def _details(recipe: Recipe) -> dict:
    """Das vollständige Rezept für die Realtime-KI.

    Bis 2026-08-08 bekam sie nur die Zusammenfassung zurück — Mengen und
    Zubereitungsschritte hat sie nie gesehen. Auf "lies mal die Schritte
    vor" blieb ihr deshalb nur, sie zu erfinden, und genau das tat sie
    auch: Jean-Luc spülte Kichererbsen, die im Rezept gar nicht vorkamen.
    """
    return {
        "titel": recipe.title,
        "portionen": recipe.servings,
        "zutaten": [_menge(z) for z in recipe.ingredients],
        "schritte": list(recipe.instructions),
    }


class KeinRezept(Exception):
    """Claude hat kein Rezept geliefert (Ablehnung oder leere Antwort)."""


async def _generate_recipe(query: str) -> Recipe:
    """Ein Rezept von Claude erzeugen. Eigene Funktion, damit Tests sie ersetzen."""
    response = await _get_client().beta.messages.parse(
        model=MODEL,
        max_tokens=8192,
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
        messages=[{"role": "user", "content": query}],
        output_format=Recipe,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise KeinRezept(f"stop_reason={response.stop_reason}")
    return response.parsed_output


def _aenderungs_prompt(recipe: Recipe, change: str) -> str:
    """Bestehendes Rezept + Änderungswunsch → Prompt für die Neufassung."""
    return (
        "Hier ist ein bestehendes Rezept als JSON:\n\n"
        f"{recipe.model_dump_json(indent=2)}\n\n"
        f"Ändere es so: {change}\n\n"
        "Gib das vollständige geänderte Rezept zurück, nicht nur die Änderung. "
        "Behalte alles bei, was der Änderungswunsch nicht betrifft — "
        "insbesondere den Titel, sofern die Änderung ihn nicht unpassend macht."
    )


async def handle_suggest_recipe(args: dict) -> dict:
    """Generiert ein Rezept via Claude und legt es als Draft ab."""
    global _draft
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "ack": "Was für ein Rezept soll ich vorschlagen?"}

    try:
        _draft = await _generate_recipe(query)
    except (anthropic.APIError, KeinRezept) as exc:
        log.exception("recipe generation failed")
        return {
            "ok": False,
            "ack": "Mir fällt gerade kein Rezept ein, die KI antwortet nicht.",
            "error": str(exc),
        }

    summary = _format_summary(_draft)
    log.info(
        "recipe draft: %s (%d ingredients, %d steps)",
        _draft.title,
        len(_draft.ingredients),
        len(_draft.instructions),
    )
    return {"ok": True, "ack": summary, "title": _draft.title, **_details(_draft)}


def _payload(recipe: Recipe) -> dict:
    payload = {
        "title": recipe.title,
        "description": recipe.description,
        "servings": recipe.servings,
        "prepTime": recipe.prep_time_minutes,
        "cookTime": recipe.cook_time_minutes,
        "instructions": recipe.instructions,
        "tags": recipe.tags,
        "ingredients": [i.model_dump() for i in recipe.ingredients],
        "source": "ai_generated",
        "language": "de",
    }
    return {k: v for k, v in payload.items() if v is not None}


def _mise_en_place() -> tuple[str, str] | None:
    base_url = os.getenv("KITELEFON_MISE_EN_PLACE_URL", "").rstrip("/")
    secret = os.getenv("KITELEFON_MISE_EN_PLACE_SECRET", "")
    if not base_url or not secret:
        return None
    return base_url, secret


async def handle_confirm_recipe(args: dict, transport=None) -> dict:
    """Aktuellen Draft an mise-en-place posten."""
    global _draft, _saved
    if _draft is None:
        return {"ok": False, "ack": "Da ist noch kein Rezept zum Speichern."}

    ziel = _mise_en_place()
    if ziel is None:
        return {"ok": False, "ack": "Mise-en-Place ist nicht konfiguriert."}
    base_url, secret = ziel

    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            r = await client.post(
                f"{base_url}/api/agent/recipes",
                headers={"Authorization": f"Bearer {secret}"},
                json=_payload(_draft),
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "ack": "Mise-en-Place antwortet gerade nicht.", "error": str(exc)}

    if r.status_code in (200, 201):
        title = _draft.title
        # id merken, damit ein späteres "mach's mit Hähnchen" dieses Rezept
        # ändert statt ein zweites anzulegen.
        try:
            recipe_id = r.json().get("id")
        except ValueError:
            recipe_id = None
        _saved = (recipe_id, _draft) if recipe_id else None
        _draft = None
        return {"ok": True, "ack": f"'{title}' habe ich in Mise-en-Place gespeichert."}
    log.error("save recipe failed: %s %s", r.status_code, r.text)
    return {"ok": False, "ack": "Speichern hat nicht geklappt.", "error": f"HTTP {r.status_code}"}


async def handle_modify_recipe(args: dict, transport=None) -> dict:
    """Ändert den Entwurf — oder das zuletzt gespeicherte Rezept per PUT."""
    global _draft, _saved
    change = (args.get("change") or "").strip()
    if not change:
        return {"ok": False, "ack": "Was soll ich am Rezept ändern?"}

    if _draft is None and _saved is None:
        return {"ok": False, "ack": "Da ist kein Rezept, das ich ändern könnte."}

    basis = _draft if _draft is not None else _saved[1]  # type: ignore[index]
    try:
        neu = await _generate_recipe(_aenderungs_prompt(basis, change))
    except (anthropic.APIError, KeinRezept) as exc:
        log.exception("recipe modification failed")
        return {
            "ok": False,
            "ack": "Das Ändern hat nicht geklappt, die KI antwortet nicht.",
            "error": str(exc),
        }

    # Noch nicht gespeichert: nur den Entwurf ersetzen, nichts hochladen.
    if _draft is not None:
        _draft = neu
        log.info("recipe draft geändert: %s", neu.title)
        return {"ok": True, "gespeichert": False, "ack": _format_summary(neu), **_details(neu)}

    recipe_id, _ = _saved  # type: ignore[misc]
    ziel = _mise_en_place()
    if ziel is None:
        return {"ok": False, "ack": "Mise-en-Place ist nicht konfiguriert."}
    base_url, secret = ziel

    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            r = await client.put(
                f"{base_url}/api/agent/recipes/{recipe_id}",
                headers={"Authorization": f"Bearer {secret}"},
                json=_payload(neu),
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "ack": "Mise-en-Place antwortet gerade nicht.", "error": str(exc)}

    if r.status_code in (200, 201):
        _saved = (recipe_id, neu)
        log.info("recipe %s aktualisiert: %s", recipe_id, neu.title)
        return {
            "ok": True,
            "gespeichert": True,
            "ack": f"Ich habe '{neu.title}' in Mise-en-Place geändert.",
            **_details(neu),
        }

    # Das gespeicherte Rezept ist weg (gelöscht?) — ehrlich sein, nicht
    # heimlich ein zweites anlegen. Genau das wollten wir ja loswerden.
    if r.status_code == 404:
        _saved = None
        _draft = neu
        return {
            "ok": False,
            "gespeichert": False,
            "ack": (
                "Das Rezept finde ich in Mise-en-Place nicht mehr. "
                "Ich habe die geänderte Fassung als Entwurf — soll ich sie "
                "neu speichern?"
            ),
        }

    log.error("update recipe failed: %s %s", r.status_code, r.text)
    return {"ok": False, "ack": "Das Ändern hat nicht geklappt.", "error": f"HTTP {r.status_code}"}


async def handle_discard_recipe(args: dict) -> dict:
    global _draft, _saved
    _saved = None
    if _draft is None:
        return {"ok": True, "ack": "Okay, kein Rezept gespeichert."}
    title = _draft.title
    _draft = None
    return {"ok": True, "ack": f"'{title}' verworfen."}


# --- Kochbuch durchsuchen (mise-en-place, nur lesend) -------------------------

MAX_TREFFER = 3
FILTER = ("quick", "proteinRich", "fiberRich")
_MIN_FEHLER = "Mise-en-Place antwortet gerade nicht."


async def _kochbuch_get(
    pfad: str, params: dict | None, transport
) -> tuple[dict | None, dict | None]:
    """GET gegen die Agent-API. Liefert (json, None) oder (None, fertiges Fehlerergebnis).

    Nie eine Exception nach aussen: ein Netzfehler darf das Gespräch nicht stören.
    """
    ziel = _mise_en_place()
    if ziel is None:
        return None, {"ok": False, "ack": "Mise-en-Place ist nicht konfiguriert."}
    base_url, secret = ziel
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            r = await client.get(
                f"{base_url}{pfad}",
                headers={"Authorization": f"Bearer {secret}"},
                params=params,
            )
    except httpx.HTTPError as exc:
        log.warning("kochbuch %s: %s", pfad, exc)
        return None, {"ok": False, "ack": _MIN_FEHLER, "error": str(exc)}

    if r.status_code == 404:
        return None, {"ok": False, "ack": "Dieses Rezept gibt es im Kochbuch nicht."}
    if r.status_code in (401, 403):
        log.error("kochbuch %s: %s, Secret falsch?", pfad, r.status_code)
        return None, {
            "ok": False,
            "ack": "Ich komme gerade nicht ins Kochbuch.",
            "error": f"HTTP {r.status_code}",
        }
    if r.status_code != 200:
        log.error("kochbuch %s failed: %s %s", pfad, r.status_code, r.text)
        return None, {
            "ok": False,
            "ack": "Die Suche im Kochbuch hat nicht geklappt.",
            "error": f"HTTP {r.status_code}",
        }
    try:
        daten = r.json()
    except ValueError:
        return None, {"ok": False, "ack": _MIN_FEHLER, "error": "keine gültige Antwort"}
    if not isinstance(daten, dict):
        return None, {"ok": False, "ack": _MIN_FEHLER, "error": "unerwartete Antwort"}
    return daten, None


def _treffer(rezept: dict) -> dict:
    minuten = rezept.get("totalMinutes")
    return {
        "id": rezept.get("id"),
        "titel": rezept.get("title"),
        "minuten": minuten,
        "hauptzutaten": list(rezept.get("mainIngredients") or []),
    }


def _treffer_ergebnis(daten: dict, gesamt: int | None) -> dict:
    """Höchstens MAX_TREFFER Treffer; das Kürzen macht der Code, nicht das Modell."""
    alle = [r for r in (daten.get("recipes") or []) if isinstance(r, dict)]
    gesamt = gesamt if gesamt is not None else len(alle)
    liste = [_treffer(r) for r in alle[:MAX_TREFFER]]
    if not liste:
        return {"ok": True, "gefunden": 0, "rezepte": [], "ack": "Dazu habe ich nichts gefunden."}
    return {"ok": True, "gefunden": gesamt, "rezepte": liste}


async def handle_search_recipes(args: dict, transport=None) -> dict:
    q = str(args.get("q") or "").strip()[:200]
    roh = args.get("filter") or []
    if isinstance(roh, str):
        roh = [roh]
    filter_ = [f for f in roh if f in FILTER]
    params: dict = {"limit": MAX_TREFFER}
    if q:
        params["q"] = q
    if filter_:
        params["filter"] = ",".join(filter_)
    daten, fehler = await _kochbuch_get("/api/agent/recipes", params, transport)
    if fehler:
        return fehler
    total = daten.get("total")
    return _treffer_ergebnis(daten, total if isinstance(total, int) else None)


async def handle_similar_recipes(args: dict, transport=None) -> dict:
    recipe_id = str(args.get("recipe_id") or "").strip()
    if not recipe_id or "/" in recipe_id or ".." in recipe_id:
        return {"ok": False, "ack": "Zu welchem Rezept soll ich ähnliche suchen?"}
    daten, fehler = await _kochbuch_get(
        f"/api/agent/recipes/{quote(recipe_id, safe='')}/similar", None, transport
    )
    if fehler:
        return fehler
    ergebnis = _treffer_ergebnis(daten, None)
    if not ergebnis["rezepte"]:
        ergebnis["ack"] = "Dazu habe ich nichts Ähnliches gefunden."
    return ergebnis


def _menge_text(amount, unit, name: str) -> str:
    """'300 Gramm Hähnchen', 'Prise Salz' — wie _menge, aber aus der API-Form."""
    try:
        zahl = float(amount)
        zahl = int(zahl) if zahl == int(zahl) else zahl
    except (TypeError, ValueError):
        zahl = None
    if unit == "stueck":
        einheit = ""
    else:
        einheit = _EINHEIT_LANG.get(unit, unit or "")
    return " ".join(str(t) for t in (zahl, einheit, name) if t not in (None, ""))


async def handle_read_recipe(args: dict, transport=None) -> dict:
    recipe_id = str(args.get("recipe_id") or "").strip()
    if not recipe_id or "/" in recipe_id or ".." in recipe_id:
        return {"ok": False, "ack": "Welches Rezept soll ich vorlesen?"}
    daten, fehler = await _kochbuch_get(
        f"/api/agent/recipes/{quote(recipe_id, safe='')}", None, transport
    )
    if fehler:
        return fehler
    zutaten = []
    for z in daten.get("ingredients") or []:
        # Prisma-Form: {amount, unit, ingredient: {name, ...}}; amount ist ein
        # Decimal und kommt als String ("300", "0.5").
        if not isinstance(z, dict):
            continue
        zutat = z.get("ingredient")
        name = zutat.get("name") if isinstance(zutat, dict) else None
        if not name:
            continue
        zutaten.append(_menge_text(z.get("amount"), z.get("unit"), name))
    minuten = [daten.get("prepTime"), daten.get("cookTime")]
    return {
        "ok": True,
        "titel": daten.get("title"),
        "portionen": daten.get("servings"),
        "minuten": sum(m for m in minuten if isinstance(m, int))
        if all(isinstance(m, int) for m in minuten)
        else None,
        "zutaten": zutaten,
        "schritte": [str(t) for t in (daten.get("instructions") or [])],
    }
