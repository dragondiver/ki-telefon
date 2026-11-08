# Wetter im Gespräch — Design

**Datum:** 2026-08-01
**Status:** abgestimmt, Umsetzung folgt

## Problem

Fragt man das Telefon nach dem Wetter, greift das Modell zu `delegate_task`
und verspricht einen Rückruf. Zwei Fehler auf einmal:

1. Wetter ist eine Live-Frage — sie gehört ins Gespräch beantwortet, wie
   Einkaufsliste, Rezepte und Spotify.
2. Den versprochenen Rückkanal gibt es in Phase 1 nicht. Das Telefon sagt
   etwas zu, das niemand einlöst.

## Lösung

### 1. Neues Modul `src/kitelefon/agents/weather.py`

Aufbau analog zu `spotify.py` und `recipes.py`. Netzwerkzugriff und
Aufbereitung sind getrennt: die Aufbereitung sind reine Funktionen, damit sie
ohne Netz testbar bleiben.

### 2. Ein Tool `get_weather`

| Parameter | Typ | Pflicht | Werte |
|---|---|---|---|
| `ort` | string | ja | z.B. „Hamburg", „Wagenfeld" |
| `wann` | enum | nein | `jetzt` \| `heute` \| `morgen` (Default `heute`) |

**Ein** Tool statt getrennter „jetzt"/„Vorhersage"-Tools: Je mehr Werkzeuge
zur Auswahl stehen, desto häufiger greift ein Modell daneben — und „wie ist
das Wetter" liegt genau zwischen beiden. Mit einem Tool füllt das Modell nur
ein Feld aus, statt eine Auswahl zu treffen. Auch weniger Code: ein Schema,
ein Handler, eine Fehlerbehandlung.

### 3. Datenquelle: Open-Meteo

Kostenlos, **kein API-Key**, keine Registrierung — es kommt kein weiteres
Geheimnis in die `.env`.

1. **Geocoding:** `geocoding-api.open-meteo.com/v1/search` → Koordinaten,
   aufgelöster Name, Land.
2. **Vorhersage:** `api.open-meteo.com/v1/forecast` → `current` bzw. `daily`.

### 4. Rückgabe: strukturierte Werte, keine fertigen Sätze

Der Handler liefert Temperatur, Min/Max, Niederschlag und die Wetterlage als
deutschen Text. Das Formulieren übernimmt das Realtime-Modell — so klingt die
Antwort in Marlenes Stimme anders als beim verrückten Professor.

**Der aufgelöste Ortsname samt Land geht mit zurück.** Bei mehrdeutigen Namen
(„Neustadt") kann die KI sagen, welchen Ort sie genommen hat, statt still den
falschen zu raten.

### 5. Fehlerfälle

Jeder Fehler gibt `{"ok": false, "error": ...}` zurück, damit die KI ihn
aussprechen kann, statt Werte zu erfinden:

- Ort nicht gefunden
- Zeitüberschreitung oder Netzfehler (**2 s Timeout** — am Telefon darf
  nichts hängen)

### 6. `delegate_task` ehrlich machen

Die Tool-Beschreibung weist das Modell an, das Notieren zu bestätigen, aber
**keinen Rückruf und keine E-Mail zu versprechen**, solange Phase 2 fehlt.
Die Queue selbst bleibt unverändert.

## Tests

Ohne Netzzugriff — die Suite darf keine echten API-Aufrufe machen:

- Übersetzung der Open-Meteo-Wettercodes ins Deutsche
- Aufbereitung von Roh-JSON zur Antwort, je für `jetzt`, `heute`, `morgen`
- Argumentprüfung (fehlender Ort, unbekanntes `wann`)
- Fehlerfälle: Ort nicht gefunden, Timeout
- ein Verdrahtungstest mit `httpx.MockTransport`

## Bewusst nicht dabei (YAGNI)

- Mehrtagesvorhersage über morgen hinaus
- Unwetterwarnungen
- Rückfrage bei mehrdeutigen Orten (stattdessen: aufgelösten Namen nennen)
