"""Wire-protocol module for clipsync.

This module defines the wire-protocol constants, exceptions, data models,
validation rules, content hashing, and wire serialization/deserialization
for untrusted wire data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

WIRE_VERSION: int = 1
MAX_TEXT_BYTES: int = 1_000_000  # 1 MB UTF-8
MAX_IMAGE_BYTES: int = 8 * 1024 * 1024  # 8 MiB
SUPPORTED_TEXT_MIMES: frozenset[str] = frozenset(
    {"text/plain", "text/html", "text/uri-list"}
)
SUPPORTED_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

__all__ = [
    "WIRE_VERSION",
    "MAX_TEXT_BYTES",
    "MAX_IMAGE_BYTES",
    "SUPPORTED_TEXT_MIMES",
    "SUPPORTED_IMAGE_MIMES",
    "ClipError",
    "ValidationError",
    "PayloadTooLarge",
    "UnsupportedMime",
    "ClipPayload",
    "ClipMessage",
    "text_payload",
    "image_payload",
    "canonical_json",
    "encode_frame",
    "decode_frame",
]


class ClipError(Exception):
    """Base exception for all clipsync errors."""


class ValidationError(ClipError):
    """Validation failed on a clip message or payload."""


class PayloadTooLarge(ValidationError):
    """Payload exceeds allowed size limits or is empty."""


class UnsupportedMime(ValidationError):
    """MIME type is not supported for the given payload kind."""


@dataclass(frozen=True, slots=True)
class ClipPayload:
    kind: Literal["text", "image"]
    mime: str
    text: str | None = None
    data: bytes | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("text", "image"):
            raise ValidationError(f"invalid payload kind: {self.kind!r}")

        if not isinstance(self.mime, str):
            raise ValidationError("mime must be a str")

        if self.kind == "text":
            if self.mime not in SUPPORTED_TEXT_MIMES:
                raise UnsupportedMime(f"unsupported text MIME: {self.mime!r}")
            if self.text is None:
                raise ValidationError("text payload requires text to be non-None")
            if self.data is not None:
                raise ValidationError("text payload must not have data")
            if not isinstance(self.text, str):
                raise ValidationError("text must be a str")
            encoded_len = len(self.text.encode("utf-8"))
            if encoded_len == 0:
                raise PayloadTooLarge("text payload cannot be empty")
            if encoded_len > MAX_TEXT_BYTES:
                raise PayloadTooLarge(
                    f"text payload size ({encoded_len} bytes) exceeds MAX_TEXT_BYTES ({MAX_TEXT_BYTES})"
                )
        else:  # kind == "image"
            if self.mime not in SUPPORTED_IMAGE_MIMES:
                raise UnsupportedMime(f"unsupported image MIME: {self.mime!r}")
            if self.data is None:
                raise ValidationError("image payload requires data to be non-None")
            if self.text is not None:
                raise ValidationError("image payload must not have text")
            if not isinstance(self.data, (bytes, bytearray, memoryview)):
                raise ValidationError("data must be bytes-like")
            coerced = bytes(self.data)
            object.__setattr__(self, "data", coerced)
            coerced_len = len(coerced)
            if coerced_len == 0:
                raise PayloadTooLarge("image payload cannot be empty")
            if coerced_len > MAX_IMAGE_BYTES:
                raise PayloadTooLarge(
                    f"image payload size ({coerced_len} bytes) exceeds MAX_IMAGE_BYTES ({MAX_IMAGE_BYTES})"
                )

    @property
    def size_bytes(self) -> int:
        if self.kind == "text":
            assert self.text is not None
            return len(self.text.encode("utf-8"))
        assert self.data is not None
        return len(self.data)

    def content_hash(self) -> str:
        body = self.text.encode("utf-8") if self.kind == "text" else self.data
        assert body is not None
        prefix = (
            b"clipsync/1\0"
            + self.kind.encode("ascii")
            + b"\0"
            + self.mime.encode("ascii")
            + b"\0"
        )
        return hashlib.sha256(prefix + body).hexdigest()

    def to_wire(self) -> dict[str, Any]:
        if self.kind == "text":
            return {
                "kind": self.kind,
                "mime": self.mime,
                "text": self.text,
            }
        assert self.data is not None
        return {
            "kind": self.kind,
            "mime": self.mime,
            "data_b64": base64.b64encode(self.data).decode("ascii"),
        }

    @classmethod
    def from_wire(cls, obj: Any) -> "ClipPayload":
        if not isinstance(obj, Mapping):
            raise ValidationError("payload must be a Mapping")
        if "kind" not in obj:
            raise ValidationError("payload missing 'kind'")
        kind = obj["kind"]
        if not isinstance(kind, str):
            raise ValidationError("payload 'kind' must be str")
        if "mime" not in obj:
            raise ValidationError("payload missing 'mime'")
        mime = obj["mime"]
        if not isinstance(mime, str):
            raise ValidationError("payload 'mime' must be str")

        if kind == "text":
            if "text" not in obj:
                raise ValidationError("text payload missing 'text'")
            if "data_b64" in obj and obj["data_b64"] is not None:
                raise ValidationError("text payload cannot contain 'data_b64'")
            text = obj["text"]
            if not isinstance(text, str):
                raise ValidationError("payload 'text' must be str")
            return cls(kind="text", mime=mime, text=text)
        elif kind == "image":
            if "data_b64" not in obj:
                raise ValidationError("image payload missing 'data_b64'")
            if "text" in obj and obj["text"] is not None:
                raise ValidationError("image payload cannot contain 'text'")
            data_b64 = obj["data_b64"]
            if not isinstance(data_b64, str):
                raise ValidationError("payload 'data_b64' must be str")
            try:
                data = base64.b64decode(data_b64.encode("ascii"), validate=True)
            except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
                raise ValidationError(f"malformed base64 image data: {exc}") from exc
            return cls(kind="image", mime=mime, data=data)
        else:
            raise ValidationError(f"unknown payload kind: {kind!r}")

    def __repr__(self) -> str:
        if self.kind == "text":
            assert self.text is not None
            sample = self.text if len(self.text) <= 40 else self.text[:37] + "..."
            return (
                f"ClipPayload(kind='text', mime={self.mime!r}, "
                f"size={self.size_bytes}, text={sample!r})"
            )
        assert self.data is not None
        sample = self.data[:16].hex() + ("..." if len(self.data) > 16 else "")
        return (
            f"ClipPayload(kind='image', mime={self.mime!r}, "
            f"size={self.size_bytes}, data=<{sample}>)"
        )


@dataclass(frozen=True, slots=True)
class ClipMessage:
    origin: str
    seq: int
    payload: ClipPayload
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    content_hash: str = ""
    wire_version: int = WIRE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.payload, ClipPayload):
            raise ValidationError("payload must be an instance of ClipPayload")

        if (
            not isinstance(self.origin, str)
            or len(self.origin) == 0
            or len(self.origin) > 128
        ):
            raise ValidationError("origin must be a non-empty str <= 128 chars")

        if (
            isinstance(self.seq, bool)
            or not isinstance(self.seq, int)
            or self.seq < 0
        ):
            raise ValidationError("seq must be an int >= 0")

        if (
            not isinstance(self.msg_id, str)
            or len(self.msg_id) != 32
            or not all(c in "0123456789abcdef" for c in self.msg_id)
        ):
            raise ValidationError("msg_id must be exactly 32 lowercase hex chars")

        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or math.isnan(self.created_at)
            or math.isinf(self.created_at)
            or self.created_at < 0
        ):
            raise ValidationError("created_at must be a real/int >= 0")

        if (
            isinstance(self.wire_version, bool)
            or not isinstance(self.wire_version, int)
            or self.wire_version != WIRE_VERSION
        ):
            raise ValidationError(f"wire_version must equal {WIRE_VERSION}")

        expected_hash = self.payload.content_hash()
        if self.content_hash != "":
            if (
                not isinstance(self.content_hash, str)
                or self.content_hash != expected_hash
            ):
                raise ValidationError(
                    f"content_hash mismatch: expected {expected_hash}, got {self.content_hash}"
                )
        object.__setattr__(self, "content_hash", expected_hash)

    def to_wire(self) -> dict[str, Any]:
        return {
            "v": self.wire_version,
            "id": self.msg_id,
            "origin": self.origin,
            "seq": self.seq,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
            "payload": self.payload.to_wire(),
        }

    @classmethod
    def from_wire(cls, obj: Any) -> "ClipMessage":
        if not isinstance(obj, Mapping):
            raise ValidationError("obj must be a Mapping")

        required_fields = (
            "v",
            "id",
            "origin",
            "seq",
            "created_at",
            "content_hash",
            "payload",
        )
        for f in required_fields:
            if f not in obj:
                raise ValidationError(f"missing field: {f!r}")

        wire_v = obj["v"]
        if (
            isinstance(wire_v, bool)
            or not isinstance(wire_v, int)
            or wire_v != WIRE_VERSION
        ):
            raise ValidationError(f"unsupported wire version: {wire_v!r}")

        msg_id = obj["id"]
        if (
            not isinstance(msg_id, str)
            or len(msg_id) != 32
            or not all(c in "0123456789abcdef" for c in msg_id)
        ):
            raise ValidationError(f"invalid msg_id: {msg_id!r}")

        origin = obj["origin"]
        if not isinstance(origin, str) or len(origin) == 0 or len(origin) > 128:
            raise ValidationError(f"invalid origin: {origin!r}")

        seq = obj["seq"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValidationError(f"invalid seq: {seq!r}")

        created_at = obj["created_at"]
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or math.isnan(created_at)
            or math.isinf(created_at)
            or created_at < 0
        ):
            raise ValidationError(f"invalid created_at: {created_at!r}")

        wire_hash = obj["content_hash"]
        if (
            not isinstance(wire_hash, str)
            or len(wire_hash) != 64
            or not all(c in "0123456789abcdef" for c in wire_hash)
        ):
            raise ValidationError(f"invalid content_hash: {wire_hash!r}")

        payload = ClipPayload.from_wire(obj["payload"])

        if payload.content_hash() != wire_hash:
            raise ValidationError(
                f"content_hash mismatch: computed {payload.content_hash()}, wire has {wire_hash}"
            )

        return cls(
            origin=origin,
            seq=seq,
            payload=payload,
            msg_id=msg_id,
            created_at=float(created_at),
            content_hash=wire_hash,
            wire_version=wire_v,
        )


def text_payload(text: str, mime: str = "text/plain") -> ClipPayload:
    return ClipPayload(kind="text", mime=mime, text=text)


def image_payload(data: bytes, mime: str) -> ClipPayload:
    return ClipPayload(kind="image", mime=mime, data=data)


def canonical_json(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def encode_frame(msg: ClipMessage) -> bytes:
    if not isinstance(msg, ClipMessage):
        raise ValidationError("msg must be a ClipMessage")
    return canonical_json(msg.to_wire())


def decode_frame(frame: Any) -> ClipMessage:
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise ValidationError("frame must be bytes-like")
    raw = bytes(frame)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"frame is not valid UTF-8: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"frame is not valid JSON: {exc}") from exc

    return ClipMessage.from_wire(data)
