"""End-to-end integration tests for clipsync over real WebSocket relay.

Puts SyncEngine over real RelayTransport connected to a real RelayServer
on 127.0.0.1:0 (loopback only, no mocks).

Scenarios:
1. Bidirectional text sync without echo loops:
   Two SyncEngines ("laptop", "phone") over RelayTransports; sync in both
   directions; assert applied >= 1, clipboards stabilize, no unbounded resends,
   and dropped_loop == 0.
2. Encrypted image pass-through:
   Deterministic ~64 KiB image payload encrypted with FernetCodec; forwarded
   through RelayServer (which has no key/codec); received and applied
   verbatim by peer; peer with mismatched codec drops the frame.
3. Outbox persistence across relay restart:
   Frame queued in outbox while relay is offline; relay restarted on same port;
   transport reconnects; frame drains from outbox and applies exactly once.
"""

from __future__ import annotations

import time
from typing import Callable

from clipsync.clipboard import FakeClipboard
from clipsync.crypto import FernetCodec
from clipsync.engine import SyncEngine
from clipsync.protocol import image_payload, text_payload
from clipsync.relay_server import RelayServer
from clipsync.relay_transport import RelayTransport, relay_url

TOKEN = "integration-test-secret-token"
LOOPBACK = "127.0.0.1"


def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01
) -> bool:
    """Poll predicate until True or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def test_text_sync_bidirectional_no_echo() -> None:
    """Two engines sync text bidirectionally without echo loops or resends."""
    server = RelayServer(token=TOKEN, host=LOOPBACK, port=0)
    server.start()
    port = server.port

    clip_laptop = FakeClipboard()
    clip_phone = FakeClipboard()

    t_laptop = RelayTransport(relay_url(port), TOKEN, reconnect_delay=0.05)
    t_phone = RelayTransport(relay_url(port), TOKEN, reconnect_delay=0.05)

    try:
        t_laptop.connect()
        t_phone.connect()
        assert wait_until(lambda: t_laptop.connected and t_phone.connected, timeout=3.0)

        engine_laptop = SyncEngine("laptop", clip_laptop, t_laptop, seq_base=0)
        engine_phone = SyncEngine("phone", clip_phone, t_phone, seq_base=0)

        # 1. Laptop -> Phone text sync
        laptop_text = "Hello from laptop across WebSocket relay!"
        clip_laptop.set_payload(text_payload(laptop_text))

        deadline = time.monotonic() + 5.0
        applied_phone = 0
        laptop_dropped_loop = 0
        phone_dropped_loop = 0

        while time.monotonic() < deadline:
            stats_l = engine_laptop.poll_once()
            stats_p = engine_phone.poll_once()
            applied_phone += stats_p.applied
            laptop_dropped_loop += stats_l.dropped_loop
            phone_dropped_loop += stats_p.dropped_loop
            if clip_phone.payload == text_payload(laptop_text):
                break
            time.sleep(0.01)

        assert clip_phone.payload == text_payload(laptop_text)
        assert applied_phone >= 1
        assert laptop_dropped_loop == 0
        assert phone_dropped_loop == 0

        # 2. Phone -> Laptop text sync
        phone_text = "Greetings from phone, echo-free!"
        clip_phone.set_payload(text_payload(phone_text))

        deadline = time.monotonic() + 5.0
        applied_laptop = 0

        while time.monotonic() < deadline:
            stats_l = engine_laptop.poll_once()
            stats_p = engine_phone.poll_once()
            applied_laptop += stats_l.applied
            laptop_dropped_loop += stats_l.dropped_loop
            phone_dropped_loop += stats_p.dropped_loop
            if clip_laptop.payload == text_payload(phone_text):
                break
            time.sleep(0.01)

        assert clip_laptop.payload == text_payload(phone_text)
        assert applied_laptop >= 1
        assert laptop_dropped_loop == 0
        assert phone_dropped_loop == 0

        # 3. Stabilization and no unbounded resends
        summary_l_before = engine_laptop.state_summary()
        summary_p_before = engine_phone.state_summary()

        for _ in range(5):
            stats_l = engine_laptop.poll_once()
            stats_p = engine_phone.poll_once()
            assert stats_l.sent == 0
            assert stats_p.sent == 0
            assert stats_l.applied == 0
            assert stats_p.applied == 0
            assert stats_l.dropped_loop == 0
            assert stats_p.dropped_loop == 0
            time.sleep(0.01)

        summary_l_after = engine_laptop.state_summary()
        summary_p_after = engine_phone.state_summary()

        assert summary_l_after["last_seq"] == summary_l_before["last_seq"]
        assert summary_l_after["last_local_hash"] == summary_l_before["last_local_hash"]
        assert summary_l_after["outbox"] == 0
        assert summary_p_after["last_seq"] == summary_p_before["last_seq"]
        assert summary_p_after["last_local_hash"] == summary_p_before["last_local_hash"]
        assert summary_p_after["outbox"] == 0

        assert clip_laptop.payload == text_payload(phone_text)
        assert clip_phone.payload == text_payload(phone_text)

    finally:
        t_laptop.close()
        t_phone.close()
        server.stop()


def test_encrypted_image_passthrough() -> None:
    """Encrypted image payload passes through relay untouched; peer verifies bytes."""
    server = RelayServer(token=TOKEN, host=LOOPBACK, port=0)
    server.start()
    port = server.port

    codec_shared = FernetCodec.generate()
    codec_other = FernetCodec.generate()

    clip_laptop = FakeClipboard()
    clip_phone = FakeClipboard()
    clip_eavesdropper = FakeClipboard()

    t_laptop = RelayTransport(relay_url(port), TOKEN, reconnect_delay=0.05)
    t_phone = RelayTransport(relay_url(port), TOKEN, reconnect_delay=0.05)
    t_eavesdropper = RelayTransport(relay_url(port), TOKEN, reconnect_delay=0.05)

    try:
        t_laptop.connect()
        t_phone.connect()
        t_eavesdropper.connect()
        assert wait_until(
            lambda: t_laptop.connected and t_phone.connected and t_eavesdropper.connected,
            timeout=3.0,
        )

        engine_laptop = SyncEngine(
            "laptop", clip_laptop, t_laptop, codec=codec_shared, seq_base=0
        )
        engine_phone = SyncEngine(
            "phone", clip_phone, t_phone, codec=codec_shared, seq_base=0
        )
        engine_eavesdropper = SyncEngine(
            "eavesdropper",
            clip_eavesdropper,
            t_eavesdropper,
            codec=codec_other,
            seq_base=0,
        )

        # 64 KiB deterministic image payload
        image_bytes = bytes((i * 37 + 13) % 256 for i in range(64 * 1024))
        img_payload = image_payload(image_bytes, "image/png")
        clip_laptop.set_payload(img_payload)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            engine_laptop.poll_once()
            engine_phone.poll_once()
            engine_eavesdropper.poll_once()
            if (
                clip_phone.payload is not None
                and clip_phone.payload.data == image_bytes
            ):
                break
            time.sleep(0.01)

        # Authorized peer with matching codec received byte-identical image
        assert clip_phone.payload is not None
        assert clip_phone.payload.kind == "image"
        assert clip_phone.payload.mime == "image/png"
        assert clip_phone.payload.data == image_bytes
        assert clip_phone.payload.content_hash() == img_payload.content_hash()

        # Peer with different codec failed decryption and did not apply
        assert clip_eavesdropper.payload is None
        assert clip_eavesdropper.writes == []

        # Assert RelayServer was constructed without any codec/key attributes
        assert not hasattr(server, "codec")
        assert not hasattr(server, "_codec")
        assert not hasattr(server, "key")
        assert not hasattr(server, "_key")

    finally:
        t_laptop.close()
        t_phone.close()
        t_eavesdropper.close()
        server.stop()


def test_outbox_survives_relay_restart() -> None:
    """Outbox holds frame while relay is down; flushes exactly once on restart."""
    token = TOKEN
    server = RelayServer(token=token, host=LOOPBACK, port=0)
    server.start()
    port = server.port

    clip_laptop = FakeClipboard()
    clip_phone = FakeClipboard()

    t_laptop = RelayTransport(
        relay_url(port), token, reconnect=True, reconnect_delay=0.05
    )
    t_phone = RelayTransport(
        relay_url(port), token, reconnect=True, reconnect_delay=0.05
    )
    restarted: RelayServer | None = None

    try:
        t_laptop.connect()
        t_phone.connect()
        assert wait_until(lambda: t_laptop.connected and t_phone.connected, timeout=3.0)

        engine_laptop = SyncEngine("laptop", clip_laptop, t_laptop, seq_base=0)
        engine_phone = SyncEngine("phone", clip_phone, t_phone, seq_base=0)

        # 1. Stop the relay server
        server.stop()
        assert wait_until(
            lambda: not t_laptop.connected and not t_phone.connected, timeout=3.0
        )

        # 2. Update laptop clipboard and poll_once(): send fails, frame stays in outbox
        offline_text = "persisted in outbox while relay was down"
        clip_laptop.set_payload(text_payload(offline_text))

        poll_stats = engine_laptop.poll_once()
        assert poll_stats.send_errors >= 1
        assert engine_laptop.state_summary()["outbox"] >= 1

        # 3. Restart RelayServer on the same port (retry briefly on OSError)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                restarted = RelayServer(token=token, host=LOOPBACK, port=port)
                restarted.start()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

        # 4. Wait for transports to reconnect
        assert wait_until(lambda: t_laptop.connected and t_phone.connected, timeout=5.0)

        # 5. Poll until phone applies the text exactly once
        deadline = time.monotonic() + 5.0
        phone_applied = 0
        while time.monotonic() < deadline:
            stats_l = engine_laptop.poll_once()
            stats_p = engine_phone.poll_once()
            phone_applied += stats_p.applied
            if clip_phone.payload == text_payload(offline_text):
                break
            time.sleep(0.01)

        assert clip_phone.payload == text_payload(offline_text)
        assert phone_applied == 1

        # 6. Assert queued frames drained and no duplicate application
        assert engine_laptop.state_summary()["outbox"] == 0

        # Poll extra rounds: no resends, no duplicates
        for _ in range(5):
            stats_l = engine_laptop.poll_once()
            stats_p = engine_phone.poll_once()
            phone_applied += stats_p.applied
            assert stats_l.sent == 0
            time.sleep(0.01)

        assert phone_applied == 1
        assert len(clip_phone.writes) == 1
        assert engine_laptop.state_summary()["outbox"] == 0

    finally:
        t_laptop.close()
        t_phone.close()
        if restarted is not None:
            restarted.stop()
        server.stop()
