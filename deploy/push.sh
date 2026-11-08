#!/bin/bash
# Pi auf den Stand von origin/main bringen.
#
# sprachkurs kommt als Git-Abhängigkeit auf festem Commit (`rev` in
# pyproject.toml, telefon-ai#81) — `uv sync` holt genau diesen Stand. Eine neue
# Camille erreicht den Pi also erst, wenn `rev` in telefon-ai angehoben und
# gemergt ist. Ein Checkout ~/sprachkurs auf dem Pi wird nicht mehr gebraucht.
#
# Nur Git, kein rsync: Ein rsync-Stand sieht auf dem Pi wie 80 uncommittete
# Änderungen aus und blockiert jedes spätere `git pull` (so geschehen am
# 12./13.09.2026). Ungepushte Zwischenstände testet man über einen gepushten
# Branch, den man auf dem Pi auscheckt.
#
# Aufruf vom Mac aus: ZIEL=<host> deploy/push.sh
#   ZIEL=…       Host des Pi, Pflicht (z. B. ZIEL=kitelefon.local)
#   KLINGELN=0   Funktionsklingeln beim Neustart unterdrücken
set -euo pipefail

ZIEL=${ZIEL:?ZIEL fehlt, z. B. ZIEL=kitelefon.local deploy/push.sh}
KLINGELN=${KLINGELN:-1}

# uv liegt auf dem Pi nur im PATH der Login-Shell, daher überall bash -lc.
pi() { ssh "$ZIEL" "bash -lc '$*'"; }

echo "→ Arbeitsbaum auf $ZIEL prüfen"
for repo in telefon-ai; do
  # Geänderte versionierte Dateien würde ein Pull überschreiben — Abbruch.
  # Unversionierte Dateien (Kontext-Dumps, Notizen) lässt ein Pull in Ruhe.
  offen=$(pi "cd ~/$repo && git status --porcelain --untracked-files=no")
  if [ -n "$offen" ]; then
    echo "   $repo hat uncommittete Änderungen — Abbruch, nichts überschrieben:" >&2
    echo "$offen" | sed 's/^/     /' >&2
    echo "   (Eine zweite Claude-Instanz arbeitet auf dem Pi; erst dort committen oder verwerfen.)" >&2
    exit 1
  fi
  fremd=$(pi "cd ~/$repo && git status --porcelain --untracked-files=all | grep '^??' || true")
  [ -n "$fremd" ] && echo "   $repo: unversioniert, bleibt liegen: $(echo "$fremd" | sed 's/^?? //' | tr '\n' ' ')"
done

echo "→ git pull (fast-forward) in telefon-ai"
pi "cd ~/telefon-ai && git pull --ff-only -q && git log -1 --format=\"   telefon-ai  %h %s\""

echo "→ uv sync auf dem Pi (Abhängigkeiten, sprachkurs auf dem Commit aus pyproject.toml)"
pi "cd ~/telefon-ai && uv sync --extra pi -q"

# Uhrzeit des Pi vor dem Neustart: Erst eine Startzeile ab diesem Moment
# stammt vom neuen Prozess. Sofort nach `systemctl restart` steht im Log noch
# der alte Lauf (26.09.2026: die Anzeige zeigte Zeilen vom Vorabend).
vorher=$(pi "date +%F\ %T")

if [ "$KLINGELN" = "1" ]; then
  echo "→ Dienst neu starten — das Telefon klingelt dabei einmal (Funktionstest)."
  echo "  (KLINGELN=0 deploy/push.sh unterdrückt das.)  Fragt nach dem sudo-Passwort."
  ssh -t "$ZIEL" "sudo systemctl restart kitelefon"
else
  echo "→ Dienst neu starten, ohne Klingeln. Fragt nach dem sudo-Passwort."
  ssh -t "$ZIEL" "sudo systemctl set-environment KITELEFON_STARTUP_RING=false && \
                  sudo systemctl restart kitelefon; \
                  sudo systemctl unset-environment KITELEFON_STARTUP_RING"
fi

echo "→ Zustand"
pi "systemctl is-active kitelefon" || true
# Auf die Startzeile des neuen Prozesses warten (Zeitstempel als Text
# vergleichbar: "2026-09-26 17:06:05"), höchstens 15 s.
start=""
for _ in $(seq 15); do
  start=$(pi "grep -a \"──────── Start ────────\" ~/telefon-ai/kitelefon.log | tail -1 | cut -c1-19")
  [[ ! "$start" < "$vorher" ]] && break
  sleep 1
done
if [[ "$start" < "$vorher" ]]; then
  echo "   Keine neue Startzeile in kitelefon.log nach 15 s — der Start hängt oder schlug fehl." >&2
  echo "   Nachsehen: ssh $ZIEL journalctl -u kitelefon -n 30" >&2
  exit 1
fi
sleep 1  # die Zeilen nach „Start“ folgen binnen Millisekunden
pi "tail -3 ~/telefon-ai/kitelefon.log | cut -c1-120"
