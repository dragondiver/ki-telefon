"""Rückruf: der Heim-Agent meldet ein Ergebnis, das Telefon klingelt."""

import asyncio
import json

import pytest

from kitelefon.rueckruf import Rueckruf, start_server


async def _anfrage(
    port: int,
    koerper: bytes,
    *,
    pfad="/api/agent/callback",
    methode="POST",
    token: str | None = None,
) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    kopf = [
        f"{methode} {pfad} HTTP/1.1",
        "Host: kitelefon.local",
        "Content-Type: application/json",
        f"Content-Length: {len(koerper)}",
    ]
    if token is not None:
        kopf.append(f"X-Agent-Token: {token}")
    writer.write(("\r\n".join(kopf) + "\r\n\r\n").encode() + koerper)
    await writer.drain()
    rohdaten = await reader.read()
    writer.close()
    kopfzeile, _, rest = rohdaten.partition(b"\r\n")
    status = int(kopfzeile.split()[1])
    return status, rest.decode("utf-8", "replace")


@pytest.fixture
async def server():
    """Startet den Zuhörer auf einem freien Port; sammelt Annahmen."""
    angenommen: list[Rueckruf] = []
    zustand = {"frei": True}

    def annahme(r: Rueckruf) -> bool:
        if not zustand["frei"]:
            return False
        angenommen.append(r)
        return True

    srv = await start_server(
        annahme, host="127.0.0.1", port=0, pfad="/api/agent/callback", token="geheim"
    )
    port = srv.sockets[0].getsockname()[1]
    try:
        yield port, angenommen, zustand
    finally:
        srv.close()
        await srv.wait_closed()


NUTZLAST = json.dumps(
    {
        "task_id": 12,
        "agent_type": "research",
        "description": "Fahrzeit Stuttgart–Ulm",
        "result": "Etwa 57 Minuten. Quelle: bahn.de",
    }
).encode()


async def test_gueltiger_rueckruf_wird_angenommen(server):
    port, angenommen, _ = server
    status, _ = await _anfrage(port, NUTZLAST, token="geheim")
    assert status == 202
    assert angenommen[0].task_id == 12
    assert "57 Minuten" in angenommen[0].ergebnis
    assert angenommen[0].beschreibung == "Fahrzeit Stuttgart–Ulm"


async def test_besetztes_telefon_meldet_503(server):
    """Der Zusteller soll es später nochmal versuchen, nicht aufgeben."""
    port, angenommen, zustand = server
    zustand["frei"] = False
    status, _ = await _anfrage(port, NUTZLAST, token="geheim")
    assert status == 503
    assert angenommen == []


async def test_falsches_token_prallt_ab(server):
    port, angenommen, _ = server
    status, _ = await _anfrage(port, NUTZLAST, token="geraten")
    assert status == 401
    assert angenommen == []


async def test_fehlendes_token_prallt_ab(server):
    port, angenommen, _ = server
    status, _ = await _anfrage(port, NUTZLAST)
    assert status == 401
    assert angenommen == []


async def test_ohne_ergebnis_klingelt_nichts(server):
    """Ein Telefon, das grundlos klingelt, ist schlimmer als ein Fehler."""
    port, angenommen, _ = server
    leer = json.dumps({"task_id": 5, "description": "x", "result": "  "}).encode()
    status, text = await _anfrage(port, leer, token="geheim")
    assert status == 400
    assert "Ergebnis" in text
    assert angenommen == []


async def test_kaputtes_json_prallt_ab(server):
    port, angenommen, _ = server
    status, _ = await _anfrage(port, b"{kein json", token="geheim")
    assert status == 400
    assert angenommen == []


async def test_falscher_pfad_ist_404(server):
    port, angenommen, _ = server
    status, _ = await _anfrage(port, NUTZLAST, pfad="/irgendwas", token="geheim")
    assert status == 404
    assert angenommen == []


async def test_get_ist_nicht_erlaubt(server):
    port, angenommen, _ = server
    status, _ = await _anfrage(port, b"", methode="GET", token="geheim")
    assert status == 405
    assert angenommen == []


async def test_ohne_token_konfiguriert_geht_jeder_durch():
    """Bewusstes Verhalten — aber es soll im Test festgehalten sein."""
    angenommen: list[Rueckruf] = []
    srv = await start_server(
        lambda r: bool(angenommen.append(r)) or True,
        host="127.0.0.1",
        port=0,
        pfad="/api/agent/callback",
        token="",
    )
    port = srv.sockets[0].getsockname()[1]
    try:
        status, _ = await _anfrage(port, NUTZLAST)
        assert status == 202
        assert len(angenommen) == 1
    finally:
        srv.close()
        await srv.wait_closed()
