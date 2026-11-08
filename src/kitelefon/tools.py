import asyncio
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from kitelefon.agents.weather import GET_WEATHER_TOOL_SCHEMA
from kitelefon.personen import EMPFAENGER, empfaenger_auswahl
from kitelefon.tasks import TaskSink

DELEGATE_TASK_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "delegate_task",
    "description": (
        "Delegiere eine Aufgabe an einen asynchronen Agenten. Verwende dies "
        "NUR für echte Hintergrundarbeit: Termin, Buchung, Erinnerung für "
        "später, eine Recherche, die dauert. "
        "NICHT hierher gehören: Wetter (get_weather), Musik (spotify_play), "
        "Einkaufsliste (add_shopping_item), Rezepte (suggest_recipe) und das "
        "Haushaltsgedächtnis: Merken, Nachschlagen, Aufräumen, Zusammenführen "
        "und Löschen von Notizen erledigst du selbst mit merke, erinnere und "
        "vergiss, auch wenn jemand ‚kümmer dich drum‘ oder ‚übernimm das‘ sagt. "
        "Ein Rezeptwunsch ist KEINE Recherche — er wird im Gespräch erledigt. "
        "WICHTIG: Sag NIEMALS 'notiert' oder 'kümmere mich drum', bevor "
        "dieses Werkzeug geantwortet hat. Ohne den Aufruf gibt es die "
        "Aufgabe nicht, egal was du sagst. "
        "deliver_via: 'callback' (das Telefon klingelt zurück und liest das "
        "Ergebnis vor), 'elektra' (Kuvert auf dem Sprachgerät im Wohnzimmer, "
        "ein Tipp liest vor) oder 'email'. Frag nach, wenn es nicht klar ist. "
        "Bei 'email' gehört in deliver_to "
        + empfaenger_auswahl(EMPFAENGER, "eine Mailadresse")
        + "."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_type": {
                "type": "string",
                "enum": ["research", "reminder", "booking", "other"],
                # Kein Pflichtfeld mehr: am 14.08. rief der Hamburger ohne
                # agent_type auf, der Aufruf scheiterte an der Validierung,
                # und er brauchte einen zweiten Anlauf. Fast alles hier ist
                # eine Recherche — ein Feld, dessen Wert man raten muss,
                # gehoert nicht in die Pflichtliste.
                "default": "research",
            },
            "description": {"type": "string"},
            "deliver_via": {"type": "string", "enum": ["email", "callback", "elektra"]},
            "deliver_to": {"type": "string"},
        },
        "required": ["description", "deliver_via"],
    },
}

ADD_SHOPPING_ITEM_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "add_shopping_item",
    "description": (
        "Füge einen Artikel zur Einkaufsliste der Familie hinzu. "
        "Verwende dies, wenn der Nutzer sagt er braucht etwas oder "
        "etwas einkaufen möchte. Bestätige kurz was du notiert hast."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name des Artikels, z.B. 'Brötchen', 'Milch', 'Äpfel'",
            },
            "amount": {
                "type": "number",
                "description": "Menge, z.B. 6",
                "default": 1,
            },
            "unit": {
                "type": "string",
                "enum": [
                    "stueck",
                    "g",
                    "kg",
                    "ml",
                    "l",
                    "el",
                    "tl",
                    "prise",
                    "bund",
                    "dose",
                    "packung",
                ],
                "description": "Einheit. Bei Stückzahlen 'stueck' verwenden.",
                "default": "stueck",
            },
        },
        "required": ["name"],
    },
}

SPOTIFY_PLAY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "spotify_play",
    "description": (
        "Startet Musik per Spotify Connect auf dem Evoke Play (Vorgabe) "
        "oder auf der Anlage. Drei Arten: "
        "kind='track' für ein bestimmtes Stück ('spiel Bohemian Rhapsody'), "
        "kind='artist' für einen Künstler ('spiel was von Queen') — dann "
        "laufen dessen Top-Tracks, kind='playlist' für eine der eigenen "
        "Playlists des Haushalts. WICHTIG: Playlists sind nur die selbst "
        "angelegten Listen der beiden Konten. Ein Musikwunsch, der keine "
        "davon meint, ist ein 'track' oder ein 'artist', niemals eine "
        "Playlist. Ist der Wunsch vage ('irgendwas Indisches', 'was "
        "Fröhliches'), wähle SELBST einen konkreten Titel oder Künstler, "
        "nenne ihn im Gespräch und rufe damit auf — frage nicht zurück. "
        "Wenn aus dem Gespräch klar ist, wer anruft (Begrüßung oder "
        "Selbstnennung), Vornamen in 'wer' mitgeben für die Kontowahl. "
        "Sonst 'wer' weglassen. Der Lautsprecher ('auf dem Evoke', 'auf der "
        "Anlage') ist NIE Teil des Playlist-, Titel- oder Künstlernamens — "
        "er gehört ausschließlich ins Feld 'geraet'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "playlist": {
                "type": "string",
                "description": (
                    "Wonach gesucht wird: bei kind='track' der Titel (gern "
                    "mit Künstler, z.B. 'Kesariya Arijit Singh'), bei "
                    "kind='artist' der Künstlername, bei kind='playlist' "
                    "Name oder Namensteil der Playlist. Ohne Gerätenamen: "
                    "aus 'spiel die Abendliste auf dem Evoke' wird 'Abendliste'."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["playlist", "artist", "track"],
                "description": "Art der Wiedergabe. Default 'playlist'.",
            },
            "wer": {
                "type": "string",
                "description": (
                    "Vorname des Anrufers, falls aus dem Gespräch bekannt. Sonst weglassen."
                ),
            },
            "geraet": {
                "type": "string",
                "enum": ["evoke", "anlage"],
                "description": (
                    "Lautsprecher. NUR 'anlage', wenn der Anrufer die Anlage "
                    "ausdrücklich nennt ('auf der Anlage', '… auf Spotify, "
                    "Anlage'). Sonst weglassen — dann spielt der Evoke Play."
                ),
            },
        },
        "required": ["playlist"],
    },
}

SPOTIFY_NOW_PLAYING_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "spotify_now_playing",
    "description": (
        "Liefert Künstler und Titel des aktuell auf Spotify spielenden "
        "Stücks. Verwenden, wenn der Anrufer fragt 'wer singt das', "
        "'welcher Song läuft', 'wie heißt der Künstler' o.ä."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "wer": {
                "type": "string",
                "description": (
                    "Vorname des Anrufers, falls bekannt — sonst weglassen, "
                    "dann werden beide Konten abgefragt."
                ),
            },
        },
        "required": [],
    },
}

SUGGEST_RECIPE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "suggest_recipe",
    "description": (
        "Schlägt ein Rezept vor. PFLICHT bei jedem Rezeptwunsch — auch wenn "
        "du als Koch auftrittst, hast du kein eigenes Rezeptwissen und darfst "
        "Zutaten oder Mengen nicht selbst erfinden. Übergibt die Anfrage des "
        "Anrufers (Gericht, Personenzahl, Vorlieben/Allergien, Besonderheiten) "
        "als 'query' an einen Rezept-Generator. Antwort ist eine vorlesbare "
        "Zusammenfassung. Wird der Vorschlag verfeinert ('mit Knoblauch', "
        "'weniger Sahne'), erneut suggest_recipe aufrufen mit angereichertem "
        "Query — der alte Entwurf wird überschrieben."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Was vorgeschlagen werden soll, in natürlicher Sprache. "
                    "Beispiele: 'Pasta mit Spinat, vegetarisch, 4 Personen', "
                    "'einfacher Schokokuchen ohne Nüsse'."
                ),
            },
        },
        "required": ["query"],
    },
}

CONFIRM_RECIPE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "confirm_recipe",
    "description": (
        "Speichert den aktuell vorgeschlagenen Rezept-Entwurf in der "
        "Familien-App Mise-en-Place. Aufrufen wenn der Anrufer das Rezept "
        "behalten möchte ('ja, speicher das', 'klingt gut')."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

MODIFY_RECIPE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "modify_recipe",
    "description": (
        "Ändert das zuletzt besprochene Rezept. Aufrufen, wenn der Anrufer "
        "eine Änderung wünscht ('mach das nochmal mit Hähnchen', 'für sechs "
        "Personen', 'ohne Sahne'). Wurde das Rezept schon mit confirm_recipe "
        "gespeichert, wird es in Mise-en-Place geändert — NICHT erneut "
        "suggest_recipe + confirm_recipe aufrufen, das legt sonst ein "
        "zweites, fast gleiches Rezept an."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "change": {
                "type": "string",
                "description": (
                    "Der Änderungswunsch in natürlicher Sprache. Beispiele: "
                    "'mit Hähnchenbrust statt Tofu', 'für 6 Personen', "
                    "'ohne Knoblauch'."
                ),
            },
        },
        "required": ["change"],
    },
}

DISCARD_RECIPE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "discard_recipe",
    "description": (
        "Verwirft den aktuellen Rezept-Entwurf. Aufrufen wenn der Anrufer "
        "ablehnt ('nee, lass mal', 'gefällt mir nicht')."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

ZUSTELLUNG_AENDERN_TOOL_SCHEMA = {
    "type": "function",
    "name": "zustellung_aendern",
    "description": (
        "Aendert bei einer schon angelegten Aufgabe, wie das Ergebnis kommt. "
        "Nutze das, wenn der Anrufer es sich anders ueberlegt ('schick's mir "
        "doch per Mail', 'ruf mich lieber an', 'lieber an Elektra') — auch wenn du die Aufgabe "
        "gerade eben erst angelegt hast. Die task_id hast du aus der Antwort "
        "von delegate_task. Am 14.08. sagte Astrid eine Mail zu und konnte "
        "sie nicht einloesen: es gab dieses Werkzeug noch nicht, und beide "
        "Aufgaben haetten stattdessen angerufen."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "deliver_via": {"type": "string", "enum": ["email", "callback", "elektra"]},
            "deliver_to": {
                "type": "string",
                "description": (
                    "Nur bei 'email': " + empfaenger_auswahl(EMPFAENGER, "eine Mailadresse") + ". "
                    "Leer heisst: an den Standardempfaenger."
                ),
            },
        },
        "required": ["task_id", "deliver_via"],
    },
}

ERGEBNIS_PER_MAIL_TOOL_SCHEMA = {
    "type": "function",
    "name": "ergebnis_per_mail",
    "description": (
        "Schickt das gerade vorgelesene Ergebnis zusaetzlich per E-Mail. "
        "Nutze das, wenn der Anrufer waehrend eines Rueckrufs darum bittet "
        "('schick mir das per Mail', 'kann ich das schriftlich haben'). "
        "Nur bei einem Rueckruf moeglich — es braucht die Aufgabe, um die "
        "es geht. Frag nach, an wen es gehen soll, wenn es nicht klar ist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "an": {
                "type": "string",
                "description": (
                    "Empfaenger: " + empfaenger_auswahl(EMPFAENGER, "eine Mailadresse") + ". "
                    "Leer heisst: an den Standardempfaenger."
                ),
            },
        },
        "required": [],
    },
}

# --- Freisprechen ueber Elektra (#38, Bauabschnitt 3) -----------------------
# Beide ohne Parameter: das Modell entscheidet nur, DASS umgeschaltet wird.
# Ob Freisprechen eingerichtet ist, weiss erst der CallManager (Settings) —
# ohne Einrichtung antwortet der Handler mit einem klaren Fehlersatz.
FREISPRECHEN_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "freisprechen",
    "description": (
        "Legt das laufende Gespräch auf Elektra, das Lautsprecher-Gerät im "
        "Wohnzimmer: Ausgabe UND Mikrofon wandern dorthin, der Anrufer legt "
        "den Hörer neben das Telefon und redet freihändig weiter. Rufe das "
        "SOFORT und ohne Rückfrage auf, wenn der Anrufer sagt 'schalte auf "
        "laut', 'Freisprecheinrichtung ein', 'mach mal laut', 'auf "
        "Lautsprecher' oder Ähnliches. Das Gespräch selbst läuft unverändert "
        "weiter. Meldet das Werkzeug einen Fehler, sag genau diesen Satz."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

HOERER_ZURUECK_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "hoerer_zurueck",
    "description": (
        "Holt das Gespräch von Elektra zurück in den Telefonhörer. Rufe das "
        "auf bei 'mach wieder leise', 'zurück zum Hörer', 'Freisprechen aus' "
        "oder Ähnlichem. Läuft das Gespräch schon über den Hörer, passiert "
        "nichts."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

WEB_SUCHE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "web_suche",
    "description": (
        "Sucht aktuelle Informationen im Netz und liefert eine kurze, "
        "vorlesbare Antwort: Spielstände, Nachrichten, Öffnungszeiten, "
        "Preise, alles, was sich heute geändert haben kann. "
        "NICHT hierher gehören Wetter (get_weather) und Wissen, das du "
        "sicher hast. Dauert eine Recherche länger oder soll sie später "
        "kommen, nimm delegate_task. "
        "Sag VOR dem Aufruf kurz 'Moment', in deinem eigenen Ton: ein Wort "
        "oder ein ganz kurzer Satz, danach rufst du sofort auf. Lies die "
        "Frage nicht vor, keine längere Überleitung — sonst bleibt die "
        "Leitung während der Suche stumm. "
        "Lies die Antwort in eigenen Worten vor und nenne die Quelle beim Namen, "
        "nie als Adresse. Meldet das Werkzeug einen Fehler, sag das ehrlich."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "frage": {
                "type": "string",
                "description": "Die Frage vollständig, mit Namen und Zeitbezug "
                "(z. B. 'Wie ging das Länderspiel Deutschland gegen Griechenland aus?').",
            }
        },
        "required": ["frage"],
    },
}

MERKE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "merke",
    "description": (
        "Hält etwas im gemeinsamen Gedächtnis des Haushalts fest, unter einem "
        "Thema (z. B. 'Urlaub Italien'). Themen bleiben wochenlang erhalten. "
        "Nur aufrufen, wenn jemand ausdrücklich darum bittet ('merk dir …', "
        "'schreib dir auf …') oder ein Vorhaben nennt und sagt, es solle "
        "festgehalten werden. Nie von dir aus. Ruf es SOFORT auf, ohne vorher "
        "zu bestätigen oder nachzufragen, sobald Thema und Inhalt aus dem "
        "Satz hervorgehen; den Themennamen bildest du selbst, kurz, z. B. "
        "'Italienurlaub' wird 'Urlaub Italien'. Gibt es das Thema schon "
        "(siehe Liste im Prompt), nimm genau diesen Namen. Antwortet das "
        "Werkzeug mit aehnliche_themen, wurde nichts gespeichert: ist es "
        "dasselbe Thema, ruf merke erneut mit genau diesem Namen auf; unklar, "
        "frag den Anrufer; wirklich neu, ruf merke mit neu=true auf. "
        "Bestätige erst nach der Antwort kurz, z. B. 'Ist unter Urlaub "
        "Italien notiert.' Steht zugeordnet_zu in der Antwort, nenne diesen "
        "vorhandenen Namen."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thema": {"type": "string", "description": "Kurzer Themenname."},
            "notiz": {
                "type": "string",
                "description": "Ein Satz, höchstens 300 Zeichen.",
            },
            "neu": {
                "type": "boolean",
                "description": "true nur, um ein wirklich neues Thema trotz "
                "ähnlicher Themen anzulegen.",
            },
        },
        "required": ["thema", "notiz"],
    },
}

ERINNERE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "erinnere",
    "description": (
        "Holt Notizen aus dem gemeinsamen Gedächtnis des Haushalts. Mit "
        "'thema' die Notizen dieses Themas, ohne Argument die Liste aller "
        "Themen. Rufe es auf, sobald eine Frage zu einem Thema der Liste im "
        "Prompt passt ('Was haben wir für Italien schon?'). Passt der Name "
        "nicht genau, nimm den ähnlichsten aus der Antwort."
    ),
    "parameters": {
        "type": "object",
        "properties": {"thema": {"type": "string"}},
        "required": [],
    },
}

VERGISS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "vergiss",
    "description": (
        "Löscht eine Notiz oder ein ganzes Thema aus dem Gedächtnis des "
        "Haushalts. Frag vorher nach, was genau gelöscht werden soll, und "
        "ruf es erst nach einem klaren Ja auf. Ohne 'notiz' verschwindet das "
        "ganze Thema, mit 'notiz' nur die Notiz, die zu diesen Worten passt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thema": {"type": "string"},
            "notiz": {
                "type": "string",
                "description": "Ein Stichwort oder Satz aus der Notiz; weglassen, "
                "um das ganze Thema zu löschen.",
            },
        },
        "required": ["thema"],
    },
}

ALL_TOOL_SCHEMAS = [
    ERGEBNIS_PER_MAIL_TOOL_SCHEMA,
    ZUSTELLUNG_AENDERN_TOOL_SCHEMA,
    GET_WEATHER_TOOL_SCHEMA,
    DELEGATE_TASK_TOOL_SCHEMA,
    ADD_SHOPPING_ITEM_TOOL_SCHEMA,
    SPOTIFY_PLAY_TOOL_SCHEMA,
    SPOTIFY_NOW_PLAYING_TOOL_SCHEMA,
    SUGGEST_RECIPE_TOOL_SCHEMA,
    CONFIRM_RECIPE_TOOL_SCHEMA,
    MODIFY_RECIPE_TOOL_SCHEMA,
    DISCARD_RECIPE_TOOL_SCHEMA,
    WEB_SUCHE_TOOL_SCHEMA,
    MERKE_TOOL_SCHEMA,
    ERINNERE_TOOL_SCHEMA,
    VERGISS_TOOL_SCHEMA,
    FREISPRECHEN_TOOL_SCHEMA,
    HOERER_ZURUECK_TOOL_SCHEMA,
]


class DelegateTaskArgs(BaseModel):
    # Standardwert statt Pflichtfeld — siehe Kommentar im Schema oben.
    agent_type: Literal["research", "reminder", "booking", "other"] = "research"
    description: str
    deliver_via: Literal["email", "callback", "elektra"]
    deliver_to: str | None = None


class ZustellungAendernArgs(BaseModel):
    task_id: int
    deliver_via: Literal["email", "callback", "elektra"]
    deliver_to: str | None = None


class AddShoppingItemArgs(BaseModel):
    name: str
    amount: float = 1
    unit: str = "stueck"


def handle_delegate_task(raw_args: dict[str, Any], queue: TaskSink) -> dict[str, Any]:
    try:
        args = DelegateTaskArgs.model_validate(raw_args)
    except ValidationError as e:
        return {"ok": False, "error": str(e)}

    task_id = queue.enqueue(
        agent_type=args.agent_type,
        description=args.description,
        deliver_via=args.deliver_via,
        deliver_to=args.deliver_to,
        payload=raw_args,
    )
    return {
        "ok": True,
        "task_id": task_id,
        "ack": f"Notiert. Du bekommst das Ergebnis {_wohin(args.deliver_via)}.",
    }


def _wohin(weg: str) -> str:
    return {
        "email": "per Mail",
        "callback": "als Rueckruf aufs Telefon",
        "elektra": "zu Elektra",
    }.get(weg, f"per {weg}")


def handle_zustellung_aendern(raw_args: dict[str, Any], queue: TaskSink) -> dict[str, Any]:
    """Den Zustellweg einer bestehenden Aufgabe umstellen.

    Wir verschicken hier nichts. Die Aufgabe wird wieder auf unzugestellt
    gesetzt, den Rest macht der Zusteller des Heim-Agenten innerhalb einer
    Minute — Mailversand gibt es genau einmal im Haus, nicht zweimal in zwei
    Repos.
    """
    try:
        args = ZustellungAendernArgs.model_validate(raw_args)
    except ValidationError as e:
        return {"ok": False, "error": str(e)}

    ok = queue.zustellung_aendern(args.task_id, args.deliver_via, args.deliver_to or "")
    if not ok:
        return {
            "ok": False,
            "error": f"Aufgabe {args.task_id} nicht gefunden oder nicht aenderbar.",
        }
    return {"ok": True, "ack": f"Aufgabe {args.task_id} kommt jetzt {_wohin(args.deliver_via)}."}


async def handle_add_shopping_item(
    raw_args: dict[str, Any],
    base_url: str,
    secret: str,
) -> dict[str, Any]:
    if not secret or not base_url:
        return {"ok": False, "error": "mise-en-place not configured"}
    try:
        args = AddShoppingItemArgs.model_validate(raw_args)
    except ValidationError as e:
        return {"ok": False, "error": str(e)}

    url = f"{base_url.rstrip('/')}/api/agent/shopping/items"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"name": args.name, "amount": args.amount, "unit": args.unit},
                headers={"Authorization": f"Bearer {secret}"},
            )
        if resp.status_code == 201:
            data = resp.json()
            return {
                "ok": True,
                "ack": f"{args.name} ({args.amount} {args.unit}) auf die Einkaufsliste '{data.get('listName', 'Einkaufsliste')}' gesetzt.",
            }
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# --- Sprachkurs (Rolle 3). Bewusst NICHT in ALL_TOOL_SCHEMAS: nur
# Camille bekommt diese Werkzeuge (Spec 7.4). ---
SPRACHKURS_TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "note_error",
        "description": (
            "Notiere einen Sprachfehler der Anruferin still fuer die "
            "spaetere Fehlerarbeit. Erzeugt KEINE hoerbare Reaktion — "
            "das Gespraech laeuft einfach weiter."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "learner_utterance": {
                    "type": "string",
                    "description": "der fehlerhafte Satz, woertlich",
                },
                "correction": {"type": "string", "description": "die richtige Fassung"},
                "category": {
                    "type": "string",
                    "description": "z.B. artikel, nasale, passé composé",
                },
            },
            "required": ["learner_utterance", "correction"],
        },
    },
    {
        "type": "function",
        "name": "get_news_fr",
        "description": (
            "Drei aktuelle Schlagzeilen aus dem 'Journal en français "
            "facile' — als Stoff fuer die Gespraechsaufgabe."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]


def make_sprachkurs_tool_handler(call, fallback):
    """Handler fuer Rolle 3: die zwei Kurs-Werkzeuge selbst, Rest per
    fallback (get_weather etc.)."""
    from kitelefon.agents.news_fr import get_news_fr

    def handler(name: str, args: dict):
        if name == "note_error":
            # call.note_error() greift synchron auf Postgres zu — direkt
            # aufgerufen wuerde das den Event-Loop blockieren, waehrend die
            # DB antwortet. asyncio.to_thread() gibt eine Awaitable zurueck,
            # die der Realtime-Dispatcher ohnehin schon erwarten kann.
            return asyncio.to_thread(
                call.note_error,
                args.get("learner_utterance", ""),
                args.get("correction", ""),
                args.get("category"),
            )
        if name == "get_news_fr":
            return get_news_fr()  # Awaitable — realtime.py kann beides
        if fallback is None:
            return {"fehler": f"unbekanntes Werkzeug: {name}"}
        return fallback(name, args)

    return handler
