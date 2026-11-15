"""Musik anhalten und Aufgaben ehrlich ansagen (#28).

Am 15.11.2026 sagte die Rolle auf „Du kannst stoppen“ zu, die Musik
anzuhalten. Es gab kein Werkzeug dafür, die Musik lief weiter. Jetzt gibt es
spotify_pause, und das Ergebnis kommt aus der Messung, nicht aus der Zusage.
Am 14.11. hieß eine eben angelegte Aufgabe „fertig“; die Rückgabe von
delegate_task und der Prompt sagen jetzt, dass sie nur angelegt ist.
"""

import httpx
import pytest
from test_call_manager import FakeAudio, FakeDialer, FakeHookSwitch

from kitelefon import roles
from kitelefon.agents import spotify
from kitelefon.agents.spotify import (
    NICHTS_ZU_PAUSIEREN_HINWEIS,
    SpotifyAccount,
    SpotifyAuthError,
    handle_spotify_pause,
)
from kitelefon.call_manager import CallManager
from kitelefon.roles import DELEGATION_SUFFIX, ROLES, full_instructions
from kitelefon.tasks import TaskQueue
from kitelefon.tools import ALL_TOOL_SCHEMAS, SPOTIFY_PAUSE_TOOL_SCHEMA, handle_delegate_task
from kitelefon.werkzeugdienst import FERN_WERKZEUGE


@pytest.fixture(autouse=True)
def konten(monkeypatch, tmp_path):
    monkeypatch.setattr(spotify, "KNOWN_ACCOUNTS", ["otto", "paula"])
    monkeypatch.setattr(spotify, "_accounts", {})
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(spotify, "REPO", tmp_path)
    monkeypatch.setattr(spotify, "VERIFY_WAIT", 0.0)
    for name in ("otto", "paula"):
        (tmp_path / f"token_{name}.json").write_text('{"refresh_token": "r"}')
    yield


async def _tok(konto: str) -> str:
    return f"tok-{konto}"


def _spotify(monkeypatch, spielt: dict[str, bool], pause_status: int = 204, bleibt_an=False):
    """Mock für beide Konten, unterschieden am Access-Token.

    `spielt`: Konto -> läuft dort gerade etwas. Nach einem erfolgreichen PUT
    steht der Player, außer `bleibt_an` ist gesetzt. Zurück kommt die Liste
    der Anfragen als "METHODE pfad konto".
    """
    monkeypatch.setattr(SpotifyAccount, "_access", lambda s, c: _tok(s.account))
    zustand = dict(spielt)
    anfragen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        konto = request.headers["authorization"].removeprefix("Bearer tok-")
        anfragen.append(f"{request.method} {request.url.path} {konto}")
        if request.url.path.endswith("/me/player/currently-playing"):
            if konto not in zustand:
                return httpx.Response(204)
            return httpx.Response(
                200,
                json={
                    "is_playing": zustand[konto],
                    "item": {"name": "Ein Lied", "artists": [{"name": "Eine Band"}]},
                },
            )
        if request.url.path.endswith("/me/player/pause") and request.method == "PUT":
            if pause_status >= 400:
                return httpx.Response(pause_status, json={"error": {"status": pause_status}})
            if not bleibt_an:
                zustand[konto] = False
            return httpx.Response(pause_status)
        raise AssertionError(f"unerwartet: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    echter_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: echter_client(transport=transport))
    return anfragen


# --- Werkzeug -------------------------------------------------------------


@pytest.mark.asyncio
async def test_pausiert_das_konto_auf_dem_etwas_laeuft(monkeypatch):
    anfragen = _spotify(monkeypatch, {"otto": False, "paula": True})

    ergebnis = await handle_spotify_pause({})

    assert ergebnis == {"ok": True, "ack": "Die Musik ist aus."}
    assert [a for a in anfragen if a.startswith("PUT")] == ["PUT /v1/me/player/pause paula"]
    # Nach dem PUT wurde nachgemessen, nicht dem Statuscode geglaubt.
    assert anfragen[-1] == "GET /v1/me/player/currently-playing paula"


@pytest.mark.asyncio
async def test_nichts_laeuft_sagt_das_ehrlich(monkeypatch):
    anfragen = _spotify(monkeypatch, {"otto": False})

    ergebnis = await handle_spotify_pause({})

    assert ergebnis["ok"] is False
    assert ergebnis["ack"] == "Auf Spotify läuft gerade nichts."
    assert ergebnis["hinweis"] == NICHTS_ZU_PAUSIEREN_HINWEIS
    assert not any(a.startswith("PUT") for a in anfragen)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 502])
async def test_api_fehler_beim_anhalten(monkeypatch, status):
    _spotify(monkeypatch, {"paula": True}, pause_status=status)

    ergebnis = await handle_spotify_pause({})

    assert ergebnis["ok"] is False
    assert "nicht anhalten" in ergebnis["ack"]
    assert str(status) in ergebnis["error"]


@pytest.mark.asyncio
async def test_204_aber_musik_laeuft_weiter(monkeypatch):
    """Wie beim Starten zählt die Messung danach, nicht der Statuscode."""
    _spotify(monkeypatch, {"paula": True}, bleibt_an=True)

    ergebnis = await handle_spotify_pause({})

    assert ergebnis["ok"] is False
    assert "läuft noch" in ergebnis["ack"]


@pytest.mark.asyncio
async def test_abgelaufene_anmeldung_ist_nicht_nichts_laeuft(monkeypatch):
    async def kaputt(self, client):
        raise SpotifyAuthError(self.account, "invalid_grant")

    monkeypatch.setattr(SpotifyAccount, "_access", kaputt)

    ergebnis = await handle_spotify_pause({})

    assert ergebnis["ok"] is False
    assert "abgelaufen" in ergebnis["ack"]
    assert "Otto" in ergebnis["ack"] and "Paula" in ergebnis["ack"]
    assert "hinweis" not in ergebnis


def test_schema_im_katalog_und_im_werkzeugdienst():
    namen = [s["name"] for s in ALL_TOOL_SCHEMAS]
    assert namen.count("spotify_pause") == 1
    assert "spotify_pause" in FERN_WERKZEUGE
    assert SPOTIFY_PAUSE_TOOL_SCHEMA["parameters"]["required"] == []
    beschreibung = SPOTIFY_PAUSE_TOOL_SCHEMA["description"]
    assert "'mach die Musik aus'" in beschreibung
    assert "erst nach ok" in beschreibung


@pytest.mark.asyncio
async def test_call_manager_fertigt_spotify_pause_ab(monkeypatch, tmp_path):
    gerufen = []

    async def pause(args):
        gerufen.append(args)
        return {"ok": True, "ack": "Die Musik ist aus."}

    monkeypatch.setattr("kitelefon.call_manager.handle_spotify_pause", pause)
    cm = CallManager(
        hookswitch=FakeHookSwitch([], delay=0.0),
        dialer=FakeDialer(digit=1),
        audio=FakeAudio(),
        open_session=lambda role, on_audio, th: None,
        queue=TaskQueue(tmp_path / "tasks.db"),
    )

    ergebnis = await cm.werkzeug_ausfuehren("spotify_pause", {})

    assert ergebnis["ok"] is True
    assert gerufen == [{}]


# --- Prompt ---------------------------------------------------------------


def test_spotify_abschnitt_nennt_pause_und_ok():
    suffix = roles.SPOTIFY_SUFFIX
    assert suffix.startswith("## Musik (spotify_play / spotify_pause)")
    for wort in ("'stopp'", "'aus'", "'Pause'", "'mach die\n  Musik aus'"):
        assert wort in suffix
    assert "ruf spotify_pause auf" in suffix
    assert "erst,\n  wenn das Werkzeug ok gemeldet hat" in suffix


def test_jede_rolle_mit_spotify_kennt_spotify_pause():
    for r in ROLES:
        text = full_instructions(r)
        if "spotify_play" in text:
            assert "spotify_pause" in text, r["name"]


def test_delegation_sagt_nur_angelegt():
    assert "delegate_task legt die Aufgabe nur an." in DELEGATION_SUFFIX
    assert '"ist in Auftrag" oder "kommt per Mail"' in DELEGATION_SUFFIX
    assert 'nie "fertig" oder "erledigt"' in DELEGATION_SUFFIX


def test_rueckgabe_von_delegate_task_klingt_nicht_nach_erledigt(tmp_path):
    ergebnis = handle_delegate_task(
        {"description": "Dossier zusammenstellen", "deliver_via": "email"},
        TaskQueue(tmp_path / "tasks.db"),
    )

    ack = ergebnis["ack"]
    assert "angelegt" in ack and "später per Mail" in ack
    for wort in ("Notiert", "fertig", "erledigt", "Du bekommst"):
        assert wort not in ack
