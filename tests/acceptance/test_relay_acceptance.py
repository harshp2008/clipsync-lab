"""Acceptance tests for the clipsync network relay (spec-first, black box).

Contract: ``docs/design/interfaces.md`` § "relay_server.py / relay_transport.py".
These tests only use the public relay API plus the frozen ``protocol``/``crypto``
modules. They never touch the real clipboard, never use a non-loopback address,
and start the server on an ephemeral port inside pytest.

Owner: orchestrator (written before the implementation job; do not edit during it).
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from clipsync.crypto import FernetCodec
from clipsync.protocol import (
    ClipMessage,
    decode_frame,
    encode_frame,
    image_payload,
    text_payload,
)
from clipsync.relay_server import RelayServer
from clipsync.relay_transport import RelayTransport
from clipsync.transport import Transport, TransportError

TOKEN = "acceptance-shared-secret"
LOOPBACK = "127.0.0.1"


# -- helpers ----------------------------------------------------------------


def relay_url(port: int) -> str:
    return f"ws://{LOOPBACK}:{port}/"


def text_frame(origin: str, seq: int, text: str) -> bytes:
    return encode_frame(ClipMessage(origin=origin, seq=seq, payload=text_payload(text)))


def image_frame(origin: str, seq: int) -> tuple[bytes, bytes]:
    data = bytes(range(256)) * 40 + b"\x89PNG\r\n\x1a\n"
    msg = ClipMessage(origin=origin, seq=seq, payload=image_payload(data, "image/png"))
    return encode_frame(msg), data


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def wait_frames(transport: RelayTransport, count: int, timeout: float = 5.0) -> list[bytes]:
    """Collect up to ``count`` frames, tolerating a temporarily dropped link."""
    frames: list[bytes] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(frames) < count:
        try:
            frames.extend(transport.recv())
        except TransportError:
            pass
        time.sleep(0.01)
    return frames


@contextlib.contextmanager
def running_server(*, token: str = TOKEN, port: int = 0, **kwargs):
    server = RelayServer(token=token, port=port, **kwargs)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@contextlib.contextmanager
def connected_client(server: RelayServer, *, token: str = TOKEN, **kwargs):
    transport = RelayTransport(relay_url(server.port), token, **kwargs)
    transport.connect()
    try:
        yield transport
    finally:
        transport.close()


# -- conformance ------------------------------------------------------------


def test_relay_transport_satisfies_transport_protocol() -> None:
    transport = RelayTransport("ws://127.0.0.1:9/", TOKEN)
    assert isinstance(transport, Transport)
    assert transport.connected is False
    assert transport.url == "ws://127.0.0.1:9/"


def test_server_binds_loopback_only() -> None:
    server = RelayServer(token=TOKEN, port=0)
    assert server.host == LOOPBACK
    server.start()
    try:
        assert server.running is True
        assert server.port > 0
    finally:
        server.stop()
    assert server.running is False

    for bad_host in ("0.0.0.0", "::", "localhost", "192.168.1.10", "example.com"):
        with pytest.raises(ValueError):
            RelayServer(token=TOKEN, host=bad_host)
    with pytest.raises(ValueError):
        RelayServer(token="", port=0)


# -- core exchange ----------------------------------------------------------


def test_two_clients_exchange_text_and_image() -> None:
    with running_server() as server:
        with connected_client(server) as laptop, connected_client(server) as phone:
            assert wait_until(lambda: laptop.connected and phone.connected)

            laptop.send(text_frame("laptop", 0, "hello from the laptop"))
            got = wait_frames(phone, 1)
            assert len(got) == 1
            msg = decode_frame(got[0])
            assert msg.origin == "laptop"
            assert msg.payload.text == "hello from the laptop"

            frame, image_bytes = image_frame("phone", 1)
            phone.send(frame)
            got = wait_frames(laptop, 1)
            assert len(got) == 1
            msg = decode_frame(got[0])
            assert msg.origin == "phone"
            assert msg.payload.mime == "image/png"
            assert msg.payload.data == image_bytes


def test_message_fans_out_to_every_other_client() -> None:
    with running_server() as server:
        with connected_client(server) as a, connected_client(server) as b, connected_client(
            server
        ) as c:
            assert wait_until(lambda: a.connected and b.connected and c.connected)
            a.send(text_frame("a", 0, "broadcast"))
            assert len(wait_frames(b, 1)) == 1
            assert len(wait_frames(c, 1)) == 1
            assert wait_frames(a, 1, timeout=0.4) == []


def test_sender_never_receives_its_own_message() -> None:
    with running_server() as server:
        with connected_client(server) as sender, connected_client(server) as peer:
            assert wait_until(lambda: sender.connected and peer.connected)
            sender.send(text_frame("sender", 0, "not an echo"))
            assert len(wait_frames(peer, 1)) == 1
            assert wait_frames(sender, 1, timeout=0.5) == []


def test_per_sender_message_ordering() -> None:
    with running_server() as server:
        with connected_client(server) as sender, connected_client(server) as receiver:
            assert wait_until(lambda: sender.connected and receiver.connected)
            for seq in range(25):
                sender.send(text_frame("sender", seq, f"message-{seq}"))
            got = wait_frames(receiver, 25)
            assert len(got) == 25
            assert [decode_frame(frame).seq for frame in got] == list(range(25))
            assert [decode_frame(frame).payload.text for frame in got] == [
                f"message-{seq}" for seq in range(25)
            ]


def test_opaque_non_json_frames_round_trip() -> None:
    blob = bytes(range(256)) + b'\r\n\x00{"not":"json"}\n\xff'
    with running_server() as server:
        with connected_client(server) as sender, connected_client(server) as receiver:
            assert wait_until(lambda: sender.connected and receiver.connected)
            sender.send(blob)
            got = wait_frames(receiver, 1)
            assert got == [blob]


# -- authentication ---------------------------------------------------------


def test_wrong_token_is_rejected() -> None:
    with running_server() as server:
        with connected_client(server) as peer:
            assert wait_until(lambda: peer.connected)
            bad = RelayTransport(
                relay_url(server.port),
                "definitely-the-wrong-token",
                reconnect=False,
                connect_timeout=3.0,
            )
            try:
                with pytest.raises(TransportError):
                    bad.connect()
                assert bad.connected is False
            finally:
                bad.close()
            # The authenticated peer is unaffected.
            peer.send(text_frame("peer", 0, "still works"))
            assert len(wait_frames(peer, 1, timeout=0.3)) == 0  # no echo
            with connected_client(server) as other:
                assert wait_until(lambda: other.connected)
                peer.send(text_frame("peer", 1, "post-rejection"))
                assert len(wait_frames(other, 1)) == 1


async def _tokenless_probe(url: str) -> None:
    """Connect without an Authorization header; the server must refuse."""
    import websockets

    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, open_timeout=2, close_timeout=1), 3
        )
    except Exception:  # noqa: BLE001 - rejected during the handshake is fine
        return
    try:
        try:
            await asyncio.wait_for(ws.send(b"x"), 2)
        except Exception:  # noqa: BLE001 - closed straight after the upgrade
            return
        try:
            await asyncio.wait_for(ws.wait_closed(), 2)
        except asyncio.TimeoutError:
            raise AssertionError(
                "server accepted a tokenless client and kept it open"
            ) from None
        except Exception:  # noqa: BLE001 - any close is a refusal
            return
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


def test_missing_token_is_rejected() -> None:
    with running_server() as server:
        asyncio.run(_tokenless_probe(relay_url(server.port)))


# -- size limit -------------------------------------------------------------


def test_oversize_frame_is_rejected_by_the_server() -> None:
    # Give the client a larger limit so the *server* is the one rejecting.
    with running_server(max_frame_bytes=512) as server:
        with connected_client(
            server, max_frame_bytes=4096, reconnect=False
        ) as offender, connected_client(
            server, max_frame_bytes=4096, reconnect=False
        ) as bystander:
            assert wait_until(lambda: offender.connected and bystander.connected)
            with contextlib.suppress(TransportError):
                offender.send(b"x" * 600)
            assert wait_until(lambda: not offender.connected, timeout=3.0)
            assert wait_frames(bystander, 1, timeout=0.5) == []
            assert bystander.connected is True


def test_oversize_frame_is_rejected_by_the_client() -> None:
    with running_server() as server:
        with connected_client(server, max_frame_bytes=512) as sender, connected_client(
            server, max_frame_bytes=512
        ) as receiver:
            assert wait_until(lambda: sender.connected and receiver.connected)
            with pytest.raises(TransportError):
                sender.send(b"x" * 600)
            assert wait_frames(receiver, 1, timeout=0.4) == []
            assert sender.connected is True


# -- reconnect --------------------------------------------------------------


def test_client_reconnects_after_server_restart() -> None:
    token = TOKEN
    server = RelayServer(token=token, port=0)
    server.start()
    port = server.port
    a = RelayTransport(relay_url(port), token, reconnect_delay=0.05)
    b = RelayTransport(relay_url(port), token, reconnect_delay=0.05)
    restarted = None
    try:
        a.connect()
        b.connect()
        assert wait_until(lambda: a.connected and b.connected)
        a.send(text_frame("a", 0, "before-restart"))
        assert len(wait_frames(b, 1)) == 1

        server.stop()
        assert wait_until(lambda: not a.connected and not b.connected, timeout=5.0)

        # Rebind the same port (allow the OS a moment to release it).
        deadline = time.monotonic() + 5.0
        while True:
            try:
                restarted = RelayServer(token=token, port=port)
                restarted.start()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

        assert wait_until(lambda: a.connected and b.connected, timeout=10.0)
        a.send(text_frame("a", 1, "after-restart"))
        got = wait_frames(b, 1, timeout=5.0)
        assert [decode_frame(frame).payload.text for frame in got] == ["after-restart"]
    finally:
        a.close()
        b.close()
        if restarted is not None:
            restarted.stop()


# -- end-to-end encryption pass-through -------------------------------------


def test_encrypted_payloads_pass_through_untouched() -> None:
    codec = FernetCodec.generate()
    plaintext = text_frame("a", 0, "only the endpoints can read this")
    ciphertext = codec.encrypt(plaintext)
    assert ciphertext != plaintext

    with running_server() as server:  # server is constructed with no key/codec
        with connected_client(server) as a, connected_client(server) as b:
            assert wait_until(lambda: a.connected and b.connected)
            a.send(ciphertext)
            got = wait_frames(b, 1)
            assert got == [ciphertext]
            assert codec.decrypt(got[0]) == plaintext
            assert decode_frame(codec.decrypt(got[0])).payload.text == (
                "only the endpoints can read this"
            )
