"""Tests für den Rezept-Agenten, insbesondere das Ändern statt Duplizieren.

Kein Netz: Claude wird über _generate_recipe ersetzt, mise-en-place über
einen httpx.MockTransport.
"""

import json

import httpx
import pytest

from kitelefon.agents import recipes
from kitelefon.agents.recipes import (
    Ingredient,
    Recipe,
    handle_confirm_recipe,
    handle_discard_recipe,
    handle_modify_recipe,
)
from kitelefon.tools import ALL_TOOL_SCHEMAS


def baue_rezept(titel="Gemüse-Couscous", portionen=4, zutaten=("Couscous", "Zucchini")):
    return Recipe(
        title=titel,
        servings=portionen,
        ingredients=[Ingredient(name=n, amount=100, unit="g") for n in zutaten],
        instructions=["Quellen lassen", "Anbraten", "Mischen"],
        tags=["schnell"],
    )


@pytest.fixture(autouse=True)
def sauberer_zustand(monkeypatch):
    """Modul-globale Zustände zwischen Tests zurücksetzen."""
    monkeypatch.setattr(recipes, "_draft", None)
    monkeypatch.setattr(recipes, "_saved", None)
    monkeypatch.setenv("KITELEFON_MISE_EN_PLACE_URL", "https://example.test")
    monkeypatch.setenv("KITELEFON_MISE_EN_PLACE_SECRET", "geheim")
    yield
    recipes._draft = None
    recipes._saved = None


def falscher_generator(monkeypatch, rezept):
    """_generate_recipe ersetzen und die gesehenen Prompts sammeln."""
    prompts = []

    async def fake(query: str) -> Recipe:
        prompts.append(query)
        return rezept

    monkeypatch.setattr(recipes, "_generate_recipe", fake)
    return prompts


def test_modify_recipe_steht_im_werkzeugkasten():
    namen = [t["name"] for t in ALL_TOOL_SCHEMAS]
    assert "modify_recipe" in namen
    schema = next(t for t in ALL_TOOL_SCHEMAS if t["name"] == "modify_recipe")
    assert schema["parameters"]["required"] == ["change"]


@pytest.mark.asyncio
async def test_ohne_rezept_gibt_es_nichts_zu_aendern():
    ergebnis = await handle_modify_recipe({"change": "mit Hähnchen"})
    assert ergebnis["ok"] is False


@pytest.mark.asyncio
async def test_leerer_aenderungswunsch_wird_abgewiesen(monkeypatch):
    monkeypatch.setattr(recipes, "_draft", baue_rezept())
    ergebnis = await handle_modify_recipe({"change": "   "})
    assert ergebnis["ok"] is False


@pytest.mark.asyncio
async def test_entwurf_wird_ersetzt_und_nicht_hochgeladen(monkeypatch):
    monkeypatch.setattr(recipes, "_draft", baue_rezept())
    neu = baue_rezept(titel="Couscous mit Hähnchen", zutaten=("Couscous", "Hähnchen"))
    prompts = falscher_generator(monkeypatch, neu)

    # Jeder HTTP-Aufruf wäre hier ein Fehler — ein Entwurf ist noch nichts,
    # was in Mise-en-Place stehen dürfte.
    def keine_anfragen(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unerwartete HTTP-Anfrage: {request.method} {request.url}")

    ergebnis = await handle_modify_recipe(
        {"change": "mit Hähnchen"}, transport=httpx.MockTransport(keine_anfragen)
    )

    assert ergebnis["ok"] is True
    assert ergebnis["gespeichert"] is False
    assert recipes._draft is neu
    assert "Hähnchen" in prompts[0]
    assert "Gemüse-Couscous" in prompts[0]  # altes Rezept liegt dem Prompt bei


@pytest.mark.asyncio
async def test_confirm_merkt_sich_die_id(monkeypatch):
    monkeypatch.setattr(recipes, "_draft", baue_rezept())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(201, json={"id": "abc-123", "title": "Gemüse-Couscous"})

    ergebnis = await handle_confirm_recipe({}, transport=httpx.MockTransport(handler))

    assert ergebnis["ok"] is True
    assert recipes._saved is not None
    assert recipes._saved[0] == "abc-123"
    assert recipes._draft is None


@pytest.mark.asyncio
async def test_gespeichertes_rezept_wird_per_put_geaendert(monkeypatch):
    alt = baue_rezept()
    monkeypatch.setattr(recipes, "_saved", ("abc-123", alt))
    neu = baue_rezept(titel="Couscous mit Hähnchen", zutaten=("Couscous", "Hähnchen"))
    falscher_generator(monkeypatch, neu)

    gesehen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["method"] = request.method
        gesehen["url"] = str(request.url)
        gesehen["body"] = json.loads(request.content)
        gesehen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "abc-123", "title": neu.title})

    ergebnis = await handle_modify_recipe(
        {"change": "mit Hähnchen"}, transport=httpx.MockTransport(handler)
    )

    assert ergebnis["ok"] is True
    assert ergebnis["gespeichert"] is True
    # Der eigentliche Punkt: PUT auf die bekannte id, kein zweites POST.
    assert gesehen["method"] == "PUT"
    assert gesehen["url"] == "https://example.test/api/agent/recipes/abc-123"
    assert gesehen["auth"] == "Bearer geheim"
    assert gesehen["body"]["title"] == "Couscous mit Hähnchen"
    assert recipes._saved == ("abc-123", neu)


@pytest.mark.asyncio
async def test_verschwundenes_rezept_wird_nicht_heimlich_neu_angelegt(monkeypatch):
    monkeypatch.setattr(recipes, "_saved", ("weg-999", baue_rezept()))
    neu = baue_rezept(titel="Couscous mit Hähnchen")
    falscher_generator(monkeypatch, neu)

    aufrufe = []

    def handler(request: httpx.Request) -> httpx.Response:
        aufrufe.append(request.method)
        return httpx.Response(404, json={"error": "Not found"})

    ergebnis = await handle_modify_recipe(
        {"change": "mit Hähnchen"}, transport=httpx.MockTransport(handler)
    )

    assert ergebnis["ok"] is False
    assert aufrufe == ["PUT"]  # kein stiller POST hinterher
    assert recipes._saved is None
    assert recipes._draft is neu  # Arbeit ist nicht verloren
    assert "neu speichern" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_netzfehler_meldet_sich_ehrlich(monkeypatch):
    monkeypatch.setattr(recipes, "_saved", ("abc-123", baue_rezept()))
    falscher_generator(monkeypatch, baue_rezept(titel="Neu"))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("keine Verbindung")

    ergebnis = await handle_modify_recipe(
        {"change": "mit Hähnchen"}, transport=httpx.MockTransport(handler)
    )
    assert ergebnis["ok"] is False
    assert "antwortet gerade nicht" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_entwurf_hat_vorrang_vor_gespeichertem(monkeypatch):
    """Nach dem Speichern ein neuer Vorschlag: die Änderung trifft den Entwurf."""
    monkeypatch.setattr(recipes, "_saved", ("alt-1", baue_rezept(titel="Altes")))
    monkeypatch.setattr(recipes, "_draft", baue_rezept(titel="Neuer Entwurf"))
    prompts = falscher_generator(monkeypatch, baue_rezept(titel="Neuer Entwurf, geändert"))

    def keine_anfragen(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Entwurf darf nichts hochladen")

    ergebnis = await handle_modify_recipe(
        {"change": "schärfer"}, transport=httpx.MockTransport(keine_anfragen)
    )

    assert ergebnis["gespeichert"] is False
    assert "Neuer Entwurf" in prompts[0]
    assert "Altes" not in prompts[0]


@pytest.mark.asyncio
async def test_discard_vergisst_auch_das_gespeicherte(monkeypatch):
    monkeypatch.setattr(recipes, "_saved", ("abc-123", baue_rezept()))
    await handle_discard_recipe({})
    assert recipes._saved is None


# --- Das Rezept muss beim Modell ankommen ------------------------------


def _rezept(**kw):
    from kitelefon.agents.recipes import Ingredient, Recipe

    vorgabe = dict(
        title="Panisse aus der Provence",
        servings=4,
        ingredients=[
            Ingredient(name="Kichererbsenmehl", amount=250, unit="g"),
            Ingredient(name="Wasser", amount=750, unit="ml"),
            Ingredient(name="Eier", amount=2, unit="stueck"),
        ],
        instructions=["Wasser aufkochen.", "Mehl einrühren.", "Goldbraun braten."],
    )
    vorgabe.update(kw)
    return Recipe(**vorgabe)


@pytest.mark.asyncio
async def test_vorschlag_liefert_mengen_und_schritte(monkeypatch):
    """Bis 2026-08-08 bekam die KI nur die Zusammenfassung. Auf "lies die
    Schritte vor" blieb ihr deshalb nur, sie zu erfinden — Jean-Luc
    spuelte Kichererbsen, die im Rezept gar nicht vorkamen."""

    async def erzeuge(query):
        return _rezept()

    monkeypatch.setattr(recipes, "_generate_recipe", erzeuge)

    ergebnis = await recipes.handle_suggest_recipe({"query": "Panisse"})

    assert ergebnis["schritte"] == ["Wasser aufkochen.", "Mehl einrühren.", "Goldbraun braten."]
    assert "250 Gramm Kichererbsenmehl" in ergebnis["zutaten"]
    assert "750 Milliliter Wasser" in ergebnis["zutaten"]
    assert ergebnis["portionen"] == 4


@pytest.mark.asyncio
async def test_mengen_sind_vorlesbar_nicht_maschinenlesbar(monkeypatch):
    """'200.0 g' liest sich am Telefon schlecht; Stueck braucht gar keine
    Einheit."""

    async def erzeuge(query):
        return _rezept()

    monkeypatch.setattr(recipes, "_generate_recipe", erzeuge)

    zutaten = (await recipes.handle_suggest_recipe({"query": "x"}))["zutaten"]

    assert "2 Eier" in zutaten  # kein "2 Stück Eier"
    assert not any(".0" in z for z in zutaten)


@pytest.mark.asyncio
async def test_auch_nach_dem_aendern_liegen_die_details_bei(monkeypatch):
    """Gerade nach einer Aenderung fragt man nach — dann muessen die
    neuen Mengen mitkommen, nicht die alten."""

    async def erzeuge(query):
        return _rezept()

    async def aendere(recipe, change):
        return _rezept(title="Panisse ohne Ei", instructions=["Nur Mehl und Wasser verrühren."])

    monkeypatch.setattr(recipes, "_generate_recipe", erzeuge)
    await recipes.handle_suggest_recipe({"query": "Panisse"})

    async def erzeuge2(query):
        return _rezept(title="Panisse ohne Ei", instructions=["Nur Mehl und Wasser verrühren."])

    monkeypatch.setattr(recipes, "_generate_recipe", erzeuge2)

    ergebnis = await recipes.handle_modify_recipe({"change": "ohne Ei"})

    assert ergebnis["gespeichert"] is False
    assert ergebnis["schritte"] == ["Nur Mehl und Wasser verrühren."]


async def test_abgelehntes_rezept_legt_das_gespraech_nicht_lahm(monkeypatch):
    """Opus 5.5 kann ablehnen, dann ist parsed_output leer (Modellwechsel 14.11.2026)."""
    from types import SimpleNamespace

    from kitelefon.agents import recipes

    class _Abgelehnt:
        async def parse(self, **kwargs):
            return SimpleNamespace(stop_reason="refusal", parsed_output=None)

    monkeypatch.setattr(
        recipes, "_get_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=_Abgelehnt()))
    )
    ergebnis = await recipes.handle_suggest_recipe({"query": "Kichererbsen mit Spinat"})
    assert ergebnis["ok"] is False


# --- Einheit Handvoll (#4) ------------------------------------------------


def test_handvoll_wird_als_einheit_angenommen():
    from kitelefon.agents.recipes import Ingredient

    assert Ingredient(name="Parmesan", amount=2, unit="handvoll").unit == "handvoll"


@pytest.mark.asyncio
async def test_handvoll_wird_vorgelesen(monkeypatch):
    """Handvoll hat keinen Plural: 'zwei Handvoll Parmesan'."""
    from kitelefon.agents.recipes import Ingredient

    async def erzeuge(query):
        return _rezept(ingredients=[Ingredient(name="Parmesan", amount=2, unit="handvoll")])

    monkeypatch.setattr(recipes, "_generate_recipe", erzeuge)

    zutaten = (await recipes.handle_suggest_recipe({"query": "x"}))["zutaten"]

    assert zutaten == ["2 Handvoll Parmesan"]


def test_einheiten_stimmen_ueberall_ueberein():
    """Die Einheitenliste steht an vier Stellen: Literal, Prompt,
    Sprechformen und das Werkzeug fuer die Einkaufsliste. Fehlt eine
    Einheit an einer Stelle, kann das Telefon sie nicht benutzen (#4)."""
    import typing

    from kitelefon.tools import ADD_SHOPPING_ITEM_TOOL_SCHEMA

    einheiten = set(typing.get_args(recipes.UNITS))
    werkzeug = ADD_SHOPPING_ITEM_TOOL_SCHEMA["parameters"]["properties"]["unit"]["enum"]

    assert "handvoll" in einheiten
    assert set(werkzeug) == einheiten
    assert set(recipes._EINHEIT_LANG) == einheiten
    for einheit in einheiten:
        assert einheit in recipes.SYSTEM_PROMPT
