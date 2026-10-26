"""Unit tests for clipsync.relay_server (RelayServer).

Verifies the contract in docs/design/interfaces.md § "relay_server.py / relay_transport.py":
- RELAY_MAX_FRAME_BYTES formula derivation from MAX_IMAGE_BYTES
- Constructor validation (strict loopback host, non-empty token, valid port and frame sizes)
- Server lifecycle (start, stop idempotency, context manager, port conflict OSError)
- Handshake authentication (valid Bearer token, rejection of missing or wrong token with HTTP 403)
- Verbatim payload forwarding without echo (raw bytes, non-JSON data, text)
- Frame size enforcement (offender closed with code 1009, bystander unaffected, legal frames follow)
- Client disconnection on server shutdown (close code 1001)
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from clipsync.protocol import MAX_IMAGE_BYTES
from clipsync.relay_server import RELAY_MAX_FRAME_BYTES, RelayServer

TOKEN = "unit-test-relay-token"
LOOPBACK = "127.0.0.1"


def _ws_url(port: int) -> str:
    """Format a loopback WebSocket URL for the given port."""
    return f"ws://{LOOPBACK}:{port}/"


def _auth_headers(token: str = TOKEN) -> dict[str, str]:
    """Return standard Bearer authorization headers."""
    return {"Authorization": f"Bearer {token}"}


# -- Constant and Constructor Validation ------------------------------------


def test_relay_max_frame_bytes_formula() -> None:
    """RELAY_MAX_FRAME_BYTES matches the base64 expansion formula with slack."""
    expected = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 8192
    assert RELAY_MAX_FRAME_BYTES == expected
    assert RELAY_MAX_FRAME_BYTES > MAX_IMAGE_BYTES


def test_constructor_defaults_and_validation() -> None:
    """RelayServer validates host, token, port, and size parameters on init."""
    # Valid defaults
    server = RelayServer(token=TOKEN)
    assert server.host == LOOPBACK
    assert server.port == 0
    assert server.running is False
    assert server._max_frame_bytes == RELAY_MAX_FRAME_BYTES

    # Loopback-only enforcement
    for bad_host in (
        "0.0.0.0",
        "::",
        "localhost",
        "192.168.1.100",
        "10.0.0.1",
        "example.com",
        "relay.local",
        "",
    ):
        with pytest.raises(ValueError, match="host must be '127.0.0.1'"):
            RelayServer(token=TOKEN, host=bad_host)

    # Token must be a non-empty string
    with pytest.raises(ValueError, match="token must be a non-empty string"):
        RelayServer(token="")
    with pytest.raises(ValueError, match="token must be a non-empty string"):
        RelayServer(token=None)  # type: ignore[arg-type]

    # Port range validation
    with pytest.raises(ValueError, match="port must be an int"):
        RelayServer(token=TOKEN, port=-1)
    with pytest.raises(ValueError, match="port must be an int"):
        RelayServer(token=TOKEN, port=65536)

    # Frame size limit validation
    with pytest.raises(ValueError, match="max_frame_bytes must be a positive int"):
        RelayServer(token=TOKEN, max_frame_bytes=0)
    with pytest.raises(ValueError, match="max_frame_bytes must be a positive int"):
        RelayServer(token=TOKEN, max_frame_bytes=-1)


# -- Lifecycle & Port Binding -----------------------------------------------


def test_lifecycle_start_stop_idempotent() -> None:
    """Server starts in background thread, binds a port, and stop is idempotent."""
    server = RelayServer(token=TOKEN, port=0)
    assert server.running is False
    assert server.port == 0

    server.start()
    try:
        assert server.running is True
        assert server.port > 0
        bound_port = server.port

        # Calling start() while already running is a no-op
        server.start()
        assert server.running is True
        assert server.port == bound_port
    finally:
        server.stop()

    assert server.running is False

    # Second stop() is a safe no-op
    server.stop()
    assert server.running is False


def test_context_manager() -> None:
    """RelayServer context manager starts on enter and stops on exit."""
    server = RelayServer(token=TOKEN, port=0)
    assert server.running is False
    with server as s:
        assert s is server
        assert server.running is True
        assert server.port > 0
    assert server.running is False


def test_port_conflict_raises_oserror() -> None:
    """Starting a second server on an already-bound port raises OSError."""
    with RelayServer(token=TOKEN, port=0) as server1:
        bound_port = server1.port
        server2 = RelayServer(token=TOKEN, port=bound_port)
        with pytest.raises(OSError):
            server2.start()
        assert server2.running is False
        server2.stop()


# -- Handshake Authentication ----------------------------------------------


def test_handshake_auth_valid_token() -> None:
    """A client presenting the valid Bearer token connects and registers."""
    with RelayServer(token=TOKEN) as server:
        async def _test() -> None:
            url = _ws_url(server.port)
            async with websockets.connect(url, extra_headers=_auth_headers()) as ws:
                assert not ws.closed
                assert len(server._clients) == 1

        asyncio.run(_test())


def test_handshake_auth_missing_token_refused() -> None:
    """A connection without Authorization header is rejected during handshake (403)."""
    with RelayServer(token=TOKEN) as server:
        async def _test() -> None:
            url = _ws_url(server.port)
            with pytest.raises(InvalidStatusCode) as exc_info:
                async with websockets.connect(url):
                    pass
            assert exc_info.value.status_code == 403
            assert len(server._clients) == 0

        asyncio.run(_test())


def test_handshake_auth_wrong_token_refused() -> None:
    """A connection with an incorrect token is rejected during handshake (403)."""
    with RelayServer(token=TOKEN) as server:
        async def _test() -> None:
            url = _ws_url(server.port)
            bad_headers = [
                {"Authorization": "Bearer wrong-token"},
                {"Authorization": "Basic dXNlcjpwYXNz"},
                {"Authorization": TOKEN},
            ]
            for headers in bad_headers:
                with pytest.raises(InvalidStatusCode) as exc_info:
                    async with websockets.connect(url, extra_headers=headers):
                        pass
                assert exc_info.value.status_code == 403
                assert len(server._clients) == 0

        asyncio.run(_test())


# -- Message Forwarding & Echo Prevention -----------------------------------


def test_forwarding_verbatim_and_no_echo() -> None:
    """Server forwards raw bytes and text verbatim to peers without echoing to sender."""
    with RelayServer(token=TOKEN) as server:
        async def _test() -> None:
            url = _ws_url(server.port)
            headers = _auth_headers()
            async with websockets.connect(url, extra_headers=headers) as ws_a:
                async with websockets.connect(url, extra_headers=headers) as ws_b:
                    # Non-JSON byte frame with nulls and high bytes
                    binary_payload = bytes(range(256)) + b"\x00\xff\r\n{'not': 'json'}"
                    await ws_a.send(binary_payload)

                    # B receives payload verbatim
                    received_b = await asyncio.wait_for(ws_b.recv(), timeout=2.0)
                    assert received_b == binary_payload
                    assert isinstance(received_b, bytes)

                    # Echo check: A does not receive its own binary_payload.
                    # Send a distinct reply from B; A's first message is B's reply.
                    reply_payload = b"verbatim-reply-from-b"
                    await ws_b.send(reply_payload)
                    received_a = await asyncio.wait_for(ws_a.recv(), timeout=2.0)
                    assert received_a == reply_payload

                    # Text payload forwarding
                    text_payload = "plain text message without formatting"
                    await ws_a.send(text_payload)
                    received_text = await asyncio.wait_for(ws_b.recv(), timeout=2.0)
                    assert received_text == text_payload
                    assert isinstance(received_text, str)

        asyncio.run(_test())


# -- Size Limit Enforcement -------------------------------------------------


def test_oversize_frame_closes_offender_with_1009() -> None:
    """Oversize frame closes offender with code 1009; bystander receives nothing."""
    limit = 256
    with RelayServer(token=TOKEN, max_frame_bytes=limit) as server:
        async def _test() -> None:
            url = _ws_url(server.port)
            headers = _auth_headers()

            async with websockets.connect(
                url, extra_headers=headers, max_size=None
            ) as ws_bystander:
                async with websockets.connect(
                    url, extra_headers=headers, max_size=None
                ) as ws_offender:
                    oversize_data = b"M" * (limit + 64)
                    try:
                        await ws_offender.send(oversize_data)
                        with pytest.raises(ConnectionClosed) as exc_info:
                            await asyncio.wait_for(ws_offender.recv(), timeout=2.0)
                        assert exc_info.value.code == 1009
                    except ConnectionClosed as exc:
                        assert exc.code == 1009

                    # Offender is closed with 1009
                    await asyncio.wait_for(ws_offender.wait_closed(), timeout=2.0)
                    assert ws_offender.closed
                    assert ws_offender.close_code == 1009

                # Bystander was not dropped and received nothing from the offender
                assert not ws_bystander.closed
                assert ws_bystander.close_code is None

                # Server remains operational; legal frame forwards successfully
                async with websockets.connect(
                    url, extra_headers=headers, max_size=None
                ) as ws_legal:
                    legal_data = b"legal frame within 256 bytes"
                    assert len(legal_data) <= limit
                    await ws_legal.send(legal_data)

                    received = await asyncio.wait_for(ws_bystander.recv(), timeout=2.0)
                    assert received == legal_data

        asyncio.run(_test())


# -- Shutdown Behavior ------------------------------------------------------


def test_stop_drops_connected_clients() -> None:
    """RelayServer.stop() closes all active client connections with code 1001."""
    server = RelayServer(token=TOKEN)
    server.start()
    try:
        async def _test() -> None:
            url = _ws_url(server.port)
            ws = await websockets.connect(url, extra_headers=_auth_headers())
            try:
                assert not ws.closed
                assert len(server._clients) == 1

                # Stop server in a worker thread so client event loop can process close frame
                await asyncio.to_thread(server.stop)

                # Client is closed promptly with 1001 (Server shutdown)
                await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
                assert ws.closed
                assert ws.close_code == 1001
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()

        asyncio.run(_test())
    finally:
        server.stop()
