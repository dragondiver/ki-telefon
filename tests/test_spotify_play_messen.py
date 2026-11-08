"""spotify_play: Gerätename raus aus dem Suchbegriff, Treffer prüfen, Erfolg messen.

Anlass (telefon-ai#65, #66, 16.09.2026): „Spiel die Spotify Daylist auf Evoke
Play" wurde zu `spotify_play({"playlist": "Evoke Play"})`. Keine Playlist
dieses Namens, also Rückfall auf die Titelsuche — und Spotify liefert zu fast
allem einen ersten Song: „That Two Can Play" lief. Der zweite Anlauf mit
„Daylist" meldete Erfolg, weil Spotify einen toten Playlist-Kontext mit 204
quittiert; der Evoke spielte weiter das Alte.

Regel „LLM urteilt, Code rechnet": Erfolg kommt aus `/me/player`, nicht aus
dem Statuscode.
"""

import json

import httpx
import pytest

from kitelefon.agents import spotify
from kitelefon.agents.spotify import (
    SpotifyAccount,
    SpotifyPlayRejected,
    handle_spotify_play,
)


@pytest.fixture(autouse=True)
def konten(monkeypatch, tmp_path):
    monkeypatch.setattr(spotify, "KNOWN_ACCOUNTS", ["florian", "paula"])
    monkeypatch.setattr(spotify, "_accounts", {})
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(spotify, "REPO", tmp_path)
    monkeypatch.setattr(spotify, "SHORTCUTS_PATH", tmp_path / "playlists.json")
    monkeypatch.setattr(spotify, "_shortcuts_geprueft", True)
    monkeypatch.setattr(spotify, "VERIFY_WAIT", 0.0)
    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    for name in ("florian", "paula"):
        (tmp_path / f"token_{name}.json").write_text('{"refresh_token": "r"}')
    yield


async def _tok() -> str:
    return "tok"


def _konto(client_handler) -> tuple[SpotifyAccount, httpx.AsyncClient]:
    acct = SpotifyAccount("florian")
    acct._access_token, acct._expires_at = "tok", 9e9
    return acct, httpx.AsyncClient(transport=httpx.MockTransport(client_handler))


# --- #65: Gerätename im Suchbegriff -------------------------------------


@pytest.mark.parametrize(
    "roh, erwartet",
    [
        ("Evoke Play", ""),
        ("Daylist auf dem Evoke Play", "Daylist"),
        ("Abendliste auf der Anlage", "Abendliste"),
        ("Rockklassiker Evoke", "Rockklassiker"),
        ("Queen", "Queen"),
        ("Evokes Best", "Evokes Best"),  # nur ganze Wörter
    ],
)
def test_geraetenamen_werden_aus_dem_suchbegriff_entfernt(roh, erwartet):
    assert spotify._ohne_geraetenamen(roh) == erwartet


@pytest.mark.asyncio
async def test_nur_lautsprecher_genannt_fragt_nach_statt_zu_suchen(monkeypatch):
    """`spotify_play({"playlist": "Evoke Play"})` — nach dem Entfernen des
    Gerätenamens bleibt nichts übrig. Rückfrage, kein Such- und kein Play-Aufruf."""
    aufrufe = []

    async def merken(self, client, *a, **kw):
        aufrufe.append(a)
        return None

    for methode in ("find_playlist", "find_track", "find_artist", "play_context", "play_uris"):
        monkeypatch.setattr(SpotifyAccount, methode, merken)

    ergebnis = await handle_spotify_play({"playlist": "Evoke Play", "kind": "playlist"})

    assert ergebnis["ok"] is False
    assert "?" in ergebnis["ack"]
    assert aufrufe == []


@pytest.mark.asyncio
async def test_geraetename_wird_vor_der_suche_abgeschnitten(monkeypatch):
    gesucht = []

    async def find_playlist(self, client, query):
        gesucht.append(query)
        return "spotify:playlist:abc" if self.account == "florian" else None

    async def play_playlist(self, client, uri, device_name="Evoke Play"):
        return True

    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "play_playlist", play_playlist)

    ergebnis = await handle_spotify_play({"playlist": "Abendliste auf dem Evoke Play"})

    assert ergebnis["ok"] is True
    assert gesucht == ["Abendliste", "Abendliste"]
    assert "Abendliste" in ergebnis["ack"] and "Evoke Play" not in ergebnis["ack"]


# --- #65: Rückfall nimmt nicht jeden Treffer ----------------------------


@pytest.mark.parametrize(
    "gesucht, titel, kuenstler, passt",
    [
        ("Hamburger Lied", "Hamburg meine Perle", "Lotto King Karl", False),
        ("Rockklassiker", "That Two Can Play - Original", "Safinteam", False),
        ("Kesariya Arijit Singh", "Kesariya", "Arijit Singh", True),
        ("Bohemian Rhapsody", "Bohemian Rhapsody - Remastered 2011", "Queen", True),
        ("was von Queen", "Don't Stop Me Now", "Queen", True),
        ("ab", "Abba Gold", "ABBA", False),  # Wörter unter 3 Zeichen zählen nicht
        # 03.10.2026: Kürzel bestehen nur aus kurzen Wörtern und fielen komplett durch —
        # AC/DC und T.N.T. fanden den richtigen Treffer und lehnten ihn als "nicht ähnlich" ab.
        ("AC/DC", "Highway to Hell", "AC/DC", True),
        ("T.N.T. AC/DC", "T.N.T.", "AC/DC", True),
        ("TNT", "T.N.T.", "AC/DC", True),  # die Transkription lässt die Punkte weg
        ("tnt von ac dc", "T.N.T.", "AC/DC", True),
        ("AC/DC", "Hells Bells", "Foo Fighters", False),
        ("DC", "ABC", "XYZ", False),
    ],
)
def test_wortueberlappung(gesucht, titel, kuenstler, passt):
    assert spotify._passt_zum_wunsch(gesucht, titel, kuenstler) is passt


@pytest.mark.asyncio
async def test_rueckfall_auf_titel_lehnt_fremden_treffer_ab(monkeypatch):
    """Suche „Evoke Play" liefert „That Two Can Play" → ok: false, kein Play.

    Die Titelsuche ist hier so gemockt wie Spotify am 16.09. wirklich
    antwortete. Egal ob der Handler sie noch erreicht oder schon vorher
    nachfragt: gespielt werden darf das nicht."""
    gespielt = []

    async def find_playlist(self, client, query):
        return None

    async def find_track(self, client, query):
        return {
            "uri": "spotify:track:x",
            "name": "That Two Can Play - Original",
            "artists": ["Safinteam"],
            "label": "'That Two Can Play - Original' von Safinteam",
        }

    async def find_artist(self, client, query):
        return {"uri": "spotify:artist:y", "name": "Playboi Carti"}

    async def play(self, client, *a, **kw):
        gespielt.append(a)
        return True

    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "find_artist", find_artist)
    monkeypatch.setattr(SpotifyAccount, "play_uris", play)
    monkeypatch.setattr(SpotifyAccount, "play_context", play)

    ergebnis = await handle_spotify_play({"playlist": "Evoke Play", "kind": "playlist"})

    assert ergebnis["ok"] is False
    assert gespielt == []
    assert "That Two Can Play" not in ergebnis["ack"]
    assert ergebnis["ack"].endswith("?")


@pytest.mark.asyncio
async def test_rueckfall_auf_titel_verlangt_wortueberlappung(monkeypatch):
    """Kein Wort des Wunsches im Treffer → Rückfrage statt raten."""
    gespielt = []

    async def find_playlist(self, client, query):
        return None

    async def find_track(self, client, query):
        return {
            "uri": "spotify:track:x",
            "name": "Hamburg meine Perle",
            "artists": ["Lotto King Karl"],
            "label": "'Hamburg meine Perle' von Lotto King Karl",
        }

    async def find_artist(self, client, query):
        return None

    async def play(self, client, *a, **kw):
        gespielt.append(a)
        return True

    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "find_artist", find_artist)
    monkeypatch.setattr(SpotifyAccount, "play_uris", play)
    monkeypatch.setattr(SpotifyAccount, "play_context", play)

    ergebnis = await handle_spotify_play({"playlist": "Hamburger Lied", "kind": "playlist"})

    assert ergebnis["ok"] is False
    assert gespielt == []
    assert "Hamburger Lied" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_kind_track_prueft_den_treffer_ebenfalls(monkeypatch):
    gespielt = []

    async def find_track(self, client, query):
        return {
            "uri": "spotify:track:x",
            "name": "That Two Can Play",
            "artists": ["Safinteam"],
            "label": "'That Two Can Play' von Safinteam",
        }

    async def play_uris(self, client, uris, device_name="Evoke Play"):
        gespielt.append(uris)
        return True

    monkeypatch.setattr(SpotifyAccount, "find_track", find_track)
    monkeypatch.setattr(SpotifyAccount, "play_uris", play_uris)

    ergebnis = await handle_spotify_play({"playlist": "Bohemian Rhapsody", "kind": "track"})

    assert ergebnis["ok"] is False
    assert gespielt == []
    assert "That Two Can Play" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_kind_artist_prueft_den_treffer_ebenfalls(monkeypatch):
    gespielt = []

    async def find_artist(self, client, query):
        return {"uri": "spotify:artist:y", "name": "Playboi Carti"}

    async def play_context(self, client, uri, device_name="Evoke Play"):
        gespielt.append(uri)
        return True

    monkeypatch.setattr(SpotifyAccount, "find_artist", find_artist)
    monkeypatch.setattr(SpotifyAccount, "play_context", play_context)

    ergebnis = await handle_spotify_play({"playlist": "Queen", "kind": "artist"})

    assert ergebnis["ok"] is False
    assert gespielt == []

    async def find_queen(self, client, query):
        return {"uri": "spotify:artist:q", "name": "Queen"}

    monkeypatch.setattr(SpotifyAccount, "find_artist", find_queen)
    ergebnis = await handle_spotify_play({"playlist": "Queen", "kind": "artist"})
    assert ergebnis["ok"] is True
    assert gespielt == ["spotify:artist:q"]


# --- #66: Daylist & Co. ---------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Daylist", "daylist", "Spotify Daylist", "Discover Weekly", "release radar"]
)
@pytest.mark.asyncio
async def test_algorithmische_playlists_bekommen_feste_antwort_ohne_http(monkeypatch, name):
    async def kein_netz(self, client, *a, **kw):
        raise AssertionError("kein HTTP erwartet")

    for methode in ("find_playlist", "find_track", "find_artist", "play_context", "play_uris"):
        monkeypatch.setattr(SpotifyAccount, methode, kein_netz)

    ergebnis = await handle_spotify_play({"playlist": f"{name} auf dem Evoke Play"})

    assert ergebnis["ok"] is False
    assert "Spotify" in ergebnis["ack"] and "App" in ergebnis["ack"]


# --- #66: play_context / play_uris messen -------------------------------


def _player_handler(player_antworten: list[httpx.Response], puts: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": [{"id": "dev1", "name": "Evoke Play"}]})
        if request.method == "PUT" and request.url.path.endswith("/me/player/play"):
            puts.append(json.loads(request.content))
            return httpx.Response(204)
        if request.method == "GET" and request.url.path.endswith("/me/player"):
            return player_antworten.pop(0) if len(player_antworten) > 1 else player_antworten[0]
        raise AssertionError(f"unerwartet: {request.method} {request.url}")

    return handler


@pytest.mark.asyncio
async def test_204_ohne_wirkung_ist_kein_erfolg():
    """Gemessen am 16.09.: PUT auf eine tote Playlist-ID → 204, `/me/player`
    zeigt danach `context: null` und den alten Titel."""
    puts: list[dict] = []
    alt = httpx.Response(
        200, json={"context": None, "item": {"uri": "spotify:track:alt"}, "is_playing": True}
    )
    acct, client = _konto(_player_handler([alt], puts))
    async with client:
        with pytest.raises(SpotifyPlayRejected) as exc:
            await acct.play_context(client, "spotify:playlist:tot")

    assert puts == [{"context_uri": "spotify:playlist:tot"}]
    assert "nicht angenommen" in exc.value.ack
    assert "läuft" not in exc.value.ack.lower() or "nicht" in exc.value.ack.lower()


@pytest.mark.asyncio
async def test_anderer_kontext_nach_dem_put_ist_kein_erfolg():
    puts: list[dict] = []
    anders = httpx.Response(200, json={"context": {"uri": "spotify:playlist:anders"}, "item": {}})
    acct, client = _konto(_player_handler([anders], puts))
    async with client:
        with pytest.raises(SpotifyPlayRejected):
            await acct.play_context(client, "spotify:playlist:gewollt")


@pytest.mark.asyncio
async def test_passender_kontext_ist_erfolg_auch_erst_beim_zweiten_blick():
    """Spotify braucht manchmal einen Moment: erster Blick noch alt, zweiter passt."""
    puts: list[dict] = []
    alt = httpx.Response(200, json={"context": None, "item": {"uri": "spotify:track:alt"}})
    neu = httpx.Response(200, json={"context": {"uri": "spotify:playlist:gewollt"}, "item": {}})
    acct, client = _konto(_player_handler([alt, neu], puts))
    async with client:
        assert await acct.play_context(client, "spotify:playlist:gewollt") is True


@pytest.mark.asyncio
async def test_play_uris_misst_den_titel():
    puts: list[dict] = []
    passt = httpx.Response(200, json={"context": None, "item": {"uri": "spotify:track:abc"}})
    acct, client = _konto(_player_handler([passt], puts))
    async with client:
        assert await acct.play_uris(client, ["spotify:track:abc"]) is True

    falsch = httpx.Response(200, json={"context": None, "item": {"uri": "spotify:track:alt"}})
    acct, client = _konto(_player_handler([falsch], puts))
    async with client:
        with pytest.raises(SpotifyPlayRejected) as exc:
            await acct.play_uris(client, ["spotify:track:abc"])
    assert "Titel" in exc.value.ack


@pytest.mark.asyncio
async def test_204_von_me_player_heisst_nichts_laeuft():
    """Ohne aktiven Player antwortet /me/player mit 204 und leerem Body."""
    puts: list[dict] = []
    acct, client = _konto(_player_handler([httpx.Response(204)], puts))
    async with client:
        with pytest.raises(SpotifyPlayRejected):
            await acct.play_context(client, "spotify:playlist:x")


@pytest.mark.asyncio
async def test_handler_meldet_abgelehnte_playlist_ehrlich(monkeypatch):
    """PUT 204, aber `/me/player` zeigt anderen Kontext → ok: false, keine
    Erfolgsbehauptung in der Ansage."""

    async def find_playlist(self, client, query):
        return "spotify:playlist:tot" if self.account == "florian" else None

    async def play_context(self, client, uri, device_name="Evoke Play"):
        raise SpotifyPlayRejected(uri)

    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "play_context", play_context)

    for args in ({"playlist": "Mixtape"}, {"playlist": "Mixtape", "wer": "Otto"}):
        ergebnis = await handle_spotify_play(args)
        assert ergebnis["ok"] is False
        assert "nicht angenommen" in ergebnis["ack"]
        assert "Spiele" not in ergebnis["ack"]


@pytest.mark.asyncio
async def test_handler_meldet_erfolg_wenn_der_kontext_passt(monkeypatch):
    async def find_playlist(self, client, query):
        return "spotify:playlist:gut" if self.account == "florian" else None

    async def play_context(self, client, uri, device_name="Evoke Play"):
        return True

    monkeypatch.setattr(SpotifyAccount, "find_playlist", find_playlist)
    monkeypatch.setattr(SpotifyAccount, "play_context", play_context)

    ergebnis = await handle_spotify_play({"playlist": "Mixtape"})

    assert ergebnis["ok"] is True
    assert "Mixtape" in ergebnis["ack"]


# --- #66: tote Shortcuts beim Laden melden --------------------------------


@pytest.mark.asyncio
async def test_tote_shortcut_ids_werden_geloggt(monkeypatch, tmp_path, caplog):
    (tmp_path / "playlists.json").write_text(
        json.dumps(
            {
                "florian": {
                    "daylist": "spotify:playlist:tot",
                    "mixtape 1": "spotify:playlist:lebt",
                },
                "paula": {"kaputt": "spotify:playlist:fehler"},
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlists/tot"):
            return httpx.Response(404, json={"error": {"status": 404}})
        if request.url.path.endswith("/playlists/lebt"):
            return httpx.Response(200, json={"id": "lebt"})
        raise httpx.ConnectError("weg")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level("WARNING", logger="kitelefon.agents.spotify"):
            await spotify._pruefe_shortcuts(client)

    meldungen = "\n".join(r.getMessage() for r in caplog.records)
    assert "daylist" in meldungen and "tot" in meldungen
    assert "kaputt" in meldungen
    assert "mixtape 1" not in meldungen
