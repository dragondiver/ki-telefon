"""Unverstandener Musikwunsch (#29).

Ein schlecht verstandener Name ist kein vager Wunsch. Das Modell suchte das
Beispielwort aus der Parameterbeschreibung, fand nichts und spielte dann einen
selbst gewählten Künstler, angesagt als Erfolg. Jetzt: kein Beispiel, das wie
ein Listenname klingt, und bei „nichts gefunden“ einmal nachfragen.
"""

import pytest

from kitelefon import roles
from kitelefon.agents import spotify
from kitelefon.agents.spotify import NICHTS_GEFUNDEN_HINWEIS, SpotifyAccount, handle_spotify_play
from kitelefon.tools import ALL_TOOL_SCHEMAS, SPOTIFY_PLAY_TOOL_SCHEMA


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


async def _tok() -> str:
    return "tok"


async def _nichts(self, client, query):
    return None


@pytest.fixture
def spotify_findet_nichts(monkeypatch):
    gespielt = []

    async def spielen(self, *args, **kwargs):
        gespielt.append(args)
        return True

    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok())
    for methode in ("find_playlist", "find_track", "find_artist"):
        monkeypatch.setattr(SpotifyAccount, methode, _nichts)
    for methode in ("play_playlist", "play_context", "play_uris"):
        monkeypatch.setattr(SpotifyAccount, methode, spielen)
    return gespielt


# --- Beschreibung und Prompt ----------------------------------------------


def test_kein_beispiel_listenname_in_den_schemas():
    for schema in ALL_TOOL_SCHEMAS:
        assert "Abendliste" not in str(schema), schema["name"]


def test_playlist_parameter_nennt_die_regel_ohne_fuellwoerter():
    text = SPOTIFY_PLAY_TOOL_SCHEMA["parameters"]["properties"]["playlist"]["description"]
    assert "ohne Füllwörter" in text
    for fuellwort in ("'spiel'", "'auf Spotify'", "Lautsprecher"):
        assert fuellwort in text


def test_selbst_aussuchen_nur_bei_vagem_wunsch():
    suffix = roles.SPOTIFY_SUFFIX
    assert "erkennbar vage" in suffix
    assert "Stimmung, Genre oder 'irgendwas'" in suffix


def test_genannter_name_ohne_treffer_heisst_nachfragen():
    suffix = roles.SPOTIFY_SUFFIX
    assert "Hat der Anrufer einen Namen genannt (Titel, Künstler, Liste)" in suffix
    assert "frag\n  einmal nach" in suffix
    assert "Spiel dann nichts anderes und sag keinen Erfolg an." in suffix


# --- Rückgabe bei „nichts gefunden“ ---------------------------------------


def test_hinweis_lenkt_auf_nachfrage_nicht_auf_ersatz():
    assert "frag einmal nach" in NICHTS_GEFUNDEN_HINWEIS
    assert "Spiel nichts anderes" in NICHTS_GEFUNDEN_HINWEIS
    assert "keinen Erfolg" in NICHTS_GEFUNDEN_HINWEIS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"playlist": "Unbekannte Kapelle", "kind": "playlist"},
        {"playlist": "Unbekannte Kapelle", "kind": "playlist", "wer": "Otto"},
        {"playlist": "Unbekannte Kapelle", "kind": "artist"},
        {"playlist": "Unbekannte Kapelle", "kind": "track"},
    ],
)
async def test_nichts_gefunden_gibt_den_hinweis_mit(spotify_findet_nichts, args):
    ergebnis = await handle_spotify_play(args)

    assert ergebnis["ok"] is False
    assert "Unbekannte Kapelle" in ergebnis["ack"]
    assert ergebnis["hinweis"] == NICHTS_GEFUNDEN_HINWEIS
    assert spotify_findet_nichts == []
