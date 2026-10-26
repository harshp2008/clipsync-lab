"""Unit tests for RelayTransport."""

from __future__ import annotations

import time
import pytest

from clipsync.relay_server import RelayServer
from clipsync.relay_transport import RelayTransport, relay_url
from clipsync.transport import Transport, TransportError


def test_relay_url_helper() -> None:
    """Verify relay_url formatting."""
    assert relay_url(8080) == "ws://127.0.0.1:8080/"
    assert relay_url(9000, host="127.0.0.1") == "ws://127.0.0.1:9000/"


def test_relay_transport_conformance_and_validation() -> None:
    """Verify RelayTransport protocol conformance and input validation."""
    tr = RelayTransport("ws://127.0.0.1:9000/", "token")
    assert isinstance(tr, Transport)
    assert not tr.connected
    assert tr.url == "ws://127.0.0.1:9000/"

    # Invalid URL
    for bad_url in ("", None, 123):  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="url"):
            RelayTransport(bad_url, "token")  # type: ignore[arg-type]

    # Invalid token
    for bad_token in ("", None, 123):  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="token"):
            RelayTransport("ws://127.0.0.1:9000/", bad_token)  # type: ignore[arg-type]

    # Invalid max_frame_bytes
    for bad_max in (0, -1, "100"):  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_frame_bytes"):
            RelayTransport("ws://127.0.0.1:9000/", "token", max_frame_bytes=bad_max)  # type: ignore[arg-type]

    # Invalid reconnect
    with pytest.raises(ValueError, match="reconnect"):
        RelayTransport("ws://127.0.0.1:9000/", "token", reconnect="yes")  # type: ignore[arg-type]

    # Invalid reconnect_delay
    with pytest.raises(ValueError, match="reconnect_delay"):
        RelayTransport("ws://127.0.0.1:9000/", "token", reconnect_delay=-0.5)

    # Invalid connect_timeout
    with pytest.raises(ValueError, match="connect_timeout"):
        RelayTransport("ws://127.0.0.1:9000/", "token", connect_timeout=0)


def test_relay_transport_disconnected_operations() -> None:
    """Verify send and recv fail with TransportError when disconnected."""
    tr = RelayTransport("ws://127.0.0.1:9999/", "token")
    assert not tr.connected

    with pytest.raises(TransportError, match="not connected"):
        tr.send(b"hello")

    with pytest.raises(TransportError, match="not connected"):
        tr.recv()

    # close() on an unconnected transport is safe
    tr.close()
    assert not tr.connected


def test_relay_transport_connect_failure_to_down_server() -> None:
    """Connecting to a closed port raises TransportError quickly."""
    # Use an unallocated port with a short timeout
    tr = RelayTransport(
        "ws://127.0.0.1:54321/",
        "token",
        reconnect=False,
        connect_timeout=0.2,
    )
    with pytest.raises(TransportError):
        tr.connect()
    assert not tr.connected
    tr.close()


def test_relay_transport_frame_coercion_and_non_bytes() -> None:
    """Verify frame coercion for bytearray and memoryview, and rejection of non-bytes."""
    with RelayServer(token="coerce-token", port=0) as server:
        with RelayTransport(relay_url(server.port), "coerce-token") as sender:
            with RelayTransport(relay_url(server.port), "coerce-token") as receiver:
                # bytearray
                sender.send(bytearray(b"bytearray-payload"))
                # memoryview
                sender.send(memoryview(b"memoryview-payload"))

                # Invalid non-bytes types raise TransportError
                with pytest.raises(TransportError, match="bytes-like"):
                    sender.send("string")  # type: ignore[arg-type]
                with pytest.raises(TransportError, match="bytes-like"):
                    sender.send(1234)  # type: ignore[arg-type]
                with pytest.raises(TransportError, match="bytes-like"):
                    sender.send(None)  # type: ignore[arg-type]

                # Wait for receiver
                deadline = time.monotonic() + 2.0
                frames: list[bytes] = []
                while len(frames) < 2 and time.monotonic() < deadline:
                    frames.extend(receiver.recv())
                    time.sleep(0.02)

                assert frames == [b"bytearray-payload", b"memoryview-payload"]


def test_relay_transport_idempotency_and_context_manager() -> None:
    """Verify connect and close idempotency and context manager support."""
    with RelayServer(token="idemp-token", port=0) as server:
        tr = RelayTransport(relay_url(server.port), "idemp-token")
        tr.connect()
        assert tr.connected

        # Second connect() is a no-op
        tr.connect()
        assert tr.connected

        tr.close()
        assert not tr.connected

        # Second close() is a no-op
        tr.close()
        assert not tr.connected

        # Context manager usage
        with RelayTransport(relay_url(server.port), "idemp-token") as ctx_tr:
            assert ctx_tr.connected
        assert not ctx_tr.connected
