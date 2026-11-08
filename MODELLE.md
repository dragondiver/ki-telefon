# Modelle

Welche KI-Modelle dieses Repo nutzt, wofür, und was auf dem Gerät wirklich läuft.
Die monatliche Routine „Monatscheck KI-Modelle“ liest diese Datei zuerst und gleicht sie
gegen den Code ab. Wer ein Modell wechselt, ändert diese Datei im selben PR; wer einen
Gerätewert in 1Password ändert, trägt ihn hier mit Datum nach.

Spalten: **Code** = Wert oder Default im Code, **Gerät** = was tatsächlich läuft
(1Password/Unit, mit Prüfdatum), **Denken** = thinking bzw. reasoning.effort.

Gerät: kitelefon, `~/telefon-ai/.env` (geprüft 01.11.2026). `KITELEFON_SPRACHKURS_MODEL` ist dort nicht gesetzt, es gilt der Code-Default.

| Stelle | Zweck | Anbieter | Code | Gerät | Denken |
|---|---|---|---|---|---|
| `config.py` `openai_model` (`KITELEFON_OPENAI_MODEL`), `realtime.py` | Live-Gespräch am Telefon, alle Personas | OpenAI Realtime | `gpt-realtime-2.1-mini` | `gpt-realtime-2.1-mini` | – |
| `config.py` `sprachkurs_model` (`KITELEFON_SPRACHKURS_MODEL`) | Camille, Französisch-Sprachkurs am Telefon | OpenAI Realtime | `gpt-realtime-2.1-mini` | Default | – |
| `realtime.py` `TRANSKRIPTIONS_MODELL` | Mitschrift der Gespräche für Protokoll und Auswertung | OpenAI | `gpt-transcribe` | wie Code | – |
| `agents/websuche.py` | Schnelle Websuche mitten im Gespräch (Telefon und Elektra), Antwort in Sekunden | Anthropic | `claude-haiku-5-5` | wie Code | aus |
| `agents/themenurteil.py` | Urteil, ob ein neuer Themenname zu einem vorhandenen Haushalts-Thema gehört | Anthropic | `claude-haiku-5-5` | wie Code | aus |
| `agents/recipes.py` | Rezeptvorschlag im Gespräch, strukturiert, geht an mise-en-place | Anthropic | `claude-opus-4-7` | wie Code | nicht gesetzt |
| `agents/erinnerung.py` | Nach dem Auflegen: was man sich vom Anrufer merkt | Anthropic | `claude-opus-4-7` | wie Code | nicht gesetzt |
| `scripts/ansagen_erzeugen.py` | Einmal-Job: Störungsansagen als Audio erzeugen (kein Laufzeitcode) | OpenAI TTS | `gpt-4o-mini-tts` | Abschaltung 06.01.2027; vor einem erneuten Lauf auf ein anderes Modell umstellen | – |
| `scripts/werkzeugaufruf_probe.py` | Handmessung Werkzeugaufrufe (kein Laufzeitcode) | OpenAI Realtime | `gpt-realtime-2.1-mini` | – | – |
