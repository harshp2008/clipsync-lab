"""Unit tests for meshcore state channel and clipsync state transport."""

from __future__ import annotations

import time
import pytest

from clipsync.clipboard import FakeClipboard
from clipsync.config import DaemonConfig
from clipsync.engine import SyncEngine, SyncStats
from clipsync.state_transport import MeshStateTransport
from clipsync.transport import FrameTooLarge, TransportError
from meshcore.config import MeshConfig
from meshcore.coordination import (
    HLC,
    MAX_CLOCK_AHEAD_S,
    MAX_STATE_KEYS_PER_GROUP,
    MAX_STATE_PAYLOAD_BYTES,
    StateEntry,
    StateRegister,
    StateStats,
    StateVersion,
    is_valid_state_key,
    validate_state_key,
)
from meshcore.messages import (
    STATE_MAGIC,
    decode_state_frame,
    encode_state_frame,
    encode_state_sync,
)
from meshcore.node import Node, NodeError


# 1. Key validation
def test_key_validation() -> None:
    valid_keys = [
        "a",
        "0",
        "clipboard",
        "clip.board",
        "clip_board",
        "clip-board",
        "a" * 64,
        "x1.y_2-z",
    ]
    for key in valid_keys:
        assert is_valid_state_key(key)
        validate_state_key(key)

    invalid_keys = [
        "",
        "a" * 65,
        "Clipboard",
        "CLIP",
        "-leading",
        ".leading",
        "_leading",
        "has space",
        "has/slash",
        "has$symbol",
    ]
    for key in invalid_keys:
        assert not is_valid_state_key(key)
        with pytest.raises(ValueError):
            validate_state_key(key)


# 2. StateVersion total ordering
def test_state_version_total_ordering() -> None:
    v1 = StateVersion(1000, 0, "node-a")
    v2 = StateVersion(1000, 1, "node-a")
    v3 = StateVersion(1000, 1, "node-b")
    v4 = StateVersion(1001, 0, "node-a")

    assert v1 < v2 < v3 < v4
    assert v1 == StateVersion(1000, 0, "node-a")
    assert not (v2 < v1)

    with pytest.raises(ValueError):
        StateVersion(-1, 0, "node-a")
    with pytest.raises(ValueError):
        StateVersion(1000, -1, "node-a")
    with pytest.raises(ValueError):
        StateVersion(1000, 0, "")


# 3. State frame codec roundtrip
def test_state_frame_codec_roundtrip() -> None:
    version = StateVersion(1761713834123, 5, "device-42")
    payload = b"encrypted-fernet-token-or-raw-body"
    frame = encode_state_frame("clipboard", version, payload)

    assert frame.startswith(STATE_MAGIC)
    key, decoded_version, decoded_payload = decode_state_frame(frame)
    assert key == "clipboard"
    assert decoded_version == version
    assert decoded_payload == payload


# 4. State frame codec rejection edge cases
def test_state_frame_codec_rejection() -> None:
    version = StateVersion(1000, 0, "dev")
    valid_frame = encode_state_frame("clipboard", version, b"body")

    # Wrong magic
    with pytest.raises(ValueError, match="Invalid state magic"):
        decode_state_frame(b"\x00mc2d\x00" + valid_frame[6:])

    # Truncated frame
    with pytest.raises(ValueError):
        decode_state_frame(valid_frame[:8])

    # Oversized header
    fake_oversized_header = STATE_MAGIC + (5000).to_bytes(4, "big") + (b"x" * 5000)
    with pytest.raises(ValueError, match="exceeds 4 KiB"):
        decode_state_frame(fake_oversized_header)


# 5. State sync control frame encode
def test_state_sync_control_encode() -> None:
    msg = encode_state_sync()
    assert '"type":"state_sync"' in msg


# 6. HLC monotonicity and counter
def test_hlc_monotonicity_and_counter() -> None:
    current_time = 100.0

    def get_time() -> float:
        return current_time

    hlc = HLC("dev1", wall_clock=get_time)

    v1, corrected1 = hlc.publish()
    assert not corrected1
    assert v1.wall_ms == 100000
    assert v1.counter == 0
    assert v1.author == "dev1"

    # Same timestamp increment counter
    v2, corrected2 = hlc.publish()
    assert not corrected2
    assert v2.wall_ms == 100000
    assert v2.counter == 1

    # Advancing wall clock resets counter to 0
    current_time = 101.0
    v3, corrected3 = hlc.publish()
    assert not corrected3
    assert v3.wall_ms == 101000
    assert v3.counter == 0


# 7. HLC causality under lagging clock
def test_hlc_causality_under_lagging_clock() -> None:
    current_time = 100.0
    hlc = HLC("dev-behind", wall_clock=lambda: current_time)

    # Accept a version from another node ahead of dev-behind
    remote_version = StateVersion(105000, 2, "dev-ahead")
    accepted = hlc.accept(remote_version.wall_ms, remote_version.counter)
    assert accepted
    assert hlc.accepted_floor == (105000, 2)

    # Next publish from dev-behind must have wall >= 105000 and counter > 2
    local_version, corrected = hlc.publish()
    assert not corrected
    assert local_version.wall_ms == 105000
    assert local_version.counter == 3
    assert local_version.author == "dev-behind"
    assert local_version > remote_version


# 8. HLC clock correction
def test_hlc_clock_correction() -> None:
    current_time = 1000.0

    def get_time() -> float:
        return current_time

    hlc = HLC("dev", wall_clock=get_time)

    # Publish far in the future
    current_time = 2000.0  # +1000 seconds
    v_future, _ = hlc.publish()
    assert v_future.wall_ms == 2000000

    # Clock moves back by more than MAX_CLOCK_AHEAD_S (e.g. back to 1000.0)
    current_time = 1000.0
    v_corrected, corrected = hlc.publish()
    assert corrected
    assert v_corrected.wall_ms == 1000000
    assert v_corrected.counter == 0


# 9. HLC skew rejection and floor safety
def test_hlc_skew_rejection_and_floor_safety() -> None:
    current_time = 100.0
    hlc = HLC("dev", wall_clock=lambda: current_time)

    # +3600 seconds ahead: rejected
    far_ahead_wall_ms = int((current_time + 3600.0) * 1000)
    accepted = hlc.accept(far_ahead_wall_ms, 0)
    assert not accepted
    # Accepted floor must not move
    assert hlc.accepted_floor == (0, 0)
    assert hlc.last == (0, 0)


# 10. StateRegister bounds and LWW
def test_state_register_bounds_and_lww() -> None:
    reg = StateRegister(max_keys=MAX_STATE_KEYS_PER_GROUP)
    v1 = StateVersion(1000, 0, "n1")
    v2 = StateVersion(1001, 0, "n1")

    # Add 16 keys
    for i in range(16):
        key = f"key-{i}"
        assert reg.merge(key, v1, f"val-{i}".encode("ascii"))
    assert len(reg) == 16

    # 17th key refused
    assert not reg.merge("key-16", v1, b"overflow")
    assert len(reg) == 16

    # Update existing key succeeds with newer version
    assert reg.merge("key-0", v2, b"updated")
    assert reg.get_payload("key-0") == b"updated"

    # Losing version rejected
    assert not reg.merge("key-0", v1, b"old")
    assert reg.get_payload("key-0") == b"updated"


# 11. StateStats frozen snapshot
def test_state_stats_frozen() -> None:
    stats = StateStats()
    assert stats.published == 0
    assert stats.sent == 0
    assert stats.received == 0
    assert stats.applied == 0
    assert stats.ignored == 0
    assert stats.rejected_skew == 0
    assert stats.rejected_invalid == 0

    with pytest.raises(Exception):
        stats.published = 1  # frozen dataclass raises FrozenInstanceError


# 12. FrameTooLarge mapping in MeshStateTransport
def test_frame_too_large_mapping() -> None:
    config = MeshConfig(
        device_id="t1",
        name="T1",
        token="tok",
        bind="127.0.0.1",
        port=9999,
    )
    node = Node(config)
    transport = MeshStateTransport(node)

    # Oversized payload (> 8 MiB)
    oversized = b"a" * (MAX_STATE_PAYLOAD_BYTES + 1)
    with pytest.raises(FrameTooLarge):
        transport.send(oversized)


# 13. SyncEngine drops FrameTooLarge without stall
def test_sync_engine_handles_frame_too_large() -> None:
    config = MeshConfig(
        device_id="t2",
        name="T2",
        token="tok",
        bind="127.0.0.1",
        port=9998,
    )
    node = Node(config)
    transport = MeshStateTransport(node)
    clip = FakeClipboard()
    engine = SyncEngine("t2", clip, transport)

    oversized = b"x" * (MAX_STATE_PAYLOAD_BYTES + 1)
    # Queue oversized in outbox directly
    engine._outbox.append(oversized)

    stats = SyncStats()
    engine.flush_outbox(stats)

    assert stats.rejected_too_large == 1
    assert stats.send_errors == 0
    assert len(engine._outbox) == 0  # dropped from outbox, not stalled


# 14. Daemon mesh wiring selects MeshStateTransport and codec=None
def test_daemon_mesh_wiring_selection(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from clipsync.daemon import main

    cfg_file = tmp_path / "mesh.toml"
    cfg_file.write_text(
        'device_id = "test-node"\n'
        'group_id = "test-group"\n'
        'token = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        'e2e_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n'
        'clipboard_backend = "file"\n'
        f'clipboard_file = "{tmp_path / "clip.txt"}"\n'
        '\n'
        '[[peers]]\n'
        'device_id = "peer1"\n'
        'name = "Peer 1"\n'
        'address = "127.0.0.1:8765"\n'
        'perm = 1\n'
    )

    captured: list[tuple[object, object]] = []

    def fake_daemon_init(self, config, *, clipboard, transport, codec=None, backoff=None):
        captured.append((transport, codec))
        self._config = config
        self._clipboard = clipboard
        self._transport = transport
        self._codec = codec

    monkeypatch.setattr("clipsync.daemon.ClipboardDaemon.__init__", fake_daemon_init)
    monkeypatch.setattr("clipsync.daemon.ClipboardDaemon.connect", lambda self: None)
    monkeypatch.setattr("clipsync.daemon.ClipboardDaemon.poll_once", lambda self: SyncStats())
    monkeypatch.setattr("clipsync.daemon.ClipboardDaemon.close", lambda self: None)

    ret = main(["--config", str(cfg_file), "--once"])
    assert ret == 0
    assert len(captured) == 1
    transport, codec = captured[0]
    assert isinstance(transport, MeshStateTransport)
    assert codec is None
