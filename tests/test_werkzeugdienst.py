"""Werkzeug-Dienst: telefon-ai stellt seine Werkzeuge anderen Geräten bereit (rundfunk#31)."""

import asyncio
import json

import pytest

from kitelefon.tools import ALL_TOOL_SCHEMAS
from kitelefon.werkzeugdienst import FERN_WERKZEUGE, start_server


async def _anfrage(port, pfad, *, methode="GET", koerper=b"", token="geheim"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    kopf = [
        f"{methode} {pfad} HTTP/1.1",
        "Host: kitelefon.local",
        f"Content-Length: {len(koerper)}",
    ]
    if token is not None:
        kopf.append(f"X-Agent-Token: {token}")
    writer.write(("\r\n".join(kopf) + "\r\n\r\n").encode() + koerper)
    await writer.drain()
    roh = await reader.read()
    writer.close()
    status = int(roh.split(b" ", 2)[1])
    body = roh.split(b"\r\n\r\n", 1)[1]
    return status, (json.loads(body) if body else None)


class FakeQueue:
    def __init__(self):
        self.aufgaben = []

    def enqueue(self, **felder):
        self.aufgaben.append(felder)
        return 7


@pytest.fixture
async def dienst():
    aufrufe = []

    async def ausfuehren(name, args):
        aufrufe.append((name, args))
        if name == "add_shopping_item":
            return {"ok": True, "hinweis": f"{args['name']} steht auf der Liste"}
        return {"ok": False, "error": f"unknown tool: {name}"}

    srv = await start_server(ausfuehren, host="127.0.0.1", port=0, token="geheim")
    port = srv.sockets[0].getsockname()[1]
    try:
        yield port, aufrufe
    finally:
        srv.close()
        await srv.wait_closed()


async def test_liste_liefert_die_fernbedienbaren_werkzeuge(dienst):
    port, _ = dienst
    status, antwort = await _anfrage(port, "/werkzeuge")
    assert status == 200
    namen = [w["name"] for w in antwort["werkzeuge"]]
    assert "add_shopping_item" in namen and "spotify_play" in namen and "delegate_task" in namen
    # Nur mit laufendem Rueckruf sinnvoll — bleibt drinnen.
    assert "ergebnis_per_mail" not in namen
    assert all(w in [s["name"] for s in ALL_TOOL_SCHEMAS] for w in namen)
    assert set(namen) == set(FERN_WERKZEUGE)


async def test_aufruf_fuehrt_das_werkzeug_aus_und_gibt_das_ergebnis_zurueck(dienst):
    port, aufrufe = dienst
    status, antwort = await _anfrage(
        port,
        "/werkzeuge/add_shopping_item",
        methode="POST",
        koerper=json.dumps({"name": "Brötchen", "amount": 6, "unit": "Stück"}).encode(),
    )
    assert status == 200, antwort
    assert antwort["ergebnis"]["hinweis"] == "Brötchen steht auf der Liste"
    assert aufrufe == [("add_shopping_item", {"name": "Brötchen", "amount": 6, "unit": "Stück"})]


async def test_abweisungen(dienst):
    port, aufrufe = dienst
    s1, _ = await _anfrage(port, "/werkzeuge", token="falsch")
    s2, _ = await _anfrage(port, "/werkzeuge", token=None)
    s3, _ = await _anfrage(port, "/werkzeuge/ergebnis_per_mail", methode="POST", koerper=b"{}")
    s4, _ = await _anfrage(port, "/werkzeuge/gibt_es_nicht", methode="POST", koerper=b"{}")
    s5, _ = await _anfrage(port, "/werkzeuge/add_shopping_item", methode="POST", koerper=b"kaputt")
    s6, _ = await _anfrage(port, "/irgendwas")
    assert (s1, s2, s3, s4, s5, s6) == (401, 401, 404, 404, 400, 404)
    assert aufrufe == []


def test_settings_haben_den_dienst_standardmaessig_aus(monkeypatch, ohne_kitelefon_env):
    from kitelefon.config import Settings

    monkeypatch.setenv("OPENAI_API_KEY", "x")
    s = Settings(_env_file=None)
    assert s.werkzeug_port == 0 and s.werkzeug_token == ""


def test_katalog_enthaelt_die_haushalts_werkzeuge():
    namen = {s["name"] for s in ALL_TOOL_SCHEMAS if s["name"] in FERN_WERKZEUGE}

    assert {"web_suche", "merke", "erinnere", "vergiss"} <= namen
