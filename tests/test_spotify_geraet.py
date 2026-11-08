"""spotify_play: das Gerät ist ein Parameter des Werkzeugs, das Konto folgt der Person.

Anlass (telefon-ai#60, Schritt 1): Es gibt zwei Connect-Lautsprecher im Haus,
den Evoke Play und die Anlage. Bis hierher kannte das Schema nur den Evoke,
und „auf der Anlage" landete im Suchbegriff (#65). Jetzt bekommt spotify_play
ein Feld `geraet` mit `evoke` (Vorgabe) und `anlage`. Der Gerätename kommt
ausschließlich aus diesem Feld.

Gemessen am 16.09.2026: `/me/player/devices` listet je Konto nur die Geräte,
die dieses Konto zuletzt angesprochen hat. Fehlt das gewünschte Gerät in der
Liste, ist keine Übernahme möglich — weder per Web-API noch per Home
Assistant. Also ehrlich absagen, nicht auf die Suche zurückfallen.
"""

import httpx
import pytest

from kitelefon.agents import spotify
from kitelefon.agents.spotify import SpotifyAccount, handle_spotify_play
from kitelefon.tools import ALL_TOOL_SCHEMAS, SPOTIFY_PLAY_TOOL_SCHEMA
from kitelefon.werkzeugdienst import FERN_WERKZEUGE


@pytest.fixture(autouse=True)
def konten(monkeypatch, tmp_path):
    monkeypatch.setattr(spotify, "KNOWN_ACCOUNTS", ["otto", "paula"])
    monkeypatch.setattr(spotify, "_accounts", {})
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(spotify, "REPO", tmp_path)
    monkeypatch.setattr(spotify, "SHORTCUTS_PATH", tmp_path / "playlists.json")
    monkeypatch.setattr(spotify, "_shortcuts_geprueft", True)
    monkeypatch.setattr(spotify, "VERIFY_WAIT", 0.0)
    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    for name in ("otto", "paula"):
        (tmp_path / f"token_{name}.json").write_text('{"refresh_token": "r"}')
    yield


async def _tok() -> str:
    return "tok"


def _merke_geraet(monkeypatch, gesehen: list[str]):
    """play_context/play_uris so ersetzen, dass nur der Gerätename ankommt."""

    # play_playlist reicht device_name positional an play_context weiter,
    # darum hier dieselbe Stellung wie im Original.
    async def play(self, client, was, device_name=spotify.EVOKE_NAME):
        gesehen.append(device_name)
        return True

    monkeypatch.setattr(SpotifyAccount, "play_context", play)
    monkeypatch.setattr(SpotifyAccount, "play_uris", play)


async def _mixtape(self, client, query):
    return "spotify:playlist:mix" if self.account == "otto" else None


# --- Schema --------------------------------------------------------------


def test_schema_hat_geraet_mit_vorgabe_evoke():
    geraet = SPOTIFY_PLAY_TOOL_SCHEMA["parameters"]["properties"]["geraet"]
    assert geraet["enum"] == ["evoke", "anlage"]
    assert "geraet" not in SPOTIFY_PLAY_TOOL_SCHEMA["parameters"]["required"]
    # rundfunk/Elektra holt sich dasselbe Schema über den Werkzeugdienst.
    assert "spotify_play" in FERN_WERKZEUGE
    fern = next(s for s in ALL_TOOL_SCHEMAS if s["name"] == "spotify_play")
    assert "geraet" in fern["parameters"]["properties"]


# --- (a) Vorgabe evoke ---------------------------------------------------


@pytest.mark.asyncio
async def test_ohne_geraet_spielt_der_evoke(monkeypatch):
    gesehen: list[str] = []
    monkeypatch.setattr(SpotifyAccount, "find_playlist", _mixtape)
    _merke_geraet(monkeypatch, gesehen)

    ergebnis = await handle_spotify_play({"playlist": "Mixtape", "wer": "Otto"})

    assert ergebnis["ok"] is True
    assert gesehen == [spotify.EVOKE_NAME]
    assert "Evoke" in ergebnis["ack"] and "Anlage" not in ergebnis["ack"]


# --- (b) geraet=anlage ---------------------------------------------------


@pytest.mark.asyncio
async def test_geraet_anlage_waehlt_den_anlage_namen(monkeypatch):
    monkeypatch.setattr(spotify, "ANLAGE_NAME", "Wohnzimmer-Anlage")
    monkeypatch.setitem(spotify.GERAETE, "anlage", "Wohnzimmer-Anlage")
    gesehen: list[str] = []
    monkeypatch.setattr(SpotifyAccount, "find_playlist", _mixtape)
    _merke_geraet(monkeypatch, gesehen)

    ergebnis = await handle_spotify_play({"playlist": "Mixtape", "wer": "Otto", "geraet": "anlage"})

    assert ergebnis["ok"] is True
    assert gesehen == ["Wohnzimmer-Anlage"]
    assert "Anlage" in ergebnis["ack"] and "Evoke" not in ergebnis["ack"]


@pytest.mark.parametrize("kind", ["track", "artist"])
@pytest.mark.asyncio
async def test_geraet_gilt_auch_fuer_titel_und_kuenstler(monkeypatch, kind):
    gesehen: list[str] = []

    async def find_track(self, client, query):
        return {"uri": "spotify:track:q", "name": "Queen", "artists": ["Queen"], "label": "'Queen'"}

    async def find_artist(self, client, query):
        return {"uri": "spotify:artist:q", "name": "Queen"}

    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "find_artist", find_artist)
    _merke_geraet(monkeypatch, gesehen)

    ergebnis = await handle_spotify_play({"playlist": "Queen", "kind": kind, "geraet": "anlage"})

    assert ergebnis["ok"] is True
    assert gesehen == [spotify.ANLAGE_NAME]
    assert "Anlage" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_geraetename_bleibt_aus_dem_suchbegriff_draussen(monkeypatch):
    """Sagt das Modell trotzdem „Mixtape auf der Anlage", darf die Anlage
    weder gesucht noch der Lautsprecher aus dem Text geraten werden."""
    gesucht: list[str] = []
    gesehen: list[str] = []

    async def find_playlist(self, client, query):
        gesucht.append(query)
        return "spotify:playlist:mix"

    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    _merke_geraet(monkeypatch, gesehen)

    ergebnis = await handle_spotify_play({"playlist": "Mixtape auf der Anlage", "wer": "Otto"})

    assert ergebnis["ok"] is True
    assert gesucht == ["Mixtape"]
    assert gesehen == [spotify.EVOKE_NAME]  # Gerät kommt nur aus `geraet`


@pytest.mark.asyncio
async def test_unbekanntes_geraet_wird_abgelehnt(monkeypatch):
    async def kein_netz(self, client, *a, **kw):
        raise AssertionError("kein HTTP erwartet")

    for methode in ("find_playlist", "find_track", "find_artist", "play_context", "play_uris"):
        monkeypatch.setattr(SpotifyAccount, methode, kein_netz)

    ergebnis = await handle_spotify_play({"playlist": "Mixtape", "geraet": "kueche"})

    assert ergebnis["ok"] is False
    assert "kueche" in ergebnis["ack"]


# --- (c) Gerät nicht in der Liste des Kontos -----------------------------


def _geraeteliste(monkeypatch, geraete: list[str]) -> list[str]:
    """Jeder httpx.AsyncClient im Handler antwortet nur auf /me/player/devices.

    Der Handler und _play_track öffnen je einen eigenen Client, also pro
    Aufruf eine frische Instanz über denselben Transport. Zurück kommt die
    Liste aller gesehenen Anfragen."""
    anfragen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        anfragen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(
                200, json={"devices": [{"id": f"id-{g}", "name": g} for g in geraete]}
            )
        raise AssertionError(f"unerwartet: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    echter_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: echter_client(transport=transport))
    return anfragen


@pytest.mark.asyncio
async def test_geraet_fehlt_in_der_liste_sagt_ab_ohne_uebernahme(monkeypatch):
    """Paulas Konto sieht nur den Evoke. Wunsch: Anlage. Kein PUT, kein
    Transfer, kein Rückfall auf Titel- oder Künstlersuche — nur die Absage."""
    anfragen = _geraeteliste(monkeypatch, ["Evoke Play"])
    gesucht: list[str] = []

    async def find_playlist(self, client, query):
        return "spotify:playlist:mix"

    async def suche(self, client, query):
        gesucht.append(query)
        return None

    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "find_track", suche)
    monkeypatch.setattr(SpotifyAccount, "find_artist", suche)

    ergebnis = await handle_spotify_play(
        {"playlist": "Mixtape", "wer": "Paula", "geraet": "anlage"}
    )

    assert ergebnis["ok"] is False
    assert "Anlage" in ergebnis["ack"] and "Paula" in ergebnis["ack"]
    assert "iPhone" in ergebnis["ack"]
    assert anfragen == ["GET /v1/me/player/devices"]
    assert gesucht == []


@pytest.mark.asyncio
async def test_geraet_fehlt_beim_titel_kein_rueckfall_auf_kuenstler(monkeypatch):
    """Playlist nicht gefunden → Titel gefunden → Gerät fehlt. Vorher lief
    danach noch die Künstlersuche und am Ende hieß es „ich finde nichts"."""
    anfragen = _geraeteliste(monkeypatch, ["Evoke Play"])
    kuenstler_gesucht: list[str] = []

    async def find_playlist(self, client, query):
        return None

    async def find_track(self, client, query):
        return {"uri": "spotify:track:k", "name": "Kesariya", "artists": [], "label": "'Kesariya'"}

    async def find_artist(self, client, query):
        kuenstler_gesucht.append(query)
        return None

    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "find_artist", find_artist)

    ergebnis = await handle_spotify_play(
        {"playlist": "Kesariya", "wer": "Otto", "geraet": "anlage"}
    )

    assert ergebnis["ok"] is False
    assert "Anlage" in ergebnis["ack"] and "Otto" in ergebnis["ack"]
    assert anfragen == ["GET /v1/me/player/devices"]
    assert kuenstler_gesucht == []


@pytest.mark.asyncio
async def test_evoke_fehlt_nennt_evoke_und_konto(monkeypatch):
    _geraeteliste(monkeypatch, [])
    monkeypatch.setattr(SpotifyAccount, "find_playlist", _mixtape)

    ergebnis = await handle_spotify_play({"playlist": "Mixtape"})

    assert ergebnis["ok"] is False
    assert "Evoke" in ergebnis["ack"] and "Otto" in ergebnis["ack"]


# --- Ansagen passen zum Gerät --------------------------------------------


@pytest.mark.asyncio
async def test_abgelehnter_kontext_nennt_das_gewaehlte_geraet():
    """PUT 204, Player zeigt etwas anderes: die Absage nennt die Anlage, nicht den Evoke."""
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": [{"id": "a1", "name": "Anlage"}]})
        if request.method == "PUT":
            puts.append(request.url.params["device_id"])
            return httpx.Response(204)
        return httpx.Response(200, json={"context": None, "item": {"uri": "spotify:track:alt"}})

    acct = SpotifyAccount("otto")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(spotify.SpotifyPlayRejected) as exc:
            await acct.play_context(client, "spotify:playlist:x", device_name="Anlage")

    assert puts == ["a1"]
    assert "Anlage" in exc.value.ack and "Evoke" not in exc.value.ack
