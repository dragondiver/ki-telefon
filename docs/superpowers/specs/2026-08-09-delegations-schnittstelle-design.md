# Delegations-Schnittstelle — Design

**Datum:** 2026-08-09
**Status:** abgestimmt, Umsetzung folgt (fast reine Dokumentation)

## Ausgangslage

In `tasks.db` liegen vier unbeantwortete Aufgaben, zwei davon aus einem
echten Bedürfnis: „Find the current driving time from Stuttgart to
Sindelfingen." und „Find the Quarkus version that is compatible with
Java 5." Phase 1 schreibt nur `INSERT` — das ist Absicht (`CLAUDE.md`),
aber niemand liest die Zeilen bisher, und `delegate_task` verspricht dem
Anrufer korrekt keine Zustellung, weil es keine gibt.

Ursprünglich als Lösung angedacht: Hermes Agent (Nous Research), lokal
auf einem freien Pi. Im Brainstorming kam heraus, dass der eigentliche
Wunsch weiter reicht — ein **autonomer, mehrere-Provider-fähiger Agent
auf einem noch zu kaufenden Mini-PC**, nutzbar nicht nur vom Telefon,
sondern auch aus anderen Projekten heraus, mit eigenem Terminplan für
wiederkehrende Aufträge („jeden Tag eine Zusammenfassung der News über
unseren Ort").

## Entscheidung: zwei getrennte Vorhaben

Ein autonomer Mehrprojekt-Agent mit Zugriff auf mehrere KI-Anbieter ist
ein eigenständiges Infrastruktur-Vorhaben — eigene Hardware-Entscheidung
(noch nicht getroffen), eigene Absicherung, eigener Betrieb, eigene
Spec. Er als Unterabschnitt der telefon-ai-Doku würde diese Spec an eine
Kaufentscheidung binden, die noch aussteht.

Diese Spec deckt deshalb **nur die Telefon-Seite** ab: wie `kitelefon`
eine Aufgabe abgibt, und was ein künftiger externer Agent an stabilem
Vertrag vorfindet, wenn er gebaut wird. Der Agent selbst — Hardware,
Hermes-Einrichtung, Provider-Schlüssel, Terminplan-Logik — ist bewusst
außerhalb dieser Spec und bekommt sein eigenes Dokument, sobald der
Mini-PC steht.

## Schnittstelle: `tasks.db` bleibt der Vertrag

Keine neue Bibliothek, kein neuer Dienst auf `kitelefon` — das war eine
ausdrückliche Anforderung aus dem Brainstorming, nachdem Temperatur
(61,5 °C, `throttled=0x0`) und Lastprofil (Hintergrundaufgaben sind
API-Wartezeit, keine CPU-Last) zwar unproblematisch waren, das Telefon
aber trotzdem „ein Telefon bleiben" soll.

- `kitelefon` schreibt weiterhin nur (`TaskQueue.enqueue`, `INSERT`).
  Diese Phase-1-Grenze aus `CLAUDE.md` bleibt unangetastet.
- Der externe Agent liest und aktualisiert dieselbe SQLite-Datei von
  außen — Zugriffsweg (z.B. über den bereits eingerichteten
  Tailscale-Zugang) ist seine eigene Entscheidung, nicht Teil dieser
  Spec.
- Schema unverändert (`id, created_at, agent_type, description,
  deliver_via, deliver_to, payload_json, status, result, started_at,
  finished_at`).

**Verworfen: Kafka.** Ein Message-Broker-Cluster für ein paar Aufgaben
pro Woche von einem einzigen Gerät steht in keinem Verhältnis zum
Betriebsaufwand — und widerspricht der gerade getroffenen Entscheidung,
möglichst nichts Neues auf `kitelefon` laufen zu lassen. Eine
SQLite-Datei entkoppelt Schreiber und Leser bereits vollständig, ganz
ohne zusätzlichen Prozess.

**Verworfen: aktiver Aufruf/Webhook beim Delegieren.** Würde `kitelefon`
an eine URL koppeln, die es noch nicht gibt (der Agent existiert ja
noch nicht) — mehr Kopplung an ein Vorhaben, das gerade erst entsteht,
für einen Vorteil (sofortige statt spätere Zustellung), den niemand
gefordert hat: „kein festes Zeitfenster, Hauptsache zuverlässig" war
die ausdrückliche Antwort auf die Frage nach Tempo.

## Wiederkehrende Aufträge: kein neues Feld

Der Tagesauftrag „jeden Tag …" braucht kein strukturiertes
`schedule`-Feld (Cron-Ausdruck? `daily`/`weekly`-Enum?). Ein Format
jetzt festzulegen hieße raten, was der künftige — LLM-gestützte —
Agent erwartet, bevor er existiert. Das freie Textfeld `description`
trägt „jeden Tag eine Zusammenfassung der News über
unseren Ort" bereits vollständig; Wiederkehr aus
natürlicher Sprache herauszulesen ist für ein Sprachmodell keine harte
Aufgabe. Ein Terminplan-Feld nachzuziehen, sobald der reale Konsument
feststeht, ist die kleinere spätere Änderung als jetzt ein falsches
Format zu fixieren.

## Prompt: keine Änderung

`delegate_task` verspricht schon heute korrekt keine Zustellung
(`roles.py`: „FRAG NICHT nach Email oder Rückruf. Die Zustellung ist
noch nicht gebaut."). Das bleibt richtig, solange die Zustellung
tatsächlich nicht existiert. Erst wenn der externe Agent wirklich
liefert, wird dieser Satz falsch — das ist dann Teil der nächsten
Änderung, nicht dieser.

## Ergebnis dieser Spec

Fast reine Dokumentation, kein Code-Delta:

1. `CLAUDE.md`: Notiz, dass der allgemeine Agent (Hermes, Mini-PC)
   bewusst kein Teil von telefon-ai ist, sondern ein eigenes Vorhaben
   mit eigener Spec.
2. Dieses Dokument als stabiler Schnittstellen-Vertrag für das externe
   Agenten-Vorhaben, wenn es beginnt.

## Offene Punkte für das externe Agenten-Vorhaben (nicht hier)

- Hardware (Mini-PC, noch nicht gekauft)
- Hermes-Einrichtung, Provider-Schlüssel (Claude, OpenAI, Kimi, GLM,
  Qwen — „das soll ja Hermes entscheiden")
- Zugriffsweg auf `tasks.db` (vermutlich über Tailscale)
- Terminplan-Logik für wiederkehrende Aufträge
- Rückkanal-Umsetzung (Email vs. Rückruf mit Klingel + TTS — Anrufer
  entscheidet laut bestehendem `deliver_via`-Feld)
- Verlässlichkeits-Anforderung aus dem Brainstorming: „sehr kritisch",
  lieber ehrliches Nichtwissen als eine falsche Antwort — betrifft die
  Wahl und Führung des ausführenden Modells, nicht die Telefon-Seite.
