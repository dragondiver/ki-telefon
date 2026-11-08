#!/usr/bin/env python3
"""Spotify-Diagnose: Sind die Konten noch angemeldet, ist der Evoke sichtbar?

    python scripts/spotify_check.py

Prüft pro Konto:
  1. Refresh-Token noch gültig? (Der häufigste Ausfall — Spotify zieht ein
     Refresh-Token zurück, wenn die App-Berechtigung entzogen oder das
     Passwort geändert wurde. Im Telefon klang das früher nach "Spotify
     antwortet nicht".)
  2. Welche Connect-Geräte sieht das Konto, ist der Evoke dabei?

Nutzt bewusst die echte SpotifyAccount-Klasse: Spotify kann beim Refresh ein
neues Refresh-Token ausgeben, und die Klasse schreibt es zurück in
token_<konto>.json. Ein selbstgebauter Aufruf würde die Rotation verlieren
und das Konto damit erst recht abmelden.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from kitelefon.agents.spotify import (  # noqa: E402
    EVOKE_NAME,
    KNOWN_ACCOUNTS,
    SpotifyAuthError,
    _get_account,
)

OK = "\033[32m✓\033[0m"
FEHLER = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"


async def pruefe_konto(client: httpx.AsyncClient, name: str) -> bool:
    print(f"\n── Konto '{name}' " + "─" * 40)
    try:
        acct = _get_account(name)
    except FileNotFoundError as exc:
        print(f"  {FEHLER} {exc}")
        return False

    try:
        await acct._access(client)
    except SpotifyAuthError as exc:
        print(f"  {FEHLER} Anmeldung abgelaufen: {exc.grund}")
        print(f"      Neu einrichten:  python scripts/spotify_auth.py --account {name}")
        return False
    except httpx.HTTPError as exc:
        print(f"  {FEHLER} Spotify nicht erreichbar: {exc}")
        return False
    print(f"  {OK} Anmeldung gültig")

    try:
        r = await client.get(
            "https://api.spotify.com/v1/me/player/devices",
            headers=await acct._headers(client),
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  {FEHLER} Geräteliste nicht abrufbar: {exc}")
        return False

    geraete = r.json().get("devices", [])
    if not geraete:
        print(f"  {WARN} Kein einziges Connect-Gerät sichtbar.")
    else:
        print(f"  {OK} {len(geraete)} Gerät(e) sichtbar:")
        for d in geraete:
            aktiv = " (aktiv)" if d.get("is_active") else ""
            print(f"      · {d['name']} — {d.get('type', '?')}{aktiv}")

    if any(EVOKE_NAME.lower() in d["name"].lower() for d in geraete):
        print(f"  {OK} '{EVOKE_NAME}' gefunden")
        return True

    print(f"  {WARN} '{EVOKE_NAME}' NICHT dabei.")
    print("      Der Evoke meldet sich bei Spotify Connect erst, wenn er wach")
    print("      und im selben WLAN ist. Einmal am Gerät Musik starten weckt ihn.")
    return False


async def main() -> int:
    print(f"Suche nach Connect-Gerät: '{EVOKE_NAME}'")
    print(f"Konten laut .env: {', '.join(KNOWN_ACCOUNTS)}")
    async with httpx.AsyncClient() as client:
        ergebnisse = [await pruefe_konto(client, n) for n in KNOWN_ACCOUNTS]

    print("\n" + "═" * 52)
    if all(ergebnisse):
        print(f"{OK} Alles bereit.")
        return 0
    if not any(ergebnisse):
        print(f"{FEHLER} Kein Konto ist spielbereit — siehe Hinweise oben.")
        return 1
    print(f"{WARN} Teilweise bereit — siehe Hinweise oben.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
