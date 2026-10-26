"""Unit tests for RelayServer."""

from __future__ import annotations

import time
import pytest
from websockets.datastructures import Headers

from clipsync.protocol import MAX_IMAGE_BYTES
from clipsync.relay_server import RELAY_MAX_FRAME_BYTES, RelayServer
from clipsync.relay_transport import RelayTransport, relay_url
from clipsync.transport import TransportError


def test_relay_max_frame_bytes_constant() -> None:
    """Verify RELAY_MAX_FRAME_BYTES formula."""
    expected = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 8192
    assert RELAY_MAX_FRAME_BYTES == expected


def test_relay_server_constructor_validation() -> None:
    """Verify input validation on RelayServer constructor parameters."""
    # Valid construction
    server = RelayServer(token="secret", port=0)
    assert server.host == "127.0.0.1"
    assert server.port == 0
    assert server.running is False

    # Invalid host
    for bad_host in ("0.0.0.0", "::", "localhost", "127.0.0.2", "example.com"):
        with pytest.raises(ValueError, match="host must be '127.0.0.1'"):
            RelayServer(token="secret", host=bad_host)

    # Invalid token
    for bad_token in ("", None, 123):  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="token"):
            RelayServer(token=bad_token)  # type: ignore[arg-type]

    # Invalid port
    for bad_port in (-1, 65536, "0"):  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="port"):
            RelayServer(token="secret", port=bad_port)  # type: ignore[arg-type]

    # Invalid max_frame_bytes
    for bad_max in (0, -10, "100"):  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_frame_bytes"):
            RelayServer(token="secret", max_frame_bytes=bad_max)  # type: ignore[arg-type]


def test_relay_server_lifecycle_and_context_manager() -> None:
    """Verify start, stop, idempotency, and context manager semantics."""
    server = RelayServer(token="test-token", port=0)
    assert not server.running

    server.start()
    try:
        assert server.running
        assert server.port > 0
        bound_port = server.port

        # Calling start() again when running is a no-op
        server.start()
        assert server.running
        assert server.port == bound_port
    finally:
        server.stop()

    assert not server.running
    # Calling stop() again when stopped is a no-op
    server.stop()
    assert not server.running

    # Test context manager
    with RelayServer(token="ctx-token", port=0) as ctx_server:
        assert ctx_server.running
        assert ctx_server.port > 0
    assert not ctx_server.running


def test_relay_server_port_collision_raises_os_error() -> None:
    """Starting two servers on the same port raises OSError."""
    server1 = RelayServer(token="token1", port=0)
    server1.start()
    try:
        port = server1.port
        server2 = RelayServer(token="token2", port=port)
        with pytest.raises(OSError):
            server2.start()
    finally:
        server1.stop()


def test_relay_server_auth_process_request() -> None:
    """Verify handshake HTTP authentication helper."""
    server = RelayServer(token="valid-token")

    # Correct Bearer token -> None (allow upgrade)
    headers_ok = Headers([("Authorization", "Bearer valid-token")])
    assert server._process_request("/", headers_ok) is None

    # Case-insensitivity in header name
    headers_case = Headers([("authorization", "Bearer valid-token")])
    assert server._process_request("/", headers_case) is None

    # Wrong token -> 403
    headers_wrong = Headers([("Authorization", "Bearer invalid-token")])
    res = server._process_request("/", headers_wrong)
    assert res is not None
    assert res[0] == 403

    # Missing header -> 403
    headers_empty = Headers([])
    res = server._process_request("/", headers_empty)
    assert res is not None
    assert res[0] == 403

    # Wrong auth scheme (e.g. Basic) -> 403
    headers_basic = Headers([("Authorization", "Basic dXNlcjpwYXNz")])
    res = server._process_request("/", headers_basic)
    assert res is not None
    assert res[0] == 403


def test_relay_server_client_cleanup_on_stop() -> None:
    """Stopping server disconnects all connected clients."""
    with RelayServer(token="cleanup-token", port=0) as server:
        client = RelayTransport(relay_url(server.port), "cleanup-token")
        client.connect()
        try:
            assert client.connected
            server.stop()
            # Wait for client to detect disconnect
            deadline = time.monotonic() + 2.0
            while client.connected and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not client.connected
        finally:
            client.close()
