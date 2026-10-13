"""Forward WS connects with the installed websockets version and sends the token header."""

import asyncio

import pytest
import websockets

pytest.importorskip("app.adapters.onebot_v11.network.client")

from app.adapters.onebot_v11.network import client as client_module


def _request_headers(connection):
    request = getattr(connection, "request", None)  # websockets 14+
    if request is not None:
        return request.headers
    return connection.request_headers  # legacy server


@pytest.mark.asyncio
async def test_forward_connection_sends_authorization_header():
    seen = {}

    async def handler(connection, *_path):
        seen["authorization"] = _request_headers(connection).get("Authorization")
        await connection.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        client = client_module.OneBotV11WsClient("forward-test")
        await asyncio.wait_for(client._connect_and_listen({
            "ws_url": f"ws://127.0.0.1:{port}",
            "access_token": "secret-token",
            "access_token_in_url": False,
        }), timeout=10)

    assert seen["authorization"] == "Bearer secret-token"
    assert client._ws is None and not client._connected
