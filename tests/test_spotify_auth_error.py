"""Ein abgelaufenes Spotify-Konto ist kein Netzproblem.

Vorher fiel beides unter httpx.HTTPError, und das Telefon sagte "Spotify
antwortet gerade nicht" — während Spotify tadellos mit 400 invalid_grant
antwortete. Der Anrufer bekam damit den einzigen Hinweis nicht, der hilft.
"""

import httpx
import pytest

from kitelefon.agents import spotify
from kitelefon.agents.spotify import (
    SpotifyAccount,
    SpotifyAuthError,
    handle_spotify_now_playing,
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
    for name in ("otto", "paula"):
        (tmp_path / f"token_{name}.json").write_text('{"refresh_token": "r-' + name + '"}')
    yield


def konto(name="otto") -> SpotifyAccount:
    return SpotifyAccount(name)


@pytest.mark.asyncio
async def test_400_wird_zu_auth_fehler_mit_konto_und_grund():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SpotifyAuthError) as exc:
            await konto("paula")._access(client)

    assert exc.value.account == "paula"
    assert "revoked" in exc.value.grund
    assert "Paula" in exc.value.ack


@pytest.mark.asyncio
async def test_401_gilt_ebenfalls_als_auth_fehler():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SpotifyAuthError):
            await konto()._access(client)


@pytest.mark.asyncio
async def test_500_bleibt_ein_netzfehler():
    """Ein Serverfehler bei Spotify ist KEIN Grund, neu einzurichten."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await konto()._access(client)


@pytest.mark.asyncio
async def test_telefon_sagt_was_wirklich_los_ist(monkeypatch):
    """Statt 'Spotify antwortet nicht' die Wahrheit: Anmeldung abgelaufen."""

    async def kaputt(self, client):
        raise SpotifyAuthError(self.account, "invalid_grant")

    monkeypatch.setattr(SpotifyAccount, "_access", kaputt)

    ergebnis = await handle_spotify_play({"playlist": "Rockklassiker", "wer": "Otto"})

    assert ergebnis["ok"] is False
    assert "abgelaufen" in ergebnis["ack"]
    assert "antwortet gerade nicht" not in ergebnis["ack"]


@pytest.mark.asyncio
async def test_totes_konto_blockiert_die_suche_im_anderen_nicht(monkeypatch):
    """Ohne Namensnennung wird kaskadiert — ein kaputtes Konto darf das
    funktionierende nicht mit in den Abgrund reissen."""
    gefragt = []

    async def access(self, client):
        gefragt.append(self.account)
        if self.account == "paula":
            raise SpotifyAuthError("paula", "invalid_grant")
        return "token-otto"

    async def find_playlist(self, client, name):
        await self._access(client)
        return "spotify:playlist:abc" if self.account == "otto" else None

    async def play_playlist(self, client, uri, device_name="Evoke Play"):
        return True

    monkeypatch.setattr(SpotifyAccount, "_access", access)
    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "play_playlist", play_playlist)

    ergebnis = await handle_spotify_play({"playlist": "Rockklassiker"})

    assert ergebnis["ok"] is True
    assert "otto" in gefragt and "paula" in gefragt


@pytest.mark.asyncio
async def test_alle_konten_tot_nennt_beide_namen(monkeypatch):
    async def kaputt(self, client):
        raise SpotifyAuthError(self.account, "invalid_grant")

    async def find_playlist(self, client, name):
        await self._access(client)
        return None

    monkeypatch.setattr(SpotifyAccount, "_access", kaputt)
    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)

    ergebnis = await handle_spotify_play({"playlist": "Irgendwas"})

    assert ergebnis["ok"] is False
    assert "Otto" in ergebnis["ack"] and "Paula" in ergebnis["ack"]
    assert "abgelaufen" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_now_playing_meldet_abgelaufene_anmeldung(monkeypatch):
    async def kaputt(self, client):
        raise SpotifyAuthError(self.account, "invalid_grant")

    async def currently_playing(self, client):
        await self._access(client)
        return None

    monkeypatch.setattr(SpotifyAccount, "_access", kaputt)
    monkeypatch.setattr(SpotifyAccount, "currently_playing", currently_playing)

    ergebnis = await handle_spotify_now_playing({})

    assert ergebnis["ok"] is False
    assert "abgelaufen" in ergebnis["ack"]
    assert "läuft nichts" not in ergebnis["ack"]
