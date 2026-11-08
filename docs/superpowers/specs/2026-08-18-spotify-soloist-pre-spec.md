# Spotify Soloist für telefon-ai — Pre-Spec

**Datum:** 2026-08-18
**Status:** Pre-Spec — erörtert, Ergebnis: **für das eigentliche Ziel
(bessere Suche) ungeeignet.** Siehe Nachtrag am Ende. Die Optionen A/B
bleiben als Ideenspeicher stehen, werden aber nicht weiterverfolgt.

## Was ist Spotify Soloist?

Am 13.08.2026 hat Spotify „Soloist" vorgestellt: einen **offiziellen,
headless Spotify-Connect-Receiver für Linux** (Terminal-Client, closed
source, Binary-Download über Spotify for Developers). Kernpunkte aus
Ankündigung, README (`github.com/spotify/soloist`) und Doku:

- Läuft auf ARMv7, AArch64, x86_64 — **Pi 5 ist explizit Zielplattform.**
- Audio-Ausgabe über **PipeWire** (oder PulseAudio).
- Erscheint in der Spotify-App als ganz normales Connect-Gerät; gleiche
  Playback-Engine wie die offiziellen Apps (Smart Shuffle, Autoplay,
  Loudness-Normalisierung, Jam; Lossless und Crossfade mit Premium).
- **Lokale WebSocket-API**: Playback-Kommandos (play, pause, seek, volume,
  shuffle, repeat, add to queue), Echtzeit-Events, Queue-Zugriff mit
  Metadaten (bis ~80 kommende Titel). Query-Kommandos liefern eigene
  Event-Typen (`get_state` → `playback_state`, `get_queue` →
  `queue_changed`).
- Setup: Developer-Account nötig (Download + „device credential"/API-Key),
  Premium-Konto fürs Einrichten; danach können sich Free- **und**
  Premium-Nutzer verbinden. Kein Config-File — alles über CLI-Optionen und
  `--data-dir` (gleiches Datenverzeichnis = gleiche Geräteidentität und
  gespeicherte Connect-Session über Neustarts hinweg).
- Einordnung: der offizielle Nachfolger dessen, wofür bisher
  librespot/raspotify (Community, nur Premium) herhalten mussten.

Quellen: [Ankündigung](https://developer.spotify.com/blog/2026-08-13-introducing-spotify-soloist),
[Doku](https://developer.spotify.com/documentation/soloist),
[README](https://github.com/spotify/soloist)
(developer.spotify.com war aus der Recherche-Umgebung nur über
Suchtreffer einsehbar — Details der WebSocket-Referenz vor einer echten
Spec direkt nachlesen).

## Ausgangslage in telefon-ai

`spotify_play` steuert heute den **Pure Evoke Play** über die
**Spotify Web API** (`src/kitelefon/agents/spotify.py`): PKCE-Tokens pro
Duo-Konto, Kontowahl als Kaskade, Playlist/Track/Artist-Suche, Playback via
`PUT /me/player/play` mit `device_id` des Evoke. Bekannte Schwächen:

1. **„Der Evoke ist gerade nicht erreichbar."** Der Lautsprecher fällt aus
   der Connect-Geräteliste, wenn er schläft oder das WLAN zickt. Dann ist
   der ganze Pfad tot — es gibt kein zweites Abspielgerät.
2. `spotify_now_playing` pollt die Web API über beide Konten — Cloud-Umweg
   für eine Frage, die das Wohnzimmer betrifft.
3. Kein Pause/Lauter/Leiser implementiert (wäre über Web API möglich,
   ist aber bisher nicht gebaut).

## Die zentrale Erkenntnis zuerst

**Soloist ist ein Receiver, kein Controller.** Er macht das Gerät, auf dem
er läuft, zum Abspielgerät — er kann nicht den Evoke fernsteuern. Für den
Kernpfad „Stimme → Playlist läuft auf dem Evoke" ändert Soloist **nichts**:
Suche und Playback-Start laufen weiter über die Web API, die
Duo-Kontokaskade bleibt wie sie ist. Ein Umbau der bestehenden Integration
steht nicht zur Debatte.

Interessant ist Soloist als **zusätzliche Connect-Zone auf dem Pi selbst**.

## Optionen

### A. „Telefonkonzert" — Musik im Hörer

Soloist läuft auf dem Pi, Ausgabe über PipeWire auf die USB-Soundkarte →
Musik kommt **aus der Hörmuschel des Telefons**. Man wählt eine Ziffer
(oder sagt es Marlene), legt den Hörer ans Ohr und hört Musik wie 1965 die
Zeitansage. Passt perfekt zum Charme des Projekts. Der Pi ist dann in der
Spotify-App als Gerät „Telefon" sichtbar — auch vom Sofa aus bespielbar.

### B. Fallback, wenn der Evoke weg ist

Heute endet der Wunsch im Fehlerfall mit „nicht erreichbar". Mit Soloist
als zweitem Connect-Gerät könnte der Handler anbieten: *„Der Evoke
antwortet nicht — soll ich es dir am Telefon vorspielen?"* Gleicher
Web-API-Aufruf, nur andere `device_id`. Kleiner Eingriff in
`spotify.py` (`find_device` mit Fallback-Gerät).

### C. Lokale Events statt Cloud-Polling

Die WebSocket-API liefert Now-Playing und Queue in Echtzeit — aber **nur
für die Soloist-Zone**, nicht für den Evoke. Solange die Musik primär auf
dem Evoke läuft, hilft das `spotify_now_playing` nicht. Nutzen entsteht
erst, wenn A/B real genutzt werden. → Kein eigenständiger Grund.

### D. Ersatz für die Web API

Geht nicht (siehe oben). Auch Pause/Volume für den **Evoke** bleiben
Web-API-Aufgaben.

## Risiken und offene Fragen

- **Audio-Gerätekonflikt (das Hauptrisiko).** Die Gesprächspipeline nutzt
  `sounddevice`/PortAudio auf derselben USB-Soundkarte (PCM16 mono 24 kHz).
  Soloist spricht PipeWire. Ob PortAudio auf dem Pi durch PipeWire geht
  oder ALSA direkt greift (→ „device busy" oder Sample-Raten-Gerangel),
  muss ein Experiment zeigen. Regel wäre ohnehin: **Gespräch hat Vorrang**
  — klingelt es bzw. wird abgehoben, pausiert Soloist (WebSocket `pause`,
  ein lokaler Call in `call_manager`).
- **Frische der Software.** Soloist ist fünf Tage alt, v1, closed source,
  Bug-Tracker frisch eröffnet. Stabilität auf dem Pi unbewiesen.
- **Noch ein Geheimnis.** Device-Credential/API-Key aus dem
  Developer-Account → neuer Eintrag in `.env`, neuer systemd-Service in
  der Pi-Inbetriebnahme-Doku.
- **Duo-Verhalten ungeklärt.** Soloist hält *eine* gespeicherte
  Connect-Session pro `--data-dir`. Ob beide Duo-Konten das Gerät
  gleichzeitig in ihrer Geräteliste sehen (wie beim Evoke) oder ob die
  Zone einem Konto „gehört", muss der Versuch zeigen. Falls Letzteres:
  zwei Instanzen mit getrennten data-dirs und Gerätenamen wären der
  hässliche Ausweg.
- **Klangfrage.** Lossless 24-bit in eine Hörmuschel mit
  Telefonband-Charakteristik — technisch egal, ästhetisch vielleicht
  gerade der Witz.

## Einschätzung und Empfehlung

*(Stand vor der Besprechung — vom Nachtrag überholt: es wird kein Spike
geplant. Bleibt als Ideenspeicher stehen, falls A/B später doch reizen.)*

Soloist ist für telefon-ai **kein Baustein für den Kernpfad, aber ein
charmantes Add-on** — und die erste offizielle Antwort von Spotify auf
genau die Gerätekategorie, zu der dieses Telefon gehört. Option A trifft
den Geist des Projekts, Option B behebt den real nervigsten Fehlerfall.

Vorschlag als nächster Schritt (vor jeder Spec):

1. **Spike auf dem Pi** (ein Nachmittag): Soloist installieren, Ausgabe auf
   die USB-Soundkarte routen, parallel ein Gespräch führen. Beantwortet
   Gerätekonflikt und Duo-Sichtbarkeit.
2. Fällt der Spike positiv aus → echte Design-Spec für A + B
   (Pausier-Logik im `call_manager`, Fallback in `spotify.py`,
   systemd-Unit, `.env`, Pi-Doku).
3. Fällt er negativ aus → Thema in sechs Monaten wieder anschauen;
   Soloist ist neu, die Doku wächst noch.

## Bewusst nicht dabei (YAGNI)

- Soloist auf dem Mac (läuft nur auf Linux — Mac-Dev bleibt Web-API-only,
  der Fallback-Pfad braucht dafür einen Feature-Schalter, z. B.
  `KITELEFON_SOLOIST_DEVICE` leer = aus).
- Eigene Warteschlangen-/DJ-Logik über die WebSocket-API.
- Jam/Gruppen-Sessions.
- Musik **ins Gespräch** mischen (Realtime-Session hört mit — nein).

## Nachtrag: Das eigentliche Interesse war die Suche

In der Besprechung stellte sich heraus: Der Anlass war die Hoffnung,
Soloist könne die **Suche** verbessern — abspielen sollte weiterhin der
Evoke. Das kann Soloist nicht: Seine WebSocket-API bietet keinerlei
Such-Endpunkt, nur Playback-Kommandos, Events und die eigene Queue.
Suche gibt es bei Spotify ausschließlich in der **Web API** — also genau
dort, wo `spotify.py` heute schon sucht. Soloist ändert am Pfad
„Wunsch → Suche → Evoke" nichts und ist für dieses Ziel ungeeignet.

**Damit ist diese Pre-Spec abgeschlossen.** Bessere Suche ist eine
eigene, kleine Baustelle innerhalb von `spotify.py`, ganz ohne Soloist.
Der Ideenspeicher dafür:

1. **Fuzzy-Match für Playlist-Namen.** Heute reiner
   Teilstring-Vergleich über `/me/playlists`. Transkribierte Sprache
   liefert aber Näherungen („Dinner-Jass" für „Dinner Jazz") —
   `difflib.get_close_matches` o. ä. über alle Playlist-Namen wäre
   robuster und braucht kein neues API.
2. **Mehrere Treffer statt `limit: 1`.** `find_track` nimmt stur den
   ersten Katalog-Treffer. Mit 3–5 Kandidaten kann der Handler bei
   knappen Fällen zurückfragen oder das Realtime-Modell wählen lassen.
3. **Katalog-Playlists.** `/v1/search` mit `type=playlist` durchsucht
   den öffentlichen Katalog — heute finden wir nur Playlists aus den
   eigenen Bibliotheken (plus `playlists.json`-Shortcuts).

Nächster Schritt, wenn gewünscht: eigene kleine Design-Spec
„Spotify-Suche verbessern" mit diesen drei Punkten.
