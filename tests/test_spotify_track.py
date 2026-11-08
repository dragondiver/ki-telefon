"""Einzelne Titel abspielen — und die Playlist-Suche ueber Seitengrenzen.

Anlass: Am 2026-08-03 wollte jemand am Telefon "einen populaeren indischen
Song". spotify_play kannte nur playlist und artist, also wurde daraus eine
Playlist-Suche, die nichts fand — und die Fehlermeldung schob es auf die
abgelaufene Anmeldung des zweiten Kontos. Der Wunsch war nie das Problem.
"""

import httpx
import pytest

from kitelefon.agents import spotify
from kitelefon.agents.spotify import (
    SpotifyAccount,
    SpotifyAuthError,
    handle_spotify_play,
)


@pytest.fixture(autouse=True)
def konten(monkeypatch, tmp_path):
    monkeypatch.setattr(spotify, "KNOWN_ACCOUNTS", ["otto", "paula"])
    monkeypatch.setattr(spotify, "_accounts", {})
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(spotify, "REPO", tmp_path)
    monkeypatch.setattr(spotify, "SHORTCUTS_PATH", tmp_path / "playlists.json")
    monkeypatch.setattr(spotify, "VERIFY_WAIT", 0.0)
    for name in ("otto", "paula"):
        (tmp_path / f"token_{name}.json").write_text('{"refresh_token": "r"}')
    yield


# --- find_track -------------------------------------------------------


@pytest.mark.asyncio
async def test_find_track_liefert_uri_und_vorlesbare_bezeichnung():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["type"] == "track"
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "uri": "spotify:track:abc",
                            "name": "Kesariya",
                            "artists": [{"name": "Arijit Singh"}],
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        acct = SpotifyAccount("otto")
        acct._access_token, acct._expires_at = "tok", 9e9
        treffer = await acct.find_track(c, "Kesariya")

    assert treffer is not None
    assert treffer["uri"] == "spotify:track:abc"
    assert treffer["label"] == "'Kesariya' von Arijit Singh"


@pytest.mark.asyncio
async def test_find_track_ohne_treffer_ist_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tracks": {"items": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        acct = SpotifyAccount("otto")
        acct._access_token, acct._expires_at = "tok", 9e9
        assert await acct.find_track(c, "gibtsnicht") is None


@pytest.mark.asyncio
async def test_track_wird_ueber_uris_gestartet_nicht_als_kontext():
    """Ein Track ist kein Kontext — mit context_uri antwortet Spotify 400."""
    gesehen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": [{"id": "dev1", "name": "Evoke Play"}]})
        if request.method == "PUT":
            import json as _json

            gesehen.update(_json.loads(request.content))
            return httpx.Response(204)
        if request.method == "GET" and request.url.path.endswith("/me/player"):
            # Seit telefon-ai#66 wird nachgemessen, ob der Titel wirklich laeuft.
            return httpx.Response(200, json={"item": {"uri": "spotify:track:abc"}})
        raise AssertionError(f"unerwartet: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        acct = SpotifyAccount("otto")
        acct._access_token, acct._expires_at = "tok", 9e9
        assert await acct.play_uris(c, ["spotify:track:abc"]) is True

    assert gesehen == {"uris": ["spotify:track:abc"]}
    assert "context_uri" not in gesehen


# --- Handler ----------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_spielt_einzelnen_titel(monkeypatch):
    gespielt = []

    async def find_track(self, client, query):
        return {"uri": "spotify:track:abc", "label": "'Kesariya' von Arijit Singh"}

    async def play_uris(self, client, uris, device_name="Evoke Play"):
        gespielt.append(uris)
        return True

    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "play_uris", play_uris)

    ergebnis = await handle_spotify_play({"playlist": "Kesariya", "kind": "track", "wer": "Otto"})

    assert ergebnis["ok"] is True
    assert "Kesariya" in ergebnis["ack"]
    assert gespielt == [["spotify:track:abc"]]


@pytest.mark.asyncio
async def test_totes_konto_verhindert_den_titel_nicht(monkeypatch):
    """Die Titelsuche laeuft ueber Spotifys Katalog, nicht ueber das Konto —
    ein abgemeldetes Konto darf den Wunsch also nicht scheitern lassen."""

    async def find_track(self, client, query):
        if self.account == "paula":
            raise SpotifyAuthError("paula", "invalid_grant")
        return {"uri": "spotify:track:abc", "label": "'Kesariya'"}

    async def play_uris(self, client, uris, device_name="Evoke Play"):
        return True

    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "play_uris", play_uris)

    ergebnis = await handle_spotify_play({"playlist": "Kesariya", "kind": "track"})

    assert ergebnis["ok"] is True


@pytest.mark.asyncio
async def test_unbekanntes_kind_bleibt_playlist(monkeypatch):
    """Ohne kind muss weiterhin die Playlist-Suche laufen."""
    gefragt = []

    async def find_playlist(self, client, query):
        gefragt.append(query)
        return None

    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)

    await handle_spotify_play({"playlist": "Rockklassiker"})

    assert gefragt  # find_playlist wurde benutzt, nicht find_track


# --- Blaettern --------------------------------------------------------


@pytest.mark.asyncio
async def test_playlist_suche_blaettert_ueber_die_erste_seite_hinaus():
    """Ab der 51. Playlist war vorher nichts mehr auffindbar — lautlos."""
    seiten = {
        "/v1/me/playlists": {
            "items": [{"name": f"Liste {i}", "uri": f"spotify:playlist:{i}"} for i in range(50)],
            "next": "https://api.spotify.com/v1/me/playlists?offset=50&limit=50",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("offset") == "50":
            return httpx.Response(
                200,
                json={
                    "items": [{"name": "Bollywood Hits", "uri": "spotify:playlist:gesucht"}],
                    "next": None,
                },
            )
        return httpx.Response(200, json=seiten["/v1/me/playlists"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        acct = SpotifyAccount("otto")
        acct._access_token, acct._expires_at = "tok", 9e9
        uri = await acct.find_playlist(c, "Bollywood")

    assert uri == "spotify:playlist:gesucht"


# --- Rueckfall und ehrliche Fehlermeldung -----------------------------


@pytest.mark.asyncio
async def test_kein_playlist_treffer_faellt_auf_das_stueck_zurueck(monkeypatch):
    """'Spiel mir ein schoenes Hamburger Lied' wurde vom Modell als
    kind='playlist' geschickt. Der Handler darf sich nicht darauf
    verlassen, dass die Art richtig geraten wurde."""

    async def find_playlist(self, client, query):
        return None

    async def find_track(self, client, query):
        return {
            "uri": "spotify:track:hh",
            "name": "Hamburg meine Perle",
            "artists": ["Lotto King Karl"],
            "label": "'Hamburg meine Perle'",
        }

    async def play_uris(self, client, uris, device_name="Evoke Play"):
        return True

    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "play_uris", play_uris)

    # Seit telefon-ai#65 muss der Treffer ein Wort des Wunsches enthalten —
    # "Hamburg meine Perle" zu "Hamburger Lied" waere eine Rueckfrage.
    ergebnis = await handle_spotify_play({"playlist": "Hamburg Perle", "kind": "playlist"})

    assert ergebnis["ok"] is True
    assert "Hamburg meine Perle" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_abgelaufene_anmeldung_ueberschreibt_nicht_jeden_fehlschlag(monkeypatch):
    """Der eigentliche Aerger: solange EIN Konto abgemeldet war, meldete
    der Handler das als Grund — auch wenn das andere Konto einwandfrei
    durchsucht wurde und schlicht nichts enthielt."""

    async def find_playlist(self, client, query):
        if self.account == "paula":
            raise SpotifyAuthError("paula", "Refresh token revoked")
        return None

    async def find_track(self, client, query):
        if self.account == "paula":
            raise SpotifyAuthError("paula", "Refresh token revoked")
        return None

    async def find_artist(self, client, query):
        if self.account == "paula":
            raise SpotifyAuthError("paula", "Refresh token revoked")
        return None

    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "find_artist", find_artist)

    ergebnis = await handle_spotify_play({"playlist": "Hamburger Lied"})

    assert ergebnis["ok"] is False
    assert "abgelaufen" not in ergebnis["ack"]
    assert "Hamburger Lied" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_wenn_kein_konto_durchsuchbar_ist_bleibt_es_die_anmeldung(monkeypatch):
    """Umgekehrt muss der Hinweis erhalten bleiben, wenn wirklich kein
    einziges Konto abgefragt werden konnte."""

    async def tot(self, client, query):
        raise SpotifyAuthError(self.account, "Refresh token revoked")

    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    monkeypatch.setattr(SpotifyAccount, "find_playlist", tot)
    monkeypatch.setattr(SpotifyAccount, "find_track", tot)
    monkeypatch.setattr(SpotifyAccount, "find_artist", tot)

    ergebnis = await handle_spotify_play({"playlist": "Irgendwas"})

    assert ergebnis["ok"] is False
    assert "abgelaufen" in ergebnis["ack"]
    assert "Otto" in ergebnis["ack"] and "Paula" in ergebnis["ack"]


async def _tok() -> str:
    return "tok"


# --- Treffer aus mehreren Kandidaten waehlen (3.10.2026) --------------------


def _kandidat(uri, titel, *kuenstler):
    return {"uri": uri, "name": titel, "artists": [{"name": k} for k in kuenstler]}


@pytest.mark.asyncio
async def test_find_track_nimmt_den_titel_der_zum_wunsch_passt():
    """Elektra suchte 'TNT AC/DC' und spielte 'Thunderstruck': Spotify
    lieferte den auf Platz 1, T.N.T. stand weiter hinten. Der Künstler
    stimmte, also fiel es nicht auf."""
    gesehen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["limit"] = request.url.params["limit"]
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        _kandidat("spotify:track:thunder", "Thunderstruck", "AC/DC"),
                        _kandidat("spotify:track:cover", "T.N.T.", "Irgendeine Coverband"),
                        _kandidat("spotify:track:tnt", "T.N.T.", "AC/DC"),
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        acct = SpotifyAccount("otto")
        acct._access_token, acct._expires_at = "tok", 9e9
        treffer = await acct.find_track(c, "TNT AC/DC")

    assert int(gesehen["limit"]) > 1
    assert treffer["uri"] == "spotify:track:tnt"  # Titel UND Künstler, nicht nur der Künstler


def test_beste_wahl_ohne_titeltreffer_bleibt_beim_ersten():
    """Die Ähnlichkeitsprüfung danach entscheidet dann wie bisher."""
    items = [
        _kandidat("spotify:track:a", "That Two Can Play", "Safinteam"),
        _kandidat("spotify:track:b", "Irgendwas", "Jemand"),
    ]
    assert spotify._beste_wahl("Evoke Play", items)["uri"] == "spotify:track:a"
