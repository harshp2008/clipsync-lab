"""Message envelope and Hello structures for meshcore."""

from __future__ import annotations

from dataclasses import dataclass
import json
import struct
from typing import Any, Iterable, Mapping

CONTROL_HELLO = "hello"
CONTROL_ROSTER = "roster"
CONTROL_LEAVING = "leaving"

ENVELOPE_MAGIC = b"\x00mc2d\x00"


@dataclass(frozen=True)
class Hello:
    """Hello announcement message payload."""

    device_id: str
    name: str
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True
    group_id: str = "default"


def encode_hello(hello: Hello) -> dict[str, Any]:
    """Encode a Hello message into a JSON-serializable dictionary.

    device_id and name are always included.
    perm and temp_rank are only included when not None.
    will_hub is always included.
    group_id is only included when not 'default'.
    """
    data: dict[str, Any] = {
        "device_id": hello.device_id,
        "name": hello.name,
        "will_hub": hello.will_hub,
    }
    if hello.perm is not None:
        data["perm"] = hello.perm
    if hello.temp_rank is not None:
        data["temp_rank"] = hello.temp_rank
    if hello.group_id != "default":
        data["group_id"] = hello.group_id
    return data


def decode_hello(data: Mapping[str, Any]) -> Hello:
    """Decode a mapping into a Hello message.

    Absent perm or temp_rank defaults to None.
    Absent will_hub defaults to True.
    Absent or None group_id defaults to 'default'.
    """
    perm = data.get("perm")
    temp_rank = data.get("temp_rank")
    will_hub = data.get("will_hub")
    if will_hub is None:
        will_hub_val = True
    else:
        will_hub_val = bool(will_hub)

    group_id = data.get("group_id")
    if group_id is None or not str(group_id).strip():
        group_id_val = "default"
    else:
        group_id_val = str(group_id).strip()

    return Hello(
        device_id=str(data["device_id"]),
        name=str(data["name"]),
        perm=int(perm) if perm is not None else None,
        temp_rank=int(temp_rank) if temp_rank is not None else None,
        will_hub=will_hub_val,
        group_id=group_id_val,
    )


def encode_hello_message(hello: Hello) -> str:
    """Encode a Hello message as a compact JSON control text frame."""
    data = encode_hello(hello)
    data["type"] = CONTROL_HELLO
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def decode_control(text: str | Mapping[str, Any]) -> tuple[str, Any]:
    """Decode a control message from JSON text or an already-decoded mapping.

    Returns:
        (type, payload) tuple.
    Raises:
        ValueError on invalid JSON or unknown control type.
    """
    if isinstance(text, str):
        try:
            data = json.loads(text)
        except Exception as exc:
            raise ValueError(f"Invalid JSON control message: {exc}") from exc
    elif isinstance(text, Mapping):
        data = text
    else:
        raise ValueError(f"Expected str or Mapping, got {type(text).__name__}")

    if not isinstance(data, Mapping):
        raise ValueError("Decoded control message must be a mapping")

    msg_type = data.get("type")
    if not isinstance(msg_type, str):
        raise ValueError("Missing or invalid 'type' in control message")

    if msg_type == CONTROL_HELLO:
        return (CONTROL_HELLO, decode_hello(data))
    if msg_type == CONTROL_ROSTER:
        return (CONTROL_ROSTER, decode_roster(data))
    if msg_type == CONTROL_LEAVING:
        decode_leaving(data)
        return (CONTROL_LEAVING, True)

    raise ValueError(f"Unknown control message type: {msg_type!r}")


def encode_roster(hellos: Iterable[Hello]) -> str:
    """Encode an iterable of Hello instances as a compact JSON roster frame."""
    data = {
        "devices": [encode_hello(h) for h in hellos],
        "type": CONTROL_ROSTER,
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def decode_roster(text: str | Mapping[str, Any]) -> list[Hello]:
    """Decode a roster control message from JSON text or mapping.

    Preserves the order of devices in the message.
    """
    if isinstance(text, str):
        try:
            data = json.loads(text)
        except Exception as exc:
            raise ValueError(f"Invalid JSON roster message: {exc}") from exc
    elif isinstance(text, Mapping):
        data = text
    else:
        raise ValueError(f"Expected str or Mapping, got {type(text).__name__}")

    if not isinstance(data, Mapping):
        raise ValueError("Decoded roster must be a mapping")
    if data.get("type") != CONTROL_ROSTER:
        raise ValueError(f"Expected type {CONTROL_ROSTER!r}, got {data.get('type')!r}")
    devices = data.get("devices")
    if not isinstance(devices, list):
        raise ValueError("Roster 'devices' must be a list")
    return [decode_hello(item) for item in devices]


def encode_leaving() -> str:
    """Encode a leaving control message as a compact JSON frame."""
    return json.dumps({"type": CONTROL_LEAVING}, sort_keys=True, separators=(",", ":"))


def decode_leaving(text: str | Mapping[str, Any]) -> bool:
    """Decode and validate a leaving control message.

    Returns True if valid; raises ValueError otherwise.
    """
    if isinstance(text, str):
        try:
            data = json.loads(text)
        except Exception as exc:
            raise ValueError(f"Invalid JSON leaving message: {exc}") from exc
    elif isinstance(text, Mapping):
        data = text
    else:
        raise ValueError(f"Expected str or Mapping, got {type(text).__name__}")

    if not isinstance(data, Mapping):
        raise ValueError("Decoded leaving message must be a mapping")
    if data.get("type") != CONTROL_LEAVING:
        raise ValueError(f"Expected type {CONTROL_LEAVING!r}, got {data.get('type')!r}")
    return True


def wrap_data_envelope(sender_id: str | None, payload: bytes) -> bytes:
    """Wrap payload in binary data envelope with sender header.

    Format: b"\\x00mc2d\\x00" + 4-byte big-endian header length + JSON {"from": id|None} + payload
    """
    hdr = json.dumps({"from": sender_id}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ENVELOPE_MAGIC + struct.pack(">I", len(hdr)) + hdr + payload


def unwrap_data_envelope(frame: bytes) -> tuple[str | None, bytes]:
    """Unwrap binary frame, extracting sender id if data envelope is present.

    Returns:
        (sender_id, payload_bytes).
        If frame is a bare payload (no envelope), returns (None, frame).
    """
    magic_len = len(ENVELOPE_MAGIC)
    if len(frame) >= magic_len + 4 and frame.startswith(ENVELOPE_MAGIC):
        hdr_len = struct.unpack(">I", frame[magic_len : magic_len + 4])[0]
        start_payload = magic_len + 4 + hdr_len
        if len(frame) >= start_payload:
            try:
                hdr_bytes = frame[magic_len + 4 : start_payload]
                hdr = json.loads(hdr_bytes.decode("utf-8"))
                if isinstance(hdr, dict) and "from" in hdr:
                    sender_id = hdr["from"]
                    if sender_id is not None:
                        sender_id = str(sender_id)
                    return (sender_id, frame[start_payload:])
            except Exception:
                pass
    return (None, frame)

