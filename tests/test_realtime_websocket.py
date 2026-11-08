"""open_websocket/WebsocketTransport gegen einen lokalen websockets-Server (#93).

Kein Mock von websockets und kein Netz nach draussen: ein echter Server auf
127.0.0.1 nimmt die Verbindung an, die sonst zu OpenAI ginge. Geprueft wird,
was open_websocket selbst festlegt — Modell als Query-Parameter, der
Bearer-Header und max_size=None fuer grosse Nachrichten.
"""

import asyncio
import json

import pytest
import websockets

from kitelefon.realtime import WebsocketTransport, open_websocket

FAKE_KEY = "sk-test-nicht-echt"
MODELL = "gpt-realtime-test"
# Deutlich ueber dem websockets-Standardlimit max_size=2**20 (1 MiB).
GROSS = "x" * (3 * 2**20)


class FakeRealtime:
    """Nimmt einen Client an, merkt sich Pfad und Header, spiegelt Nachrichten."""

    def __init__(self):
        self.pfad = ""
        self.auth = None
        self.empfangen: list[str] = []
        self.geschlossen = asyncio.Event()
        self._server = None
        self.url = ""

    async def __aenter__(self):
        # Der Server selbst darf grosse Nachrichten nicht begrenzen, sonst
        # misst der Test sein eigenes Limit statt das des Clients.
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0, max_size=None)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/v1/realtime"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws):
        self.pfad = ws.request.path
        self.auth = ws.request.headers.get("Authorization")
        await ws.send(json.dumps({"type": "session.created"}))
        try:
            async for nachricht in ws:
                self.empfangen.append(nachricht)
                if nachricht == "gross bitte":
                    await ws.send(GROSS)
                else:
                    await ws.send(json.dumps({"type": "echo", "text": nachricht}))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.geschlossen.set()


@pytest.mark.asyncio
async def test_modell_in_url_und_bearer_header():
    async with FakeRealtime() as server:
        transport = await open_websocket(MODELL, FAKE_KEY, base_url=server.url)
        try:
            assert isinstance(transport, WebsocketTransport)
            assert json.loads(await transport.recv()) == {"type": "session.created"}
            assert server.pfad == f"/v1/realtime?model={MODELL}"
            assert server.auth == f"Bearer {FAKE_KEY}"
        finally:
            await transport.close()


@pytest.mark.asyncio
async def test_send_recv_close_in_beide_richtungen():
    async with FakeRealtime() as server:
        transport = await open_websocket(MODELL, FAKE_KEY, base_url=server.url)
        assert json.loads(await transport.recv()) == {"type": "session.created"}

        await transport.send('{"type": "session.update"}')
        antwort = json.loads(await transport.recv())
        assert antwort == {"type": "echo", "text": '{"type": "session.update"}'}
        assert server.empfangen == ['{"type": "session.update"}']

        await transport.close()
        await asyncio.wait_for(server.geschlossen.wait(), timeout=2)


@pytest.mark.asyncio
async def test_grosse_nachricht_geht_durch():
    """Ohne max_size=None bricht websockets bei > 1 MiB mit 1009 ab."""
    async with FakeRealtime() as server:
        transport = await open_websocket(MODELL, FAKE_KEY, base_url=server.url)
        try:
            await transport.recv()  # session.created
            await transport.send("gross bitte")
            nachricht = await asyncio.wait_for(transport.recv(), timeout=5)
            assert len(nachricht) == len(GROSS)
        finally:
            await transport.close()
