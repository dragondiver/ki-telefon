"""Spotify-Connect-Steuerung mit Duo-Konto-Kaskade.

Lädt pro Konto das von scripts/spotify_auth.py erzeugte Refresh-Token, holt
sich bei Bedarf ein frisches Access-Token und steuert die Wiedergabe auf
einem Connect-Gerät (Feld `geraet`: Evoke Play als Vorgabe oder die Anlage,
jeweils per Namensteil gesucht).

Kontowahl läuft NICHT über Stimmerkennung, sondern in drei Stufen:
  1. Telefon fragt beim Abheben "Wer ist da?" -> Name fürs Gespräch.
  2. Selbstnennung im Wunsch ("Hier Paula, spiel ...") -> Name daraus.
  3. Unbekannt: Playlist in beiden Konten suchen. Genau ein Treffer -> spielt.
     Mehrere -> Rückfrage. Keiner -> Rückfrage.

Stufen 1+2 macht der System-Prompt, Stufe 3 dieser Handler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from kitelefon.personen import SPOTIFY_KONTEN

load_dotenv()
log = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REPO = Path(__file__).resolve().parents[3]

# Namen aus der .env (SPOTIFY_ACCOUNT_1/2), dieselbe Quelle wie der Prompt (#149).
ACCOUNT_NAMES = {"1": SPOTIFY_KONTEN[0], "2": SPOTIFY_KONTEN[1]}
EVOKE_NAME = os.getenv("SPOTIFY_DEVICE_NAME", "Evoke Play")
ANLAGE_NAME = os.getenv("SPOTIFY_DEVICE_NAME_ANLAGE", "Anlage")
KNOWN_ACCOUNTS = list(dict.fromkeys(ACCOUNT_NAMES.values()))

# Das Gerät ist ein Parameter des Werkzeugs, das Konto folgt der Person
# (telefon-ai#60). Wert des Schema-Felds `geraet` -> Connect-Gerätename, der
# in find_device per Namensteil gesucht wird. Vorgabe ist der Evoke; die
# Anlage nur, wenn der Anrufer sie ausdrücklich nennt.
STANDARD_GERAET = "evoke"
GERAETE: dict[str, str] = {"evoke": EVOKE_NAME, "anlage": ANLAGE_NAME}
# Gesprochene Form für die Ansagen: (Nominativ, Dativ nach "auf").
_GERAET_WORTE: dict[str, tuple[str, str]] = {
    "evoke": ("der Evoke", "dem Evoke"),
    "anlage": ("die Anlage", "der Anlage"),
}

# Gesprochene Namen des Lautsprechers. Seit dem Feld `geraet` gehören sie
# nicht mehr in den Suchbegriff — sagt das Modell trotzdem "auf dem Evoke
# Play" mit, fliegt das hier raus. Am 16.09.2026 wurde daraus
# `playlist="Evoke Play"` und Spotify spielte "That Two Can Play" (#65).
# Längere Namen zuerst, damit "Evoke Play" nicht als "Evoke" + Rest " Play"
# übrig bleibt.
DEVICE_NAMES: tuple[str, ...] = tuple(
    sorted({EVOKE_NAME, ANLAGE_NAME, "Evoke Play", "Evoke", "Anlage"}, key=len, reverse=True)
)


def _geraet_key(device_name: str) -> str:
    """Connect-Gerätename zurück auf den Schema-Wert, für die Ansagen."""
    for key, name in GERAETE.items():
        if name == device_name:
            return key
    return STANDARD_GERAET


def _auf_dem(geraet: str) -> str:
    return "auf " + _GERAET_WORTE.get(geraet, _GERAET_WORTE[STANDARD_GERAET])[1]


def _nicht_erreichbar(geraet: str, account: str) -> dict:
    """Das Gerät steht nicht in der Geräteliste dieses Kontos.

    Gemessen am 16.09.2026: /me/player/devices listet je Konto nur die
    Geräte, die dieses Konto zuletzt angesprochen hat, und eine Übernahme
    ist weder per Web-API noch per Home Assistant möglich. Zeroconf-addUser
    scheitert mit unserer App-ID (ERROR-LOGIN-FAILED, gemessen 18.09.2026).
    Entscheidung 18.09.2026 (telefon-ai#60): statt Übernahme sagt das Telefon
    dem Anrufer, was zu tun ist — einmal in der Spotify-App auf dem iPhone
    das Gerät wählen, danach kennt es das Konto. `grund` sagt dem Handler,
    dass hier kein Rückfall auf Titel- oder Künstlersuche mehr hilft — das
    Gerät fehlt, nicht der Treffer.
    """
    nominativ, dativ = _GERAET_WORTE.get(geraet, _GERAET_WORTE[STANDARD_GERAET])
    satzanfang = nominativ[0].upper() + nominativ[1:]  # capitalize() macht "Die anlage"
    return {
        "ok": False,
        "ack": (
            f"{satzanfang} kennt das Konto von {account.capitalize()} noch nicht. "
            f"Bitte verbinde dich einmalig mit deinem iPhone in der Spotify-App mit {dativ}, "
            "danach klappt es auch übers Telefon."
        ),
        "grund": "geraet",
    }


# Spotifys algorithmische Playlists sind seit der API-Änderung vom November
# 2024 für Drittanbieter-Apps nicht mehr erreichbar: /me/playlists listet sie
# nicht, /search liefert nur Kopien fremder Nutzer, und ein Shortcut auf die
# alte ID bekommt 404. Also gar nicht erst suchen (telefon-ai#66).
ALGORITHMIC_PLAYLISTS: tuple[str, ...] = ("daylist", "discover weekly", "release radar")

# Wie lange nach dem PUT gewartet wird, bevor /me/player gelesen wird, und
# wie oft. Spotify braucht einen Moment, bis der Player den neuen Kontext
# meldet. Tests setzen VERIFY_WAIT auf 0.
VERIFY_WAIT = 1.0
VERIFY_ATTEMPTS = 3


class SpotifyAuthError(Exception):
    """Die Anmeldung eines Kontos ist hin — Spotify selbst ist erreichbar.

    Eigener Typ, weil das etwas völlig anderes ist als ein Netzproblem und
    eine andere Antwort verlangt: hier hilft kein Abwarten, sondern nur
    ein neues `scripts/spotify_auth.py --account <name>`. Vorher fiel beides
    unter httpx.HTTPError und das Telefon sagte "Spotify antwortet gerade
    nicht", während Spotify tadellos antwortete.
    """

    def __init__(self, account: str, grund: str) -> None:
        self.account = account
        self.grund = grund
        super().__init__(f"{account}: {grund}")

    @property
    def ack(self) -> str:
        return (
            f"Die Spotify-Anmeldung für {self.account.capitalize()} ist abgelaufen. "
            f"Sie muss am Rechner neu eingerichtet werden."
        )


class SpotifyPlayRejected(Exception):
    """Spotify hat den PUT mit 204 quittiert, der Player zeigt aber etwas anderes.

    Gemessen am 16.09.2026 (telefon-ai#66): ein PUT auf eine tote Playlist-ID
    bekommt 204, und /me/player zeigt danach unverändert den alten Titel mit
    `context: null`. Der Statuscode sagt also nichts über Erfolg — nur die
    Messung danach. Eigener Typ, damit die Handler das von "Evoke nicht
    erreichbar" (find_device → None) unterscheiden können.
    """

    def __init__(self, uri: str, gesehen: str | None = None, geraet: str = STANDARD_GERAET) -> None:
        self.uri = uri
        self.gesehen = gesehen
        self.geraet = geraet
        super().__init__(f"{uri} nicht angenommen, Player zeigt {gesehen!r}")

    @property
    def ack(self) -> str:
        art = self.uri.split(":")[1] if self.uri.count(":") >= 2 else ""
        was, pronomen = {
            "playlist": ("die Playlist", "sie"),
            "artist": ("den Künstler", "ihn"),
            "album": ("das Album", "es"),
            "track": ("den Titel", "ihn"),
        }.get(art, ("das", "es"))
        geraet = _GERAET_WORTE.get(self.geraet, _GERAET_WORTE[STANDARD_GERAET])[0]
        return f"Spotify hat {was} nicht angenommen — {geraet} spielt {pronomen} nicht."


# Shortcuts für Playlists, die nicht in /me/playlists auftauchen (z.B. von
# anderen Nutzern geteilte Listen). Spotifys eigene algorithmische Listen
# (Daylist, Discover Weekly, Release Radar) gehören NICHT mehr hierher, siehe
# ALGORITHMIC_PLAYLISTS.
# Format: { "<konto>": { "<gesprochener-name>": "spotify:playlist:<id>" } }
SHORTCUTS_PATH = REPO / "playlists.json"

# Einmal pro Prozess werden die Shortcut-IDs gegen /playlists/{id} geprüft,
# damit ein toter Eintrag im Journal auffällt, statt am Telefon als "läuft
# jetzt" zu erscheinen (die Daylist-ID lag monatelang tot in playlists.json).
_shortcuts_geprueft = False
_pruef_task: asyncio.Task | None = None


def _load_shortcuts() -> dict[str, dict[str, str]]:
    if not SHORTCUTS_PATH.exists():
        return {}
    try:
        return json.loads(SHORTCUTS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("playlists.json kaputt: %s", exc)
        return {}


def _shortcut_uri(account: str, query: str) -> str | None:
    """Erstes Shortcut, dessen Key als Teilstring im query vorkommt (oder umgekehrt)."""
    by_account = _load_shortcuts().get(account, {})
    q = query.lower()
    for name, uri in by_account.items():
        if name.startswith("_"):
            continue  # "_doc" und Co. aus dem Beispiel
        n = name.lower()
        if n == q or n in q or q in n:
            return uri
    return None


async def _pruefe_shortcuts(client: httpx.AsyncClient) -> None:
    """Jede Shortcut-ID einmal per GET /playlists/{id} prüfen, tote Einträge loggen.

    Blockiert nichts und wirft nichts: ein Fehler beim Prüfen ist nur eine
    Logzeile, kein Grund, den Musikwunsch scheitern zu lassen.
    """
    for account, eintraege in _load_shortcuts().items():
        if account.startswith("_") or not isinstance(eintraege, dict):
            continue
        try:
            acct = _get_account(account)
        except FileNotFoundError:
            continue
        for name, uri in eintraege.items():
            if name.startswith("_"):
                continue
            playlist_id = str(uri).rsplit(":", 1)[-1]
            try:
                r = await client.get(
                    f"{API}/playlists/{playlist_id}",
                    headers=await acct._headers(client),
                    params={"fields": "id"},
                    timeout=15,
                )
            except (httpx.HTTPError, SpotifyAuthError) as exc:
                log.warning(
                    "playlists.json: Shortcut '%s' (%s) für %s nicht prüfbar: %s",
                    name,
                    uri,
                    account,
                    exc,
                )
                continue
            if r.status_code == 404:
                log.warning(
                    "playlists.json: Shortcut '%s' für %s zeigt auf eine tote Playlist (%s, 404)",
                    name,
                    account,
                    uri,
                )
            elif r.status_code >= 400:
                log.warning(
                    "playlists.json: Shortcut '%s' (%s) für %s nicht prüfbar: HTTP %d",
                    name,
                    uri,
                    account,
                    r.status_code,
                )


async def _pruefe_shortcuts_im_hintergrund() -> None:
    try:
        async with httpx.AsyncClient() as client:
            await _pruefe_shortcuts(client)
    except Exception as exc:  # noqa: BLE001 — Prüfung darf nie etwas reißen
        log.warning("Shortcut-Prüfung abgebrochen: %s", exc)


def _shortcuts_einmal_pruefen() -> None:
    """Beim ersten Musikwunsch des Prozesses die Prüfung anstoßen, ohne zu warten."""
    global _shortcuts_geprueft, _pruef_task
    if _shortcuts_geprueft:
        return
    _shortcuts_geprueft = True
    if not _load_shortcuts():
        return
    _pruef_task = asyncio.create_task(_pruefe_shortcuts_im_hintergrund())


_WORT = re.compile(r"\w+", re.UNICODE)


def _ohne_geraetenamen(query: str) -> str:
    """Bekannte Lautsprechernamen (samt "auf dem/der/den" davor) aus dem Suchbegriff entfernen."""
    text = query
    for name in DEVICE_NAMES:
        muster = re.compile(
            r"(?:\b(?:auf|am|im)\s+(?:dem|der|den|die|das)?\s*)?\b" + re.escape(name) + r"\b",
            re.IGNORECASE,
        )
        text = muster.sub(" ", text)
    return " ".join(text.split())


def _woerter(text: str, mindestlaenge: int = 3) -> set[str]:
    return {w.lower() for w in _WORT.findall(text) if len(w) >= mindestlaenge}


def _passt_zum_wunsch(query: str, *kandidaten: str) -> bool:
    """Enthält Titel oder Künstler den Suchbegriff erkennbar?

    Wortüberlappung, case-insensitiv, nur Wörter ab 3 Zeichen; mindestens
    ein Wort des Wunsches muss in einem Kandidaten vorkommen. Spotify liefert
    zu fast allem einen ersten Song — "Evoke Play" → "That Two Can Play" —
    darum reicht "es gibt einen Treffer" nicht als Beleg.
    """
    gesucht = _woerter(query)
    gefunden: set[str] = set()
    for k in kandidaten:
        gefunden |= _woerter(k)
    if gesucht & gefunden:
        return True
    return any(_kuerzel_im_wunsch(query, k) for k in kandidaten)


def _kuerzel_im_wunsch(query: str, kandidat: str) -> bool:
    """Kürzel wie "AC/DC" oder "T.N.T." bestehen nur aus Wörtern unter 3 Zeichen.

    Die Wortüberlappung sieht sie nicht — am 03.10.2026 fand Spotify zu "AC/DC"
    und "T.N.T." jeweils den richtigen Treffer, und er wurde als "nicht ähnlich
    genug" verworfen. Darum zusätzlich: der Kandidat, ohne Satzzeichen
    zusammengezogen ("tnt", "acdc"), muss einer zusammenhängenden Wortfolge des
    Wunsches entsprechen. Gleichheit, nicht Enthaltensein: "ab" soll nicht zu
    "Abba" passen.
    """
    zeichen = "".join(_WORT.findall(kandidat)).lower()
    if len(zeichen) < 2:
        return False
    teile = [w.lower() for w in _WORT.findall(query)]
    for anfang in range(len(teile)):
        folge = ""
        for ende in range(anfang, len(teile)):
            folge += teile[ende]
            if folge == zeichen:
                return True
            if len(folge) >= len(zeichen):
                break
    return False


# Wie viele Treffer die Titelsuche holt, um daraus den passenden zu waehlen.
TRACK_KANDIDATEN = 10


def _beste_wahl(query: str, items: list[dict]) -> dict:
    """Aus Spotifys Trefferliste den Titel waehlen, der zum Wunsch passt.

    Spotify sortiert nach eigener Beliebtheit, nicht nach dem Wunsch: zu
    "TNT AC/DC" kam am 03.10.2026 "Thunderstruck" auf Platz 1, T.N.T. stand
    weiter hinten. Der Titel zaehlt doppelt, der Kuenstler einfach; bei
    Gleichstand gewinnt die Reihenfolge von Spotify. Passt keiner, bleibt es
    beim ersten — die Aehnlichkeitspruefung danach entscheidet wie bisher.
    """
    bester, beste_punkte = items[0], 0
    for t in items:
        punkte = 0
        if _passt_zum_wunsch(query, t.get("name", "")):
            punkte += 2
        if _passt_zum_wunsch(query, *(a.get("name", "") for a in t.get("artists", []))):
            punkte += 1
        if punkte > beste_punkte:
            bester, beste_punkte = t, punkte
    return bester


class SpotifyAccount:
    def __init__(self, account: str) -> None:
        self.account = account
        self._refresh_token = self._load_refresh_token()
        self._access_token: str | None = None
        self._expires_at = 0.0

    @property
    def _token_path(self) -> Path:
        return REPO / f"token_{self.account}.json"

    def _load_refresh_token(self) -> str:
        if not self._token_path.exists():
            raise FileNotFoundError(
                f"{self._token_path} fehlt. Erst: python scripts/spotify_auth.py "
                f"--account {self.account}"
            )
        return json.loads(self._token_path.read_text())["refresh_token"]

    def _persist_refresh_token(self, new_token: str) -> None:
        """PKCE-Flow rotiert Refresh-Tokens — neues Token auf Disk schreiben."""
        data = json.loads(self._token_path.read_text())
        if data.get("refresh_token") == new_token:
            return
        data["refresh_token"] = new_token
        tmp = self._token_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)  # langlebiges Refresh-Token: nicht world-readable
        tmp.replace(self._token_path)
        log.debug("rotated refresh token persisted for %s", self.account)

    async def _access(self, client: httpx.AsyncClient) -> str:
        cached = self._access_token
        if cached and time.time() < self._expires_at - 30:
            return cached
        cid = os.environ["SPOTIFY_CLIENT_ID"]
        secret = os.environ["SPOTIFY_CLIENT_SECRET"]
        resp = await client.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            auth=(cid, secret),
            timeout=15,
        )
        if resp.status_code >= 400:
            # Spotify legt den Grund in den Body ("invalid_grant" = Token
            # zurueckgezogen/abgelaufen). Ohne diese Zeile stand im Journal
            # nur ein nacktes "400 Bad Request" ohne Konto und ohne Grund.
            try:
                body = resp.json()
                grund = body.get("error_description") or body.get("error") or resp.text
            except ValueError:
                grund = resp.text
            log.error(
                "Spotify-Token-Refresh für '%s' fehlgeschlagen (HTTP %d): %s",
                self.account,
                resp.status_code,
                grund,
            )
            if resp.status_code in (400, 401):
                raise SpotifyAuthError(self.account, str(grund))
            resp.raise_for_status()
        tok = resp.json()
        access: str = tok["access_token"]
        self._access_token = access
        self._expires_at = time.time() + int(tok.get("expires_in", 3600))
        new_refresh = tok.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            self._persist_refresh_token(new_refresh)
        log.debug("access token refreshed for %s", self.account)
        return access

    async def _headers(self, client: httpx.AsyncClient) -> dict:
        return {"Authorization": f"Bearer {await self._access(client)}"}

    async def find_device(self, client: httpx.AsyncClient, name: str = EVOKE_NAME) -> str | None:
        r = await client.get(
            f"{API}/me/player/devices", headers=await self._headers(client), timeout=15
        )
        r.raise_for_status()
        for d in r.json().get("devices", []):
            if name.lower() in d["name"].lower():
                return d["id"]
        # Je Konto stehen nur die Geräte in der Liste, die es zuletzt selbst
        # angesprochen hat (gemessen 16.09.2026, telefon-ai#60).
        log.warning(
            "[%s] device '%s' not found. Gerät an, im WLAN, von diesem Konto zuletzt benutzt?",
            self.account,
            name,
        )
        return None

    async def find_playlist(self, client: httpx.AsyncClient, query: str) -> str | None:
        # Spotify liefert hoechstens 50 pro Seite. Ohne Blaettern war ab der
        # 51. Playlist einfach nichts mehr auffindbar, ohne jede Fehlermeldung.
        url: str | None = f"{API}/me/playlists"
        params: dict | None = {"limit": 50}
        while url:
            r = await client.get(
                url, headers=await self._headers(client), params=params, timeout=15
            )
            r.raise_for_status()
            data = r.json()
            for pl in data.get("items", []):
                if query.lower() in pl["name"].lower():
                    return pl["uri"]
            url = data.get("next")
            params = None  # 'next' bringt die Parameter schon mit
        return None

    async def play_playlist(
        self,
        client: httpx.AsyncClient,
        playlist_uri: str,
        device_name: str = EVOKE_NAME,
    ) -> bool:
        return await self.play_context(client, playlist_uri, device_name)

    async def play_context(
        self,
        client: httpx.AsyncClient,
        context_uri: str,
        device_name: str = EVOKE_NAME,
    ) -> bool:
        """Beliebigen Spotify-Kontext starten (playlist/artist/album).

        Gibt True zurück, wenn /me/player danach diesen Kontext zeigt. False
        nur, wenn das Gerät fehlt oder der PUT selbst scheitert. Wirft
        SpotifyPlayRejected, wenn Spotify 204 sagt, aber nichts passiert ist.
        """
        device_id = await self.find_device(client, device_name)
        if device_id is None:
            return False
        r = await client.put(
            f"{API}/me/player/play",
            headers=await self._headers(client),
            params={"device_id": device_id},
            json={"context_uri": context_uri},
            timeout=15,
        )
        if r.status_code not in (200, 202, 204):
            log.error("play failed: %s %s", r.status_code, r.text)
            return False
        await self._verify_playback(client, context_uri=context_uri, device_name=device_name)
        log.info("[%s] playing %s on %s", self.account, context_uri, device_name)
        return True

    async def _verify_playback(
        self,
        client: httpx.AsyncClient,
        context_uri: str | None = None,
        uris: list[str] | None = None,
        device_name: str = EVOKE_NAME,
    ) -> None:
        """Nach dem PUT nachmessen, was der Player wirklich spielt.

        Regel "LLM urteilt, Code rechnet": Erfolg kommt aus /me/player, nicht
        aus dem Statuscode. Spotify quittiert auch einen toten Kontext mit
        204 (telefon-ai#66). Kurz warten, dann bis zu VERIFY_ATTEMPTS Mal
        lesen — der Player meldet den neuen Kontext nicht immer sofort.
        """
        gesehen: str | None = None
        for _ in range(VERIFY_ATTEMPTS):
            await asyncio.sleep(VERIFY_WAIT)
            r = await client.get(
                f"{API}/me/player", headers=await self._headers(client), timeout=15
            )
            if r.status_code == 204:
                gesehen = None  # kein aktiver Player
                continue
            r.raise_for_status()
            data = r.json()
            ctx = (data.get("context") or {}).get("uri")
            item = (data.get("item") or {}).get("uri")
            if context_uri is not None and ctx == context_uri:
                return
            if uris is not None and item in uris:
                return
            gesehen = f"context={ctx} item={item}"
        gewollt = context_uri or uris
        log.error(
            "[%s] Spotify hat %s nicht angenommen, /me/player zeigt %s",
            self.account,
            gewollt,
            gesehen,
        )
        raise SpotifyPlayRejected(
            context_uri or (uris or [""])[0], gesehen, geraet=_geraet_key(device_name)
        )

    async def currently_playing(self, client: httpx.AsyncClient) -> dict | None:
        """Aktuell spielenden Track zurückgeben oder None wenn nichts läuft."""
        r = await client.get(
            f"{API}/me/player/currently-playing",
            headers=await self._headers(client),
            timeout=15,
        )
        if r.status_code == 204:
            return None
        r.raise_for_status()
        data = r.json()
        item = data.get("item")
        if not item:
            return None
        return {
            "title": item.get("name"),
            "artists": [a.get("name") for a in item.get("artists", [])],
            "album": (item.get("album") or {}).get("name"),
            "is_playing": data.get("is_playing", False),
        }

    async def play_uris(
        self,
        client: httpx.AsyncClient,
        uris: list[str],
        device_name: str = EVOKE_NAME,
    ) -> bool:
        """Einzelne Titel starten.

        Ein Track ist kein Kontext — mit context_uri antwortet Spotify 400.
        Dafür gibt es das Feld 'uris'.
        """
        device_id = await self.find_device(client, device_name)
        if device_id is None:
            return False
        r = await client.put(
            f"{API}/me/player/play",
            headers=await self._headers(client),
            params={"device_id": device_id},
            json={"uris": uris},
            timeout=15,
        )
        if r.status_code not in (200, 202, 204):
            log.error("play failed: %s %s", r.status_code, r.text)
            return False
        await self._verify_playback(client, uris=uris, device_name=device_name)
        log.info("[%s] playing %s on %s", self.account, uris, device_name)
        return True

    async def find_track(self, client: httpx.AsyncClient, query: str) -> dict | None:
        """Einzelnen Titel suchen. Gibt uri, name, artists und vorlesbare Bezeichnung zurück."""
        r = await client.get(
            f"{API}/search",
            headers=await self._headers(client),
            params={"q": query, "type": "track", "limit": TRACK_KANDIDATEN},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("tracks", {}).get("items", [])
        if not items:
            return None
        t = _beste_wahl(query, items)
        artist_names = [a["name"] for a in t.get("artists", [])]
        artists = ", ".join(artist_names)
        return {
            "uri": t["uri"],
            "name": t["name"],
            "artists": artist_names,
            "label": f"'{t['name']}' von {artists}" if artists else f"'{t['name']}'",
        }

    async def find_artist(self, client: httpx.AsyncClient, query: str) -> dict | None:
        """Künstler suchen. Gibt uri und name zurück — der Name ist für die
        Ähnlichkeitsprüfung nötig, Spotify liefert zu fast allem irgendwen."""
        r = await client.get(
            f"{API}/search",
            headers=await self._headers(client),
            params={"q": query, "type": "artist", "limit": 1},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("artists", {}).get("items", [])
        if not items:
            return None
        return {"uri": items[0]["uri"], "name": items[0]["name"]}


_accounts: dict[str, SpotifyAccount] = {}


def _get_account(name: str) -> SpotifyAccount:
    if name not in _accounts:
        _accounts[name] = SpotifyAccount(name)
    return _accounts[name]


def _match_account_name(spoken: str) -> str | None:
    if not spoken:
        return None
    s = spoken.strip().lower()
    for name in KNOWN_ACCOUNTS:
        if s == name.lower() or s in name.lower() or name.lower() in s:
            return name
    return None


async def handle_spotify_now_playing(args: dict) -> dict:
    """Was läuft gerade? Fragt jedes eingerichtete Konto und liefert das
    erste 'is_playing=true', sonst den ersten gefundenen Track."""
    wer = (args.get("wer") or "").strip()
    matched = _match_account_name(wer)
    candidates = [matched] if matched else KNOWN_ACCOUNTS
    fallback: tuple[str, dict] | None = None
    abgemeldet: list[str] = []
    async with httpx.AsyncClient() as client:
        for name in candidates:
            try:
                acct = _get_account(name)
            except FileNotFoundError:
                continue
            try:
                info = await acct.currently_playing(client)
            except SpotifyAuthError:
                abgemeldet.append(name)
                continue
            except httpx.HTTPError as exc:
                log.warning("currently-playing failed for %s: %s", name, exc)
                continue
            if info is None:
                continue
            if info.get("is_playing"):
                return _format_now_playing(info)
            if fallback is None:
                fallback = (name, info)
    if fallback is not None:
        return _format_now_playing(fallback[1], playing=False)
    if abgemeldet:
        wessen = " und ".join(n.capitalize() for n in abgemeldet)
        return {
            "ok": False,
            "ack": (
                f"Die Spotify-Anmeldung für {wessen} ist abgelaufen — ich kann "
                f"gerade nicht nachsehen."
            ),
        }
    return {"ok": False, "ack": "Gerade läuft nichts."}


def _format_now_playing(info: dict, playing: bool = True) -> dict:
    title = info.get("title") or "ein Stück"
    artists = info.get("artists") or []
    artist_str = " und ".join(artists) if artists else "unbekannter Künstler"
    verb = "läuft gerade" if playing else "war zuletzt"
    return {
        "ok": True,
        "ack": f"Das {verb}: '{title}' von {artist_str}.",
        "title": title,
        "artists": artists,
    }


async def _play_artist(query: str, wer: str, geraet: str = STANDARD_GERAET) -> dict:
    """Künstler suchen + Top-Tracks starten. Konto egal für die Suche,
    aber für den Playback brauchen wir ein eingerichtetes Konto."""
    matched = _match_account_name(wer)
    candidates = [matched] if matched else KNOWN_ACCOUNTS
    abgemeldet: list[str] = []
    async with httpx.AsyncClient() as client:
        for name in candidates:
            try:
                acct = _get_account(name)
            except FileNotFoundError:
                continue
            try:
                treffer = await acct.find_artist(client, query)
            except SpotifyAuthError as exc:
                # Ohne Namensnennung laufen wir ueber alle Konten — ein totes
                # ueberspringen, statt hier abzubrechen.
                if not matched:
                    abgemeldet.append(name)
                    continue
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except httpx.HTTPError as exc:
                return {"ok": False, "ack": "Spotify antwortet gerade nicht.", "error": str(exc)}
            if treffer is None:
                return {"ok": False, "ack": f"Ich finde keinen Künstler '{query}'."}
            if not _passt_zum_wunsch(query, treffer["name"]):
                log.info("artist search '%s' returned '%s' — nicht ähnlich genug", query, treffer)
                return {
                    "ok": False,
                    "ack": (
                        f"Zu '{query}' finde ich nur den Künstler '{treffer['name']}' — "
                        f"meintest du den?"
                    ),
                }
            try:
                ok = await acct.play_context(client, treffer["uri"], device_name=GERAETE[geraet])
            except SpotifyAuthError as exc:
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except SpotifyPlayRejected as exc:
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except httpx.HTTPError as exc:
                return {"ok": False, "ack": "Spotify antwortet gerade nicht.", "error": str(exc)}
            if ok:
                wer_ack = f" für {name.capitalize()}" if matched else ""
                return {
                    "ok": True,
                    "ack": f"Spiele {treffer['name']}{wer_ack} {_auf_dem(geraet)}.",
                }
            return _nicht_erreichbar(geraet, name)
    if abgemeldet:
        wessen = " und ".join(n.capitalize() for n in abgemeldet)
        return {
            "ok": False,
            "ack": (
                f"Die Spotify-Anmeldung für {wessen} ist abgelaufen und muss am "
                f"Rechner neu eingerichtet werden."
            ),
        }
    return {"ok": False, "ack": "Kein Konto eingerichtet."}


async def _play_track(query: str, wer: str, geraet: str = STANDARD_GERAET) -> dict:
    """Einzelnen Titel suchen und starten.

    Die Suche laeuft ueber Spotifys Katalog, ist also kontounabhaengig — das
    Konto braucht es nur fuers Abspielen. Deshalb reicht ein funktionierendes
    Konto, auch wenn das andere abgemeldet ist.
    """
    matched = _match_account_name(wer)
    candidates = [matched] if matched else KNOWN_ACCOUNTS
    abgemeldet: list[str] = []
    async with httpx.AsyncClient() as client:
        for name in candidates:
            try:
                acct = _get_account(name)
            except FileNotFoundError:
                continue
            try:
                treffer = await acct.find_track(client, query)
            except SpotifyAuthError as exc:
                if not matched:
                    abgemeldet.append(name)
                    continue
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except httpx.HTTPError as exc:
                return {"ok": False, "ack": "Spotify antwortet gerade nicht.", "error": str(exc)}
            if treffer is None:
                return {"ok": False, "ack": f"Ich finde kein Stück '{query}'."}
            # Spotify liefert zu fast allem einen ersten Song. Ohne diese
            # Pruefung lief zu "Evoke Play" der Titel "That Two Can Play".
            kandidaten = [treffer.get("name") or treffer.get("label", "")]
            kandidaten += treffer.get("artists", [])
            if not _passt_zum_wunsch(query, *kandidaten):
                log.info("track search '%s' returned %s — nicht ähnlich genug", query, treffer)
                return {
                    "ok": False,
                    "ack": f"Zu '{query}' finde ich nur {treffer['label']} — meintest du das?",
                }
            try:
                ok = await acct.play_uris(client, [treffer["uri"]], device_name=GERAETE[geraet])
            except SpotifyAuthError as exc:
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except SpotifyPlayRejected as exc:
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except httpx.HTTPError as exc:
                return {"ok": False, "ack": "Spotify antwortet gerade nicht.", "error": str(exc)}
            if ok:
                return {"ok": True, "ack": f"Ich spiele {treffer['label']} {_auf_dem(geraet)}."}
            return _nicht_erreichbar(geraet, name)
    if abgemeldet:
        wessen = " und ".join(n.capitalize() for n in abgemeldet)
        return {
            "ok": False,
            "ack": (
                f"Die Spotify-Anmeldung für {wessen} ist abgelaufen und muss am "
                f"Rechner neu eingerichtet werden."
            ),
        }
    return {"ok": False, "ack": "Kein Konto eingerichtet."}


async def handle_spotify_play(args: dict) -> dict:
    """Realtime-Tool-Handler. Gibt {ok, ack} zurück (ack = vorlesbarer Text)."""
    roh = (args.get("playlist") or args.get("query") or "").strip()
    wer = (args.get("wer") or "").strip()
    kind = (args.get("kind") or "playlist").strip().lower()
    geraet = (args.get("geraet") or STANDARD_GERAET).strip().lower()

    if not roh:
        return {"ok": False, "ack": "Was soll ich spielen?"}
    if geraet not in GERAETE:
        # Das Schema erlaubt nur evoke|anlage. Alles andere ist ein Fehler
        # des Aufrufers — nicht stillschweigend auf den Evoke umbiegen.
        return {"ok": False, "ack": f"Ein Gerät '{geraet}' kenne ich nicht."}

    # "auf dem Evoke Play" ist der Lautsprecher, nicht der Name (telefon-ai#65).
    # Das Gerät kommt ausschließlich aus `geraet`, nie aus dem Suchbegriff.
    wanted = _ohne_geraetenamen(roh)
    if not wanted:
        return {
            "ok": False,
            "ack": "Ich habe nur den Lautsprecher verstanden — was soll ich darauf spielen?",
        }
    if wanted != roh:
        log.info("spotify_play: Gerätename entfernt: %r -> %r", roh, wanted)

    # Daylist, Discover Weekly, Release Radar: fuer Drittanbieter seit Nov
    # 2024 unerreichbar — nicht suchen, nicht raten (telefon-ai#66).
    if any(name in wanted.lower() for name in ALGORITHMIC_PLAYLISTS):
        return {
            "ok": False,
            "ack": (
                f"Die Playlist '{wanted}' kann Spotify nur in der eigenen App starten, "
                f"nicht {_auf_dem(geraet)}. Soll ich stattdessen etwas anderes spielen?"
            ),
        }

    _shortcuts_einmal_pruefen()

    if kind == "artist":
        return await _play_artist(wanted, wer, geraet)
    if kind == "track":
        return await _play_track(wanted, wer, geraet)

    async with httpx.AsyncClient() as client:
        matched = _match_account_name(wer)
        if matched:
            try:
                acct = _get_account(matched)
            except FileNotFoundError as exc:
                return {
                    "ok": False,
                    "ack": f"Konto {matched} ist nicht eingerichtet.",
                    "error": str(exc),
                }
            uri = _shortcut_uri(matched, wanted)
            if uri is None:
                try:
                    uri = await acct.find_playlist(client, wanted)
                except SpotifyAuthError as exc:
                    return {"ok": False, "ack": exc.ack, "error": str(exc)}
                except httpx.HTTPError as exc:
                    return {
                        "ok": False,
                        "ack": "Spotify antwortet gerade nicht.",
                        "error": str(exc),
                    }
            if uri is None:
                # Auch hier: erst Stueck, dann Kuenstler, bevor wir aufgeben.
                # Fehlt dabei das Gerät, ist weitersuchen sinnlos.
                for versuch in (_play_track, _play_artist):
                    fallback = await versuch(wanted, wer, geraet)
                    if fallback.get("ok") or fallback.get("grund") == "geraet":
                        return fallback
                return {
                    "ok": False,
                    "ack": (f"{matched.capitalize()}, ich finde nichts zu '{wanted}'."),
                }
            try:
                ok = await acct.play_playlist(client, uri, device_name=GERAETE[geraet])
            except SpotifyAuthError as exc:
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except SpotifyPlayRejected as exc:
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except httpx.HTTPError as exc:
                return {"ok": False, "ack": "Spotify antwortet gerade nicht.", "error": str(exc)}
            if ok:
                return {
                    "ok": True,
                    "ack": f"Spiele {wanted} für {matched.capitalize()} {_auf_dem(geraet)}.",
                }
            return _nicht_erreichbar(geraet, matched)

        # Stufe 3: Playlist in allen verfügbaren Konten suchen
        # (Shortcuts zuerst, dann /me/playlists)
        hits: list[tuple[str, str]] = []
        abgemeldet: list[str] = []
        durchsucht: list[str] = []
        for name in KNOWN_ACCOUNTS:
            try:
                acct = _get_account(name)
            except FileNotFoundError:
                continue
            uri = _shortcut_uri(name, wanted)
            if uri is None:
                try:
                    uri = await acct.find_playlist(client, wanted)
                except SpotifyAuthError:
                    # Ein totes Konto darf die Suche im anderen nicht abwuergen.
                    abgemeldet.append(name)
                    continue
                except httpx.HTTPError as exc:
                    log.warning("playlist lookup failed for %s: %s", name, exc)
                    continue
            durchsucht.append(name)
            if uri:
                hits.append((name, uri))

        if len(hits) == 1:
            name, uri = hits[0]
            try:
                ok = await _get_account(name).play_playlist(
                    client, uri, device_name=GERAETE[geraet]
                )
            except SpotifyAuthError as exc:
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except SpotifyPlayRejected as exc:
                return {"ok": False, "ack": exc.ack, "error": str(exc)}
            except httpx.HTTPError as exc:
                return {"ok": False, "ack": "Spotify antwortet gerade nicht.", "error": str(exc)}
            if ok:
                return {"ok": True, "ack": f"Spiele {wanted} {_auf_dem(geraet)}."}
            return _nicht_erreichbar(geraet, name)
        if len(hits) > 1:
            wessen = " oder ".join(n.capitalize() for n, _ in hits)
            return {
                "ok": False,
                "ack": (f"Die Playlist '{wanted}' gibt es bei {wessen}. Wessen soll ich nehmen?"),
            }
    # Keine Playlist dieses Namens. Der haeufigste Grund ist nicht ein
    # Tippfehler, sondern dass gar keine Playlist gemeint war: "spiel mir
    # ein schoenes Hamburger Lied" ist ein Stueck, kein Listenname. Das
    # Modell waehlt die Art erfahrungsgemaess falsch, also faellt der
    # Handler selbst zurueck, statt sich darauf zu verlassen.
    fallback = await _play_track(wanted, wer, geraet)
    if fallback.get("ok") or fallback.get("grund") == "geraet":
        return fallback
    fallback = await _play_artist(wanted, wer, geraet)
    if fallback.get("ok") or fallback.get("grund") == "geraet":
        return fallback

    # Die abgelaufene Anmeldung nur dann als Grund nennen, wenn wirklich
    # KEIN Konto durchsucht werden konnte. Vorher ueberschrieb diese
    # Meldung jeden erfolglosen Fund und liess Spotify dreimal
    # hintereinander kaputt aussehen, obwohl es dreimal etwas anderes war.
    if abgemeldet and not durchsucht:
        wessen = " und ".join(n.capitalize() for n in abgemeldet)
        return {
            "ok": False,
            "ack": (
                f"Die Spotify-Anmeldung für {wessen} ist abgelaufen und muss "
                f"am Rechner neu eingerichtet werden."
            ),
        }
    return {"ok": False, "ack": f"Ich finde nichts zu '{wanted}'."}
