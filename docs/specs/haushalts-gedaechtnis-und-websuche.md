# Haushalts-Gedächtnis und Websuche

Stand: 04.10.2026 · Status: Entwurf, von Florian im Gespräch freigegeben · Betrifft: telefon-ai (Umsetzung), rundfunk/Elektra (nur Prompt)

## Ziel

Telefon und Elektra bekommen zwei Fähigkeiten, die beiden fehlen:

1. **Aktuelle Internetsuche** im laufenden Gespräch („Wie ging das Spiel gestern aus?"), in wenigen Sekunden und sprechbar.
2. **Ein gemeinsames Gedächtnis für den Haushalt**, nach Themen getrennt, damit eine Urlaubsplanung wochenlang liegen bleiben kann.

Erfolg heißt: Florian sagt am Telefon „Merk dir für den Italienurlaub: Hotel in Rimini ist gebucht", und drei Wochen später fragt jemand Elektra „Was haben wir für Italien schon?" und bekommt das Hotel genannt.

## Entscheidungen (Florian, 04.10.2026)

- **Ein Gedächtnis für den ganzen Haushalt**, nicht pro Person und nicht pro Figur. Telefon und Elektra lesen und schreiben dieselben Themen.
- **Themen statt „letztes Gespräch"**: Jedes Thema lebt so lange, wie es benutzt wird. Verfall nach 90 Tagen ohne Zugriff, nicht nach 14.
- **Die Websuche gilt für beide Geräte.**
- Das Modell legt Themen nur an oder ergänzt sie, wenn jemand es sagt („merk dir …", „wir planen …"). Es speichert nichts von selbst.

## Nicht Teil dieses Auftrags

- Das bestehende Rollen-Gedächtnis (`memory.py`, `role_memory`) bleibt unverändert. Es ist für die persönlichen Figuren des Telefons.
- Kein Personenbezug, keine Stimmerkennung, keine Benutzerverwaltung.
- Keine automatische Zusammenfassung von Gesprächen in Themen.
- Keine lange Recherche: `delegate_task` (Typ `research`) bleibt der Weg für alles, was länger als ein paar Sekunden dauert.

## Aufbau

Alles liegt in **telefon-ai** und wird über den vorhandenen Werkzeugdienst (`werkzeugdienst.py`, rundfunk#31) an Elektra weitergereicht. Was in `ALL_TOOL_SCHEMAS` steht und nicht in `NUR_AM_TELEFON`, sieht Elektra beim nächsten Start der Bridge von selbst. In rundfunk entsteht keine zweite Implementierung.

```
Telefon  ──┐
           ├──► tools.py: web_suche, merke, erinnere, vergiss ──► agents/websuche.py (Claude Haiku + Web-Search)
Elektra  ──┘      (Elektra über GET/POST /werkzeuge)             └► haushalt.py (SQLite, Tabelle haushalt_themen)
```

### 1. `web_suche`

- Argument: `frage` (Text, Pflicht).
- Umsetzung in `agents/websuche.py`, nach dem Muster von `agents/recipes.py` (ein `AsyncAnthropic`-Client, `messages.create`).
- Modell Claude Haiku 4.5 mit dem Server-Werkzeug `web_search_20250305`, `max_uses` 3. Dasselbe Verfahren nutzt heim-agent in `processor.py`.
- System-Prompt: kurze, gesprochene Antwort auf Deutsch (höchstens drei Sätze), Quellen nur als Name („laut kicker.de"), keine URLs vorlesen, Datum nennen, wenn die Quelle alt ist. Findet die Suche nichts Belastbares, sagt es das.
- Rückgabe: `{"ok": true, "antwort": "…", "quellen": ["kicker.de", …]}`.
- **Zeitlimit:** Der Werkzeugdienst bricht nach 30 s ab. Die Suche bekommt 22 s und liefert danach `{"ok": false, "error": "Die Suche hat zu lange gedauert."}`, damit das Modell es sagen kann.
- **Kosten:** etwa 1 Cent je Suche zuzüglich Haiku-Tokens. Obergrenze `websuche_max_pro_tag` (Standard 50); darüber meldet das Werkzeug, dass das Tageslimit erreicht ist. Kein Abrechnungssystem, nur ein Zähler im Speicher.
- Der Anthropic-Schlüssel ist in telefon-ai bereits konfiguriert (Rezepte, Erinnerung). Es entsteht kein neues Geheimnis.

### 2. Themen-Gedächtnis

**Speicher:** neue Tabelle in der vorhandenen `tasks.db`, unabhängig vom Schalter `role_memory`.

```sql
CREATE TABLE IF NOT EXISTS haushalt_themen (
    thema        TEXT PRIMARY KEY,   -- normalisiert: klein, getrimmt, Mehrfach-Leerzeichen zu einem
    anzeige      TEXT NOT NULL,      -- so geschrieben, wie es zuerst genannt wurde
    notizen_json TEXT NOT NULL DEFAULT '[]',   -- Liste von {text, am}
    angelegt_am  TEXT NOT NULL,
    zuletzt_am   TEXT NOT NULL       -- letzte Nutzung (Lesen oder Schreiben)
);
```

Modul `haushalt.py` mit Klasse `Haushalt` (gleiche Bauart wie `RoleMemory`: `__init__(db_path)`, Verbindung je Aufruf, `now` als Parameter für Tests).

**Werkzeuge:**

| Name | Argumente | Wirkung |
|---|---|---|
| `merke` | `thema`, `notiz` | Legt das Thema an, falls neu, und hängt die Notiz an. Setzt `zuletzt_am`. Gleiche Notiz zweimal: kein Duplikat. |
| `erinnere` | `thema` (optional) | Mit Thema: alle Notizen mit Datum. Ohne Thema: Liste aller Themen mit Anzahl der Notizen. Setzt `zuletzt_am` des gelesenen Themas. Unbekanntes Thema: Hinweis plus die Liste der vorhandenen Namen, damit das Modell „Italien" und „Urlaub Italien" zusammenbringen kann. |
| `vergiss` | `thema`, `notiz` (optional) | Ohne `notiz` löscht es das ganze Thema, mit `notiz` die Notiz, die am besten zum Text passt. Das Modell soll vorher nachfragen (Anweisung im Werkzeugtext). |

**Grenzen** (knapp, wie bei `memory.py`): höchstens 30 Themen, 40 Notizen je Thema, 300 Zeichen je Notiz. Bei Überschreitung antwortet das Werkzeug mit einem Hinweis, nichts wird stillschweigend verworfen oder gekürzt.

**Verfall:** Beim Anlegen des Haushalts-Objekts und einmal täglich werden Themen mit `zuletzt_am` älter als 90 Tage gelöscht. Gelesen oder ergänzt zählt als Nutzung.

**Themenliste im Prompt:** Zu Beginn jedes Gesprächs steht ein Satz im System-Prompt, etwa „Haushalts-Gedächtnis, Themen: Urlaub Italien (6), Geburtstag Paula (2). Inhalte holst du mit `erinnere`." Nur Namen, nie Inhalte, damit der Prompt klein bleibt und die Verhaltensregeln nicht verwässert werden.
- Telefon: `roles.full_instructions` bekommt die Themenzeile wie schon den Gedächtnisblock.
- Elektra: Die Bridge ruft beim Gesprächsbeginn `erinnere` ohne Thema über den Werkzeugdienst auf und hängt die Zeile an. Antwortet das Telefon nicht, startet das Gespräch ohne die Zeile (gleiche Haltung wie `fern_werkzeuge`); die Werkzeuge selbst funktionieren trotzdem, sobald das Telefon wieder da ist.

### 3. Verhalten im Gespräch

Die Werkzeugbeschreibungen tragen die Regeln, nicht der Prompt:

- `merke` nur auf ausdrückliche Bitte oder wenn jemand ein Vorhaben nennt und sagt, es solle festgehalten werden. Danach kurz bestätigen („Ist unter Urlaub Italien notiert.").
- `erinnere` jederzeit, wenn die Frage zu einem Thema der Liste passt.
- `web_suche` für alles Aktuelle (Sport, Nachrichten, Öffnungszeiten, Preise, Wetterlage außerhalb von `get_weather`). Nicht für Wissen, das das Modell sicher hat.

### Welche Rollen bekommen was

Die Rollen des Telefons mit `ALL_TOOL_SCHEMAS` bekommen alle vier Werkzeuge automatisch. Rollen mit eigener `tool_schemas`-Liste (Sprachkurs-Rollen, Camille) bleiben unverändert; sie haben Zweck und Sprache für sich. Beim Umsetzen gegenprüfen, welche Rollen das tatsächlich sind (`roles.py`), nicht aus diesem Absatz übernehmen.

## Fehlerfälle

- **Websuche scheitert** (Netz, API-Fehler, Zeitlimit): `ok: false` mit kurzem, vorlesbarem Text. Kein Wiederholen im Werkzeug.
- **Datenbank gesperrt oder defekt:** Werkzeug antwortet mit `ok: false`, das Gespräch läuft weiter. Gedächtnis ist nie Voraussetzung für ein Telefonat.
- **Telefon nicht erreichbar** (aus Sicht von Elektra): bestehende Behandlung in `werkzeuge_fern.py`.

## Tests

- `tests/test_haushalt.py`: Anlegen, Anhängen, Duplikat, Normalisierung („Urlaub  Italien" = „urlaub italien"), Grenzen, Verfall nach 90 Tagen mit festem `now`, Lesen verlängert das Leben, `vergiss` mit und ohne Notiz, unbekanntes Thema liefert die Namensliste.
- `tests/test_websuche.py`: Antwortaufbereitung gegen einen Fake-Client (kein echter Aufruf), Zeitlimit, Tageslimit, API-Fehler.
- `tests/test_werkzeugdienst.py` (oder bestehende Datei): die vier neuen Namen stehen im Katalog, den Elektra bekommt; `NUR_AM_TELEFON` enthält sie nicht.
- **Echtprüfung** (nicht mit Mocks zu ersetzen): eine echte Suche mit einer Frage, deren Antwort heute neu ist; ein Thema am Telefon anlegen und von Elektra abfragen, danach Neustart des Telefondienstes und erneut abfragen (Persistenz).

## Rollout

1. telefon-ai umsetzen und testen, Merge.
2. Telefondienst neu starten (`deploy/push.sh`, Supervised, Zeitpunkt mit Florian). Der Werkzeugdienst liefert die neuen Schemas sofort.
3. Bridge von Elektra neu starten, damit sie die Schemas holt und die Themenzeile einbaut (`rundfunk`, Hardware-Session). Elektras Firmware ändert sich nicht.
4. Messen: je eine Websuche und eine Themenabfrage über Telefon und Elektra.

## Offen

Nichts, das die Umsetzung blockiert. Zu beobachten nach den ersten Wochen: ob 90 Tage Verfall und die Grenzen (30 Themen, 40 Notizen) zur Nutzung passen, und ob die Websuche-Antworten für das Ohr kurz genug sind.
