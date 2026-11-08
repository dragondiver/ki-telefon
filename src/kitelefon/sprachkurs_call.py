"""Sitzungskontext des Franzoesischkurses.

Haelt zusammen, was ein laufender Anruf auf Ziffer 3 braucht: session_id,
Level, VAD-Wert, fertigen Prompt — und die beiden Notpfade: Schnappschuss
beim Start (Netz weg -> letzte bekannte Fassung), Spool beim Ende (Netz
weg -> Datei statt Datenbank, wird beim naechsten Start nachgetragen).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class SprachkursNichtVerfuegbar(RuntimeError):
    """Kein Netz UND kein Schnappschuss — Fehleransage nach Spec 7.6."""


# --- duenne Wrapper um die sprachkurs-Grenze (per monkeypatch testbar) ---


def _verbinden():
    from sprachkurs import db

    return db.connect()


def _kontextblock(conn, name: str) -> str:
    from sprachkurs import kontextblock

    return kontextblock.aus_db(conn, name)


def _learner_level(conn, name: str) -> tuple[int, int]:
    from sprachkurs import lernzustand

    lerner = lernzustand.learner_holen(conn, name)
    return lerner["id"], lerner["difficulty_level"]


def _sitzung_anlegen(conn, learner_id: int, level: int) -> int:
    from sprachkurs import lernzustand

    return lernzustand.sitzung_anlegen(conn, learner_id, difficulty_level=level)


def _offene_fehler(conn, session_id: int) -> list[dict[str, Any]]:
    from sprachkurs import lernzustand

    return lernzustand.offene_fehler(conn, session_id)


def _fehler_notieren(conn, session_id, utterance, correction, category):
    from sprachkurs import lernzustand

    lernzustand.fehler_notieren(conn, session_id, utterance, correction, category)


def _sitzung_abschliessen(conn, session_id, **felder) -> bool:
    from sprachkurs import lernzustand

    return lernzustand.sitzung_abschliessen(conn, session_id, **felder)


def _prompt(kontext: str) -> str:
    from sprachkurs import prompt

    return prompt.laden(kontext)


def _vad(level: int) -> int:
    from sprachkurs.regeln import vad_fuer_level

    return vad_fuer_level(level)


OFFLINE_VAD_MS = 3000  # konservativ: lieber zu geduldig als abschneidend


@dataclass
class SprachkursCall:
    settings: Any
    session_id: int | None
    level: int
    vad_silence_ms: int
    instructions: str
    offline: bool
    _fehler_puffer: list[dict[str, str]] = field(default_factory=list)

    def note_error(
        self, learner_utterance: str, correction: str, category: str | None
    ) -> dict[str, Any]:
        """Still notieren; nie den Gespraechsfluss stoeren (Spec 4.3)."""
        eintrag = {
            "learner_utterance": learner_utterance,
            "correction": correction,
            "category": category,
        }
        if self.offline or self.session_id is None:
            self._fehler_puffer.append(eintrag)
            return {"ok": True}
        try:
            conn = _verbinden()
            try:
                _fehler_notieren(conn, self.session_id, learner_utterance, correction, category)
            finally:
                conn.close()
        except Exception:
            # Mitten im Gespraech ist ein DB-Schluckauf kein Grund fuer
            # eine Stoerung — der Puffer wandert beim Auflegen in den Spool.
            log.exception("note_error: Datenbank nicht erreichbar, puffere")
            self._fehler_puffer.append(eintrag)
        return {"ok": True}

    def offene_fehler_text(self) -> str:
        """Die gesammelten Fehler als Ansage fuer PHASE_ACCURACY."""
        eintraege = list(self._fehler_puffer)
        if not self.offline and self.session_id is not None:
            try:
                conn = _verbinden()
                try:
                    eintraege = _offene_fehler(conn, self.session_id)
                finally:
                    conn.close()
            except Exception:
                log.exception("offene_fehler: Datenbank nicht erreichbar")
        if not eintraege:
            return "(Keine notierten Fehler — geh direkt zum Abschluss über.)"
        zeilen = [f'- "{e["learner_utterance"]}" → richtig: "{e["correction"]}"' for e in eintraege]
        return "Diese Sätze stehen zur Fehlerarbeit an:\n" + "\n".join(zeilen)

    def abschliessen(
        self,
        *,
        duration_s: int,
        speech_ratio_learner: float | None,
        ended_by: str,
        transcript: str | None,
    ) -> dict[str, Any]:
        """DB-Abschluss; bei Offline oder Fehler in den Spool.

        Der Spool enthaelt nur, was tatsaechlich NICHT geschrieben wurde:
        scheitert einer der _fehler_notieren()-Aufrufe mitten in der
        Schleife, wanderten vorher ALLE Fehler erneut in den Spool — auch
        die, die schon in der DB standen. Ein spaeterer Replay haette sie
        doppelt angelegt. session_finalisiert haelt fest, ob der
        Sitzungsdatensatz selbst (duration_s, ended_by, transcript) schon
        geschrieben wurde, damit ein Replay das nicht nochmal versucht.
        """
        session_finalisiert = False
        ungeschrieben = list(self._fehler_puffer)
        if not self.offline and self.session_id is not None:
            geschrieben = 0
            try:
                conn = _verbinden()
                try:
                    _sitzung_abschliessen(
                        conn,
                        self.session_id,
                        duration_s=duration_s,
                        speech_ratio_learner=speech_ratio_learner,
                        ended_by=ended_by,
                        transcript=transcript,
                    )
                    session_finalisiert = True
                    for e in self._fehler_puffer:
                        _fehler_notieren(
                            conn,
                            self.session_id,
                            e["learner_utterance"],
                            e["correction"],
                            e["category"],
                        )
                        geschrieben += 1
                finally:
                    conn.close()
                    # Egal ob die Schleife oben durchlief oder mittendrin
                    # scheiterte: 'geschrieben' zaehlt nur die tatsaechlich
                    # erfolgreichen Aufrufe, der Rest gilt als ungeschrieben.
                    ungeschrieben = self._fehler_puffer[geschrieben:]
                if not ungeschrieben:
                    return {"gespoolt": False, "session_id": self.session_id}
            except Exception:
                log.exception("Abschluss in der DB fehlgeschlagen — spoole")
        daten = {
            "session_id": self.session_id,
            "duration_s": duration_s,
            "speech_ratio_learner": speech_ratio_learner,
            "ended_by": ended_by,
            "transcript": transcript,
            "fehler": ungeschrieben,
            "session_finalisiert": session_finalisiert,
            # Level und Schreibzeitpunkt fuers Nachtragen: sitzung_anlegen()
            # kann beim Replay den echten Startzeitpunkt nicht mehr setzen
            # (die Sitzung traegt dann das Replay-Datum) — started_at haelt
            # das echte Datum wenigstens im JSON fest, das nach erledigt/
            # wandert und dort erhalten bleibt.
            "level": self.level,
            "started_at": datetime.now(UTC).isoformat(),
        }
        pfad = self._spoolen(daten)
        return {"gespoolt": True, "spool": str(pfad)}

    def _spoolen(self, daten: dict[str, Any]) -> Path:
        ordner = Path(self.settings.sprachkurs_spool_dir)
        ordner.mkdir(parents=True, exist_ok=True)
        # Nanosekunden-Aufloesung statt int(time.time()): zwei Sitzungen,
        # die in derselben Sekunde abschliessen (z.B. Test-Suite, oder ein
        # schneller Rueckruf direkt nach der vorigen Sitzung), ueberschrieben
        # sich sonst gegenseitig die Spool-Datei.
        pfad = ordner / f"sitzung-{time.time_ns()}.json"
        pfad.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
        return pfad


def _lernende(settings, learner_name: str | None) -> str:
    """Name aus dem Aufruf oder KITELEFON_SPRACHKURS_LERNENDE; fehlt er, laut."""
    name = learner_name or getattr(settings, "sprachkurs_lernende", "")
    if not name:
        log.error("sprachkurs: KITELEFON_SPRACHKURS_LERNENDE ist nicht gesetzt")
        raise SprachkursNichtVerfuegbar("KITELEFON_SPRACHKURS_LERNENDE fehlt")
    return name


def start_sprachkurs_call(settings, learner_name: str | None = None) -> SprachkursCall:
    """Verbinden + die drei Startabfragen; jeder Fehler dabei degradiert auf
    den Schnappschuss (Spec §7.6), statt nur einen Verbindungsfehler
    abzufangen. Am Abschluss-Review fiel auf: schlug nur eine der drei
    Abfragen fehl (z.B. eine kaputte Zeile in kontextblock), landete der
    Anruf bisher in einer unbehandelten Exception statt im Offline-Pfad."""
    learner_name = _lernende(settings, learner_name)
    try:
        conn = _verbinden()
        try:
            kontext = _kontextblock(conn, learner_name)
            learner_id, level = _learner_level(conn, learner_name)
            session_id = _sitzung_anlegen(conn, learner_id, level)
        finally:
            conn.close()
    except Exception:
        log.exception("sprachkurs: Start ueber die Datenbank fehlgeschlagen — Schnappschuss")
        snapshot = Path(settings.sprachkurs_snapshot_path)
        if not snapshot.exists():
            raise SprachkursNichtVerfuegbar(
                "weder Datenbank noch Schnappschuss vorhanden"
            ) from None
        kontext = snapshot.read_text(encoding="utf-8")
        log.info(
            "sprachkurs: Sitzung %s, Level %s, VAD %s ms, offline=%s",
            None,
            1,
            OFFLINE_VAD_MS,
            True,
        )
        return SprachkursCall(
            settings=settings,
            session_id=None,
            level=1,
            vad_silence_ms=OFFLINE_VAD_MS,
            instructions=_prompt(kontext),
            offline=True,
        )

    snapshot = Path(settings.sprachkurs_snapshot_path)
    snapshot.write_text(kontext, encoding="utf-8")
    log.info(
        "sprachkurs: Sitzung %s, Level %s, VAD %s ms, offline=%s",
        session_id,
        level,
        _vad(level),
        False,
    )
    return SprachkursCall(
        settings=settings,
        session_id=session_id,
        level=level,
        vad_silence_ms=_vad(level),
        instructions=_prompt(kontext),
        offline=False,
    )


def _spool_datei_neu_schreiben(pfad: Path, daten: dict[str, Any]) -> None:
    """Schreibt den Zwischenstand einer Spool-Datei zurueck — atomar.

    Noetig fuer Teilerfolge: wurde die Sitzung schon angelegt und
    abgeschlossen, aber ein spaeterer _fehler_notieren()-Aufruf scheitert,
    muss die Datei DAS sofort widerspiegeln — sonst legt ein Retry die
    Sitzung ein zweites Mal an (Review-Fund: genau der Doppel-Anlage-Fall,
    den der Spool verhindern soll).

    Erst in eine .tmp-Datei schreiben, dann per os.replace() umbenennen:
    auf POSIX ist das atomar, die Datei auf der Platte ist zu jedem
    Zeitpunkt entweder vollstaendig der alte ODER vollstaendig der neue
    Stand, nie ein halb geschriebener Torso. Ein direktes write_text()
    koennte bei einem Absturz mittendrin (z.B. voller Speicher auf einer
    SD-Karte) eine kaputte oder unvollstaendige Datei hinterlassen — beim
    Retry entweder unlesbar (zaehlt als fehlgeschlagen, aber der DB-Stand
    waere schon vorangekommen) oder, schlimmer, sie wuerde weiterhin den
    ALTEN Stand vortaeuschen und damit wieder in die Doppelanlage laufen.
    """
    tmp = pfad.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.replace(tmp, pfad)
    except Exception:
        # Aufraeumen, damit keine Leiche neben der (unveraenderten) alten
        # Datei liegen bleibt — sie zaehlt sonst als kaputte Restdatei, die
        # ein spaeterer Lauf gar nicht erst als *.json aufgreift, aber
        # trotzdem unnoetig herumliegt.
        tmp.unlink(missing_ok=True)
        raise


def _spool_datei_nachtragen(pfad: Path, daten: dict[str, Any], learner_name: str) -> int | None:
    """Traegt eine einzelne Spool-Datei in die Datenbank nach.

    Gibt die (neu angelegte oder vorhandene) session_id zurueck, oder None,
    falls die Datei nie eine kannte. Wirft weiter, wenn irgendein
    DB-Zugriff scheitert — der Aufrufer entscheidet dann ueber den Verbleib
    der Datei. Schreibt die Datei selbst zurueck, sobald ein Teilschritt
    unwiderruflich in der DB steht, damit ein spaeterer Retry nicht
    doppelt anlegt.
    """
    conn = _verbinden()
    try:
        session_id = daten.get("session_id")
        if not daten.get("session_finalisiert"):
            if session_id is None:
                # Offline-Sitzung: nie angelegt. sitzung_anlegen() kann den
                # echten Startzeitpunkt nicht mehr setzen — die Sitzung
                # traegt das Replay-Datum, das echte Datum steht in
                # started_at im Spool-JSON (ueberlebt in erledigt/).
                learner_id, aktuelles_level = _learner_level(conn, learner_name)
                level = daten.get("level", aktuelles_level)
                session_id = _sitzung_anlegen(conn, learner_id, level)
            _sitzung_abschliessen(
                conn,
                session_id,
                duration_s=daten.get("duration_s"),
                speech_ratio_learner=daten.get("speech_ratio_learner"),
                ended_by=daten.get("ended_by"),
                transcript=daten.get("transcript"),
            )
            # Ab hier steht die Sitzung in der DB — VOR der Fehlerschleife
            # zurueckschreiben. Scheitert unten ein _fehler_notieren(),
            # findet ein Retry hier bereits session_finalisiert: true vor
            # und geht in den (b)-Pfad statt eine zweite Sitzung anzulegen.
            #
            # Restfenster (bewusst getragen): scheitert diese Rueckschreibung
            # selbst (z.B. Plattenfehler), bleibt die ALTE Datei vollstaendig
            # erhalten (os.replace ist atomar) — die Sitzung steht dann aber
            # schon in der DB, waehrend die Datei noch session_id: null
            # zeigt. Ein Retry kann in diesem Fall die Sitzung doppelt
            # anlegen. Das Fenster ist eng (ein Plattenfehler unmittelbar
            # nach erfolgreichem DB-Schreiben) und wird in Kauf genommen:
            # die Alternative — die Sitzung erst NACH der Rueckschreibung
            # anzulegen — wuerde einen erfolgreich gefuehrten Anruf bei
            # vollem Datentraeger komplett verlieren, statt ihn im
            # schlimmsten Fall nur doppelt zu zaehlen.
            daten = {**daten, "session_id": session_id, "session_finalisiert": True}
            _spool_datei_neu_schreiben(pfad, daten)
        restliche = list(daten.get("fehler", []))
        for i, eintrag in enumerate(restliche):
            try:
                _fehler_notieren(
                    conn,
                    session_id,
                    eintrag["learner_utterance"],
                    eintrag["correction"],
                    eintrag["category"],
                )
            except Exception:
                # Nur die noch ungeschriebenen Fehler zurueckschreiben — die
                # vorherigen stehen schon in der DB, ein Retry darf sie
                # nicht ein zweites Mal notieren.
                daten = {
                    **daten,
                    "session_id": session_id,
                    "session_finalisiert": True,
                    "fehler": restliche[i:],
                }
                _spool_datei_neu_schreiben(pfad, daten)
                raise
        return session_id
    finally:
        conn.close()


def _eindeutiger_zielpfad(ordner: Path, name: str) -> Path:
    """Kollisionsschutz beim Verschieben nach erledigt/: nie eine dort
    vorhandene Datei ueberschreiben — sonst verletzt das "geloescht wird
    nie" der Sache nach doch, nur eben durch Ueberschreiben statt Loeschen.
    """
    ziel = ordner / name
    if not ziel.exists():
        return ziel
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 2
    while True:
        kandidat = ordner / f"{stem}-{i}{suffix}"
        if not kandidat.exists():
            return kandidat
        i += 1


def spool_nachtragen(settings, queue, learner_name: str | None = None) -> dict[str, int]:
    """Traegt beim Prozessstart einmalig alles nach, was offline liegen
    blieb (Spec 7.6 / Task 7): jede Datei in sprachkurs_spool_dir wird
    gelesen, in die Datenbank nachgetragen, die Auswertung enqueued und
    die Datei nach erledigt/ verschoben — geloescht wird nie, damit ein
    Vorfall forensisch nachvollziehbar bleibt. Scheitert eine Datei
    (unlesbar oder DB-/Queue-Fehler), bleibt sie liegen und der naechste
    Start versucht es erneut; die Replay-Schleife laeuft trotzdem mit der
    naechsten Datei weiter.
    """
    ordner = Path(settings.sprachkurs_spool_dir)
    nachgetragen = 0
    fehlgeschlagen = 0
    if not ordner.exists():
        return {"nachgetragen": nachgetragen, "fehlgeschlagen": fehlgeschlagen}

    learner_name = learner_name or getattr(settings, "sprachkurs_lernende", "")
    if not learner_name:
        # Ohne Namen weder Lernstand noch Empfaenger: liegen lassen, aber laut.
        offen = len(list(ordner.glob("*.json")))
        if offen:
            log.error(
                "Spool-Replay: KITELEFON_SPRACHKURS_LERNENDE fehlt, %d Datei(en) bleiben liegen",
                offen,
            )
        return {"nachgetragen": 0, "fehlgeschlagen": offen}

    erledigt = ordner / "erledigt"
    for pfad in sorted(ordner.glob("*.json")):
        try:
            daten = json.loads(pfad.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Spool-Replay: %s ist nicht lesbar — bleibt liegen", pfad)
            fehlgeschlagen += 1
            continue

        try:
            session_id = _spool_datei_nachtragen(pfad, daten, learner_name)
            # Enqueue VOR dem Verschieben, beides innerhalb desselben
            # try/except: scheitert die Queue, bleibt die Datei liegen und
            # ein Retry versucht beides erneut. Ist die Queue beim Retry
            # ok, aber das Verschieben scheitert danach, wird ein zweiter
            # Auswertungs-Task enqueued — akzeptiert, sitzung_auswerten()
            # ist ueber bereits_ausgewertet idempotent und bricht dann
            # still ab.
            if getattr(settings, "sprachkurs_enqueue", False) and session_id is not None:
                queue.enqueue(
                    agent_type="sprachkurs_auswertung",
                    description="Französisch-Sitzung auswerten",
                    deliver_via="email",
                    deliver_to=learner_name.lower(),
                    payload={"session_id": session_id},
                    konsument="sprachkurs",
                )
            erledigt.mkdir(parents=True, exist_ok=True)
            pfad.rename(_eindeutiger_zielpfad(erledigt, pfad.name))
        except Exception:
            log.exception("Spool-Replay: %s fehlgeschlagen — bleibt liegen", pfad)
            fehlgeschlagen += 1
            continue

        nachgetragen += 1

    return {"nachgetragen": nachgetragen, "fehlgeschlagen": fehlgeschlagen}
