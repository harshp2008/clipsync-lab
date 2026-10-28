"""Unit tests for meshcore phase 2a modules and contracts."""

from __future__ import annotations

import json
import pytest

from meshcore.config import (
    DEFAULT_MAX_FRAME_BYTES,
    MeshConfig,
    PeerConfig,
    format_ws_url,
)
from meshcore.messages import (
    CONTROL_HELLO,
    CONTROL_LEAVING,
    CONTROL_ROSTER,
    ENVELOPE_MAGIC,
    Hello,
    decode_control,
    decode_leaving,
    decode_roster,
    encode_hello,
    encode_hello_message,
    encode_leaving,
    encode_roster,
    unwrap_data_envelope,
    wrap_data_envelope,
)


def test_default_max_frame_bytes() -> None:
    expected = 4 * ((8 * 1024 * 1024 + 2) // 3) + 8192
    assert DEFAULT_MAX_FRAME_BYTES == expected
    assert DEFAULT_MAX_FRAME_BYTES == 11193004


def test_format_ws_url() -> None:
    assert format_ws_url("127.0.0.1:8701") == "ws://127.0.0.1:8701/"
    assert format_ws_url("ws://127.0.0.1:8701") == "ws://127.0.0.1:8701/"
    assert format_ws_url("ws://127.0.0.1:8701/") == "ws://127.0.0.1:8701/"
    assert format_ws_url("wss://secure.example.com") == "wss://secure.example.com/"
    assert format_ws_url("wss://secure.example.com/custom") == "wss://secure.example.com/custom"

    with pytest.raises(ValueError):
        format_ws_url("")
    with pytest.raises(ValueError):
        format_ws_url("   ")


def test_peer_config_validation() -> None:
    peer = PeerConfig(device_id="n1", name="Node 1", address="127.0.0.1:8000")
    assert peer.device_id == "n1"
    assert peer.name == "Node 1"
    assert peer.perm is None
    assert peer.temp_rank is None
    assert peer.will_hub is True

    with pytest.raises(ValueError):
        PeerConfig(device_id="", name="N", address="127.0.0.1:8000")
    with pytest.raises(ValueError):
        PeerConfig(device_id="n1", name="", address="127.0.0.1:8000")
    with pytest.raises(ValueError):
        PeerConfig(device_id="n1", name="N", address="")
    with pytest.raises(ValueError):
        PeerConfig(device_id="n1", name="N", address="127.0.0.1:8000", perm=-1)
    with pytest.raises(ValueError):
        PeerConfig(device_id="n1", name="N", address="127.0.0.1:8000", perm=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PeerConfig(device_id="n1", name="N", address="127.0.0.1:8000", temp_rank=-1)
    with pytest.raises(ValueError):
        PeerConfig(device_id="n1", name="N", address="127.0.0.1:8000", will_hub="yes")  # type: ignore[arg-type]


def test_mesh_config_validation() -> None:
    cfg = MeshConfig(
        device_id="self",
        name="SelfNode",
        token="secret",
    )
    assert cfg.bind == "127.0.0.1"
    assert cfg.port == 8765
    assert cfg.peers == ()
    assert cfg.max_frame_bytes == DEFAULT_MAX_FRAME_BYTES

    hello = cfg.hello()
    assert hello == Hello("self", "SelfNode", perm=None, temp_rank=None, will_hub=True)

    with pytest.raises(ValueError):
        MeshConfig(device_id="", name="N", token="T")
    with pytest.raises(ValueError):
        MeshConfig(device_id="S", name="", token="T")
    with pytest.raises(ValueError):
        MeshConfig(device_id="S", name="N", token="")
    with pytest.raises(ValueError):
        MeshConfig(device_id="S", name="N", token="T", port=-1)
    with pytest.raises(ValueError):
        MeshConfig(device_id="S", name="N", token="T", port=70000)
    with pytest.raises(ValueError):
        MeshConfig(device_id="S", name="N", token="T", heartbeat=0.0)
    with pytest.raises(ValueError):
        MeshConfig(device_id="S", name="N", token="T", missed_pongs=0)


def test_message_codecs() -> None:
    h = Hello("d1", "Dev1", perm=1, temp_rank=2, will_hub=False)
    encoded_hello = encode_hello_message(h)
    msg_type, decoded_h = decode_control(encoded_hello)
    assert msg_type == CONTROL_HELLO
    assert decoded_h == h

    roster = [h, Hello("d2", "Dev2")]
    encoded_roster = encode_roster(roster)
    msg_type, decoded_r = decode_control(encoded_roster)
    assert msg_type == CONTROL_ROSTER
    assert decoded_r == roster
    assert decode_roster(encoded_roster) == roster

    encoded_l = encode_leaving()
    msg_type, decoded_l = decode_control(encoded_l)
    assert msg_type == CONTROL_LEAVING
    assert decoded_l is True
    assert decode_leaving(encoded_l) is True

    with pytest.raises(ValueError):
        decode_control('{"type": "unknown_type"}')
    with pytest.raises(ValueError):
        decode_control("not valid json")
    with pytest.raises(ValueError):
        decode_roster('{"type": "not_roster"}')
    with pytest.raises(ValueError):
        decode_leaving('{"type": "not_leaving"}')


def test_data_envelope() -> None:
    payload = b"hello \x00 binary world"
    wrapped = wrap_data_envelope("n1", payload)
    assert wrapped.startswith(ENVELOPE_MAGIC)

    sender_id, unwrapped_payload = unwrap_data_envelope(wrapped)
    assert sender_id == "n1"
    assert unwrapped_payload == payload

    # Envelope with None sender
    wrapped_none = wrap_data_envelope(None, payload)
    sender_none, unwrapped_none = unwrap_data_envelope(wrapped_none)
    assert sender_none is None
    assert unwrapped_none == payload

    # Bare payload (no envelope)
    bare = b"bare payload without envelope"
    bare_sender, bare_payload = unwrap_data_envelope(bare)
    assert bare_sender is None
    assert bare_payload == bare

    # Malformed envelope recovers bare
    corrupt = ENVELOPE_MAGIC + b"\x00\x00\x00\x05junk"
    corrupt_sender, corrupt_payload = unwrap_data_envelope(corrupt)
    assert corrupt_sender is None
    assert corrupt_payload == corrupt
