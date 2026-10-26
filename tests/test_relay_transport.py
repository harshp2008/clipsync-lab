"""Focused unit tests for clipsync relay client transport (RelayTransport).

Tests RelayTransport against the interface contract in docs/design/interfaces.md
and the Transport protocol in clipsync.transport:
- relay_url helper formatting and Transport protocol satisfaction
- no-I/O construction and pre-connect errors
- frame type coercion and non-bytes rejection
- connect/auth success and idle recv
- connect failures (wrong token, closed port) with clean thread teardown
- context manager lifecycle
- FIFO ordering and wire opacity with mixed frames
- client-side max_frame_bytes enforcement
- reconnection semantics (reconnect=True vs reconnect=False)
- idempotent close without thread leaks
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from typing import Generator

import pytest

from clipsync.relay_server import RelayServer
from clipsync.relay_transport import RelayTransport, relay_url
from clipsync.transport import Transport, TransportError

TEST_TOKEN = "unit-test-token-secret"
LOOPBACK = "127.0.0.1"


# -- Test Helpers -----------------------------------------------------------


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    """Poll predicate until True or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def wait_frames(
    transport: RelayTransport, count: int, timeout: float = 3.0, interval: float = 0.01
) -> list[bytes]:
    """Drain frames from transport until count frames are collected or timeout expires."""
    frames: list[bytes] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(frames) < count:
        try:
            frames.extend(transport.recv())
        except TransportError:
            pass
        time.sleep(interval)
    return frames


@contextlib.contextmanager
def running_server(
    *, token: str = TEST_TOKEN, port: int = 0, **kwargs
) -> Generator[RelayServer, None, None]:
    """Context manager running an ephemeral RelayServer on 127.0.0.1."""
    server = RelayServer(token=token, port=port, **kwargs)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@contextlib.contextmanager
def connected_client(
    server: RelayServer, *, token: str = TEST_TOKEN, **kwargs
) -> Generator[RelayTransport, None, None]:
    """Context manager connecting a RelayTransport to a running RelayServer."""
    transport = RelayTransport(relay_url(server.port), token, **kwargs)
    transport.connect()
    try:
        yield transport
    finally:
        transport.close()


# -- Unit Tests -------------------------------------------------------------


def test_relay_url_and_protocol_conformance() -> None:
    """Verify relay_url output formats and RelayTransport satisfies Transport protocol."""
    assert relay_url(8080) == "ws://127.0.0.1:8080/"
    assert relay_url(8080, host=LOOPBACK) == "ws://127.0.0.1:8080/"
    assert relay_url(9000, host="127.0.0.2") == "ws://127.0.0.2:9000/"

    transport = RelayTransport(relay_url(8080), TEST_TOKEN)
    assert isinstance(transport, Transport)
    assert issubclass(TransportError, Exception)


def test_construction_performs_no_io() -> None:
    """Constructor must perform no I/O, spawn no threads, and close() before connect() is safe."""
    url = "ws://127.0.0.1:54321/"
    transport = RelayTransport(url, TEST_TOKEN)
    assert transport.connected is False
    assert transport.url == url

    active_transport_threads = [
        th.name
        for th in threading.enumerate()
        if th.name == "clipsync-relay-transport" and th.is_alive()
    ]
    assert len(active_transport_threads) == 0

    # close() before connect() must be a safe no-op
    transport.close()
    assert transport.connected is False


def test_operations_before_connect_raise_transport_error() -> None:
    """send() and recv() before connect() must raise TransportError."""
    transport = RelayTransport(
        "ws://127.0.0.1:54321/", TEST_TOKEN, max_frame_bytes=64
    )
    with pytest.raises(TransportError):
        transport.send(b"x")
    with pytest.raises(TransportError):
        transport.recv()

    # Oversize frame check also raises TransportError before connect()
    with pytest.raises(TransportError):
        transport.send(b"x" * 100)

    transport.close()


def test_send_frame_type_coercion_and_rejection() -> None:
    """send() must accept bytes, bytearray, memoryview and reject non-bytes-like types."""
    with running_server() as server:
        with connected_client(server) as sender, connected_client(
            server
        ) as receiver:
            assert wait_until(lambda: sender.connected and receiver.connected)

            # bytearray and memoryview are coerced to bytes
            sender.send(bytearray(b"bytearray-data"))
            sender.send(memoryview(b"memoryview-data"))
            got = wait_frames(receiver, 2)
            assert got == [b"bytearray-data", b"memoryview-data"]
            assert all(isinstance(f, bytes) for f in got)

            # Non-bytes types rejected with TransportError
            with pytest.raises(TransportError):
                sender.send("string")  # type: ignore[arg-type]
            with pytest.raises(TransportError):
                sender.send(12345)  # type: ignore[arg-type]
            with pytest.raises(TransportError):
                sender.send(None)  # type: ignore[arg-type]
            with pytest.raises(TransportError):
                sender.send([b"list"])  # type: ignore[arg-type]


def test_connect_success_and_idle_recv() -> None:
    """connect() connects successfully, idle recv() returns [], repeated connect() is no-op."""
    with running_server() as server:
        transport = RelayTransport(relay_url(server.port), TEST_TOKEN)
        try:
            transport.connect()
            assert transport.connected is True
            # idle recv drains nothing and returns an empty list
            assert transport.recv() == []
            # calling connect() when already connected is a no-op
            transport.connect()
            assert transport.connected is True
        finally:
            transport.close()
        assert transport.connected is False


def test_context_manager_lifecycle() -> None:
    """RelayTransport context manager connects on enter and closes on exit."""
    with running_server() as server:
        with RelayTransport(relay_url(server.port), TEST_TOKEN) as transport:
            assert transport.connected is True
            assert transport.recv() == []
        assert transport.connected is False
        assert not any(
            th.name == "clipsync-relay-transport" and th.is_alive()
            for th in threading.enumerate()
        )


def test_connect_failure_wrong_token() -> None:
    """connect() with wrong token raises TransportError, leaves connected False, and leaves no thread."""
    with running_server() as server:
        transport = RelayTransport(
            relay_url(server.port),
            "incorrect-token-secret",
            reconnect=False,
            connect_timeout=2.0,
        )
        try:
            with pytest.raises(TransportError):
                transport.connect()
            assert transport.connected is False
        finally:
            transport.close()

        assert not any(
            th.name == "clipsync-relay-transport" and th.is_alive()
            for th in threading.enumerate()
        )


def test_connect_failure_closed_port() -> None:
    """connect() to a closed port raises TransportError, leaves connected False, and leaves no thread."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        closed_port = s.getsockname()[1]

    transport = RelayTransport(
        f"ws://{LOOPBACK}:{closed_port}/",
        TEST_TOKEN,
        reconnect=False,
        connect_timeout=1.0,
    )
    try:
        with pytest.raises(TransportError):
            transport.connect()
        assert transport.connected is False
    finally:
        transport.close()

    assert not any(
        th.name == "clipsync-relay-transport" and th.is_alive()
        for th in threading.enumerate()
    )


def test_fifo_and_wire_opacity() -> None:
    """Sender sends 20 mixed frames; receiver receives exact byte-identical frames in order."""
    mixed_frames = [
        b'{"origin": "laptop", "seq": 0, "payload": {"text": "hello"}}',
        bytes(range(256)),
        b"",
        b"\x00" * 32,
        b'{"nested": {"array": [1, 2, 3], "flag": true}}',
        b"\xff\xfe\xfd\xfc\xfb\xfa",
        "clip™ UTF-8 日本語 🚀 payload".encode("utf-8"),
        b"raw binary frame with newline \r\n and null \x00",
        b"42",
        b"true",
        b"null",
        b'{"not": "really json trailing junk"',
        bytes(range(128, 256)),
        b"\x01\x02\x03\x04\x05",
        b"simple text message 1",
        b"simple text message 2",
        b'{"type": "image", "data": "Zm9vYmFy"}',
        b"\xde\xad\xbe\xef",
        b"penultimate frame 19",
        b"final frame 20",
    ]
    assert len(mixed_frames) == 20

    with running_server() as server:
        with connected_client(server) as sender, connected_client(
            server
        ) as receiver:
            assert wait_until(lambda: sender.connected and receiver.connected)
            for frame in mixed_frames:
                sender.send(frame)

            received = wait_frames(receiver, 20)
            assert len(received) == 20
            assert received == mixed_frames
            # Second recv call when idle returns empty list
            assert receiver.recv() == []


def test_client_side_size_limit() -> None:
    """Frame exceeding max_frame_bytes raises TransportError client-side without dropping connection."""
    limit = 256
    with running_server() as server:
        with connected_client(
            server, max_frame_bytes=limit
        ) as sender, connected_client(server, max_frame_bytes=limit) as receiver:
            assert wait_until(lambda: sender.connected and receiver.connected)

            # Oversize frame rejected client-side
            with pytest.raises(TransportError) as exc_info:
                sender.send(b"x" * (limit + 1))
            assert "exceeds max_frame_bytes" in str(exc_info.value)

            # Sender remains connected and peer receives nothing
            assert sender.connected is True
            assert wait_frames(receiver, 1, timeout=0.3) == []

            # A legal frame sent afterwards delivers normally
            sender.send(b"legal small frame")
            got = wait_frames(receiver, 1)
            assert got == [b"legal small frame"]


def test_reconnect_true_on_server_restart() -> None:
    """With reconnect=True, client detects server stop, reconnects on restart, and resumes delivery."""
    server = RelayServer(token=TEST_TOKEN, port=0)
    server.start()
    port = server.port
    restarted: RelayServer | None = None
    c1 = RelayTransport(
        relay_url(port), TEST_TOKEN, reconnect=True, reconnect_delay=0.05
    )
    c2 = RelayTransport(
        relay_url(port), TEST_TOKEN, reconnect=True, reconnect_delay=0.05
    )
    try:
        c1.connect()
        c2.connect()
        assert wait_until(lambda: c1.connected and c2.connected)
        c1.send(b"before-restart")
        assert wait_frames(c2, 1) == [b"before-restart"]

        # Stop server: both clients become disconnected
        server.stop()
        assert wait_until(
            lambda: not c1.connected and not c2.connected, timeout=3.0
        )

        # Restart server on the same port (retry briefly on OSError)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                restarted = RelayServer(token=TEST_TOKEN, port=port)
                restarted.start()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

        # Clients automatically reconnect
        assert wait_until(lambda: c1.connected and c2.connected, timeout=5.0)
        c1.send(b"after-restart")
        got = wait_frames(c2, 1, timeout=3.0)
        assert got == [b"after-restart"]
    finally:
        c1.close()
        c2.close()
        if restarted is not None:
            restarted.stop()
        server.stop()


def test_reconnect_false_stays_disconnected() -> None:
    """With reconnect=False, client remains disconnected after server stop even when server returns."""
    server = RelayServer(token=TEST_TOKEN, port=0)
    server.start()
    port = server.port
    restarted: RelayServer | None = None
    client = RelayTransport(
        relay_url(port), TEST_TOKEN, reconnect=False, reconnect_delay=0.05
    )
    try:
        client.connect()
        assert client.connected is True

        server.stop()
        assert wait_until(lambda: not client.connected, timeout=3.0)

        # Restart server on same port
        deadline = time.monotonic() + 5.0
        while True:
            try:
                restarted = RelayServer(token=TEST_TOKEN, port=port)
                restarted.start()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

        # Wait briefly to confirm client does not reconnect
        time.sleep(0.2)
        assert client.connected is False
    finally:
        client.close()
        if restarted is not None:
            restarted.stop()
        server.stop()


def test_close_is_idempotent_and_cleans_up_thread() -> None:
    """close() is idempotent, leaves connected False, and joins background thread cleanly."""
    with running_server() as server:
        transport = RelayTransport(relay_url(server.port), TEST_TOKEN)
        transport.connect()
        assert transport.connected is True
        assert any(
            th.name == "clipsync-relay-transport" and th.is_alive()
            for th in threading.enumerate()
        )

        # First close
        transport.close()
        assert transport.connected is False
        assert not any(
            th.name == "clipsync-relay-transport" and th.is_alive()
            for th in threading.enumerate()
        )

        # Repeated close() calls are safe no-ops
        transport.close()
        transport.close()
        assert transport.connected is False
        assert not any(
            th.name == "clipsync-relay-transport" and th.is_alive()
            for th in threading.enumerate()
        )
