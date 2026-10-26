"""Tests for clipsync wire protocol module."""

from __future__ import annotations

import base64
import hashlib
import json
import pytest

from clipsync.protocol import (
    MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
    SUPPORTED_IMAGE_MIMES,
    SUPPORTED_TEXT_MIMES,
    WIRE_VERSION,
    ClipError,
    ClipMessage,
    ClipPayload,
    PayloadTooLarge,
    UnsupportedMime,
    ValidationError,
    canonical_json,
    decode_frame,
    encode_frame,
    image_payload,
    text_payload,
)


def test_exception_hierarchy() -> None:
    """Check that exception hierarchy matches specifications."""
    assert issubclass(ValidationError, ClipError)
    assert issubclass(PayloadTooLarge, ValidationError)
    assert issubclass(UnsupportedMime, ValidationError)


# -- ClipPayload tests --------------------------------------------------------


def test_valid_text_payload_and_roundtrip() -> None:
    p = text_payload("hello world", mime="text/plain")
    assert p.kind == "text"
    assert p.mime == "text/plain"
    assert p.text == "hello world"
    assert p.data is None
    assert p.size_bytes == 11

    wire = p.to_wire()
    assert wire == {
        "kind": "text",
        "mime": "text/plain",
        "text": "hello world",
    }
    p_decoded = ClipPayload.from_wire(wire)
    assert p_decoded == p

    # Unicode text
    unicode_p = text_payload("こんにちは", mime="text/html")
    assert unicode_p.size_bytes == len("こんにちは".encode("utf-8"))
    assert ClipPayload.from_wire(unicode_p.to_wire()) == unicode_p


def test_valid_image_payload_and_roundtrip() -> None:
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    p = image_payload(raw, "image/png")
    assert p.kind == "image"
    assert p.mime == "image/png"
    assert p.data == raw
    assert p.text is None
    assert p.size_bytes == len(raw)

    wire = p.to_wire()
    assert wire == {
        "kind": "image",
        "mime": "image/png",
        "data_b64": base64.b64encode(raw).decode("ascii"),
    }
    p_decoded = ClipPayload.from_wire(wire)
    assert p_decoded == p

    # Test coercion from bytearray and memoryview
    p_ba = ClipPayload(kind="image", mime="image/png", data=bytearray(raw))
    assert type(p_ba.data) is bytes
    assert p_ba.data == raw

    p_mv = ClipPayload(kind="image", mime="image/png", data=memoryview(raw))
    assert type(p_mv.data) is bytes
    assert p_mv.data == raw


def test_empty_payload_rejected() -> None:
    with pytest.raises(PayloadTooLarge):
        text_payload("")

    with pytest.raises(PayloadTooLarge):
        image_payload(b"", "image/png")

    with pytest.raises(PayloadTooLarge):
        ClipPayload.from_wire({"kind": "text", "mime": "text/plain", "text": ""})

    with pytest.raises(PayloadTooLarge):
        ClipPayload.from_wire({"kind": "image", "mime": "image/png", "data_b64": ""})


def test_oversized_payload_rejected() -> None:
    # 1 MB + 1 byte for text
    oversized_text = "a" * (MAX_TEXT_BYTES + 1)
    with pytest.raises(PayloadTooLarge):
        text_payload(oversized_text)

    # Max allowed text size succeeds
    exact_text = "a" * MAX_TEXT_BYTES
    p_exact = text_payload(exact_text)
    assert p_exact.size_bytes == MAX_TEXT_BYTES

    # 8 MiB + 1 byte for image
    oversized_image = b"x" * (MAX_IMAGE_BYTES + 1)
    with pytest.raises(PayloadTooLarge):
        image_payload(oversized_image, "image/png")


def test_unsupported_mime() -> None:
    # Text kinds with invalid mimes
    with pytest.raises(UnsupportedMime):
        text_payload("hello", mime="application/json")

    with pytest.raises(UnsupportedMime):
        text_payload("hello", mime="image/png")

    # Image kinds with invalid mimes
    with pytest.raises(UnsupportedMime):
        image_payload(b"fake", mime="text/plain")

    with pytest.raises(UnsupportedMime):
        image_payload(b"fake", mime="image/tiff")

    # Verify all supported mimes work
    for mime in SUPPORTED_TEXT_MIMES:
        p = text_payload("content", mime=mime)
        assert p.mime == mime

    for mime in SUPPORTED_IMAGE_MIMES:
        p = image_payload(b"content", mime=mime)
        assert p.mime == mime


def test_kind_text_data_mismatch() -> None:
    # Text kind with data
    with pytest.raises(ValidationError):
        ClipPayload(kind="text", mime="text/plain", text="hello", data=b"unexpected")

    # Text kind without text
    with pytest.raises(ValidationError):
        ClipPayload(kind="text", mime="text/plain", text=None)

    # Image kind with text
    with pytest.raises(ValidationError):
        ClipPayload(kind="image", mime="image/png", data=b"img", text="unexpected")

    # Image kind without data
    with pytest.raises(ValidationError):
        ClipPayload(kind="image", mime="image/png", data=None)

    # Unknown kind
    with pytest.raises(ValidationError):
        ClipPayload(kind="audio", mime="audio/mp3", data=b"sound")  # type: ignore[arg-type]

    # Non-string text
    with pytest.raises(ValidationError):
        ClipPayload(kind="text", mime="text/plain", text=123)  # type: ignore[arg-type]

    # Non-bytes data
    with pytest.raises(ValidationError):
        ClipPayload(kind="image", mime="image/png", data="not bytes")  # type: ignore[arg-type]


def test_payload_from_wire_invalid_inputs() -> None:
    with pytest.raises(ValidationError):
        ClipPayload.from_wire("not a mapping")

    with pytest.raises(ValidationError):
        ClipPayload.from_wire({"kind": "text"})  # missing mime and text

    with pytest.raises(ValidationError):
        ClipPayload.from_wire({"mime": "text/plain"})  # missing kind

    # Text payload containing data_b64
    with pytest.raises(ValidationError):
        ClipPayload.from_wire(
            {"kind": "text", "mime": "text/plain", "text": "hello", "data_b64": "aGVsbG8="}
        )

    # Image payload containing text
    with pytest.raises(ValidationError):
        ClipPayload.from_wire(
            {"kind": "image", "mime": "image/png", "data_b64": "aGVsbG8=", "text": "hello"}
        )

    # Malformed base64
    with pytest.raises(ValidationError):
        ClipPayload.from_wire({"kind": "image", "mime": "image/png", "data_b64": "invalid!b64"})


def test_payload_repr_truncation() -> None:
    long_text = "A" * 1000
    p_text = text_payload(long_text)
    rep_text = repr(p_text)
    assert len(rep_text) < 120
    assert "..." in rep_text
    assert long_text not in rep_text

    long_data = b"\xaa" * 1000
    p_img = image_payload(long_data, "image/png")
    rep_img = repr(p_img)
    assert len(rep_img) < 120
    assert "..." in rep_img
    assert len(rep_img) < len(long_data)


def test_hash_determinism_and_sensitivity() -> None:
    p1 = text_payload("identical content", "text/plain")
    p2 = text_payload("identical content", "text/plain")
    assert p1.content_hash() == p2.content_hash()

    # Changing body changes hash
    p3 = text_payload("different content", "text/plain")
    assert p1.content_hash() != p3.content_hash()

    # Changing mime changes hash
    p4 = text_payload("identical content", "text/html")
    assert p1.content_hash() != p4.content_hash()

    # Changing kind changes hash even if raw body bytes match
    p_text_same_bytes = text_payload("bytes", "text/plain")
    p_img_same_bytes = image_payload(b"bytes", "image/png")
    assert p_text_same_bytes.content_hash() != p_img_same_bytes.content_hash()

    # Verify expected formula
    body = "identical content".encode("utf-8")
    expected_hash = hashlib.sha256(b"clipsync/1\0text\0text/plain\0" + body).hexdigest()
    assert p1.content_hash() == expected_hash


# -- ClipMessage tests --------------------------------------------------------


def test_message_wire_roundtrip_and_defaults() -> None:
    payload = text_payload("sample clip")
    msg = ClipMessage(origin="laptop-1", seq=42, payload=payload)

    assert msg.wire_version == WIRE_VERSION
    assert len(msg.msg_id) == 32
    assert msg.created_at > 0
    assert msg.content_hash == payload.content_hash()

    wire = msg.to_wire()
    assert set(wire.keys()) == {
        "v",
        "id",
        "origin",
        "seq",
        "created_at",
        "content_hash",
        "payload",
    }
    assert wire["v"] == WIRE_VERSION
    assert wire["origin"] == "laptop-1"
    assert wire["seq"] == 42
    assert wire["content_hash"] == payload.content_hash()

    msg_decoded = ClipMessage.from_wire(wire)
    assert msg_decoded == msg

    # Image message wire roundtrip
    img_payload = image_payload(b"\x89PNG\r\n\x1a\nfake", "image/png")
    img_msg = ClipMessage(origin="phone-1", seq=1, payload=img_payload)
    img_wire = img_msg.to_wire()
    img_decoded = ClipMessage.from_wire(img_wire)
    assert img_decoded == img_msg


def test_message_unknown_keys_ignored() -> None:
    payload = text_payload("ignore extra keys")
    msg = ClipMessage(origin="dev-1", seq=0, payload=payload)
    wire = msg.to_wire()
    wire["future_field"] = "something"
    wire["another_unknown"] = 12345

    decoded = ClipMessage.from_wire(wire)
    assert decoded == msg


def test_content_hash_is_over_payload_only() -> None:
    payload = text_payload("shared payload")
    msg1 = ClipMessage(origin="device-a", seq=1, payload=payload)
    msg2 = ClipMessage(origin="device-b", seq=999, payload=payload)

    assert msg1.msg_id != msg2.msg_id
    assert msg1.origin != msg2.origin
    assert msg1.seq != msg2.seq
    assert msg1.content_hash == msg2.content_hash
    assert msg1.content_hash == payload.content_hash()


def test_message_validation_bad_version() -> None:
    payload = text_payload("test")
    msg = ClipMessage(origin="dev", seq=0, payload=payload)
    wire = msg.to_wire()

    wire["v"] = 999
    with pytest.raises(ValidationError):
        ClipMessage.from_wire(wire)

    wire["v"] = 1.0  # float is not valid int wire version
    with pytest.raises(ValidationError):
        ClipMessage.from_wire(wire)

    wire["v"] = True
    with pytest.raises(ValidationError):
        ClipMessage.from_wire(wire)

    with pytest.raises(ValidationError):
        ClipMessage(origin="dev", seq=0, payload=payload, wire_version=2)


def test_message_validation_bad_content_hash() -> None:
    payload = text_payload("test")
    msg = ClipMessage(origin="dev", seq=0, payload=payload)
    wire = msg.to_wire()

    # Wire with tampered content_hash
    wire["content_hash"] = "0" * 64
    with pytest.raises(ValidationError):
        ClipMessage.from_wire(wire)

    # Disagreeing hash during ClipMessage construction
    with pytest.raises(ValidationError):
        ClipMessage(origin="dev", seq=0, payload=payload, content_hash="f" * 64)


def test_message_validation_malformed_base64() -> None:
    payload = image_payload(b"abc", "image/png")
    msg = ClipMessage(origin="dev", seq=0, payload=payload)
    wire = msg.to_wire()

    wire["payload"]["data_b64"] = "not base64!"
    with pytest.raises(ValidationError):
        ClipMessage.from_wire(wire)


def test_message_validation_bad_msg_id() -> None:
    payload = text_payload("test")
    msg = ClipMessage(origin="dev", seq=0, payload=payload)
    wire = msg.to_wire()

    for bad_id in ["", "short", "a" * 31, "a" * 33, "G" * 32, "A" * 32, 12345]:
        wire["id"] = bad_id
        with pytest.raises(ValidationError):
            ClipMessage.from_wire(wire)

        with pytest.raises(ValidationError):
            ClipMessage(origin="dev", seq=0, payload=payload, msg_id=bad_id)  # type: ignore[arg-type]


def test_message_validation_bad_origin() -> None:
    payload = text_payload("test")
    msg = ClipMessage(origin="dev", seq=0, payload=payload)
    wire = msg.to_wire()

    for bad_origin in ["", "x" * 129, 123, True]:
        wire["origin"] = bad_origin
        with pytest.raises(ValidationError):
            ClipMessage.from_wire(wire)

        with pytest.raises(ValidationError):
            ClipMessage(origin=bad_origin, seq=0, payload=payload)  # type: ignore[arg-type]


def test_message_validation_bad_seq() -> None:
    payload = text_payload("test")
    msg = ClipMessage(origin="dev", seq=0, payload=payload)
    wire = msg.to_wire()

    for bad_seq in [-1, -100, True, False, 1.5, "0"]:
        wire["seq"] = bad_seq
        with pytest.raises(ValidationError):
            ClipMessage.from_wire(wire)

        with pytest.raises(ValidationError):
            ClipMessage(origin="dev", seq=bad_seq, payload=payload)  # type: ignore[arg-type]


def test_message_validation_bad_created_at() -> None:
    payload = text_payload("test")
    msg = ClipMessage(origin="dev", seq=0, payload=payload)
    wire = msg.to_wire()

    for bad_ts in [-1.0, float("nan"), float("inf"), float("-inf"), True, "123"]:
        wire["created_at"] = bad_ts
        with pytest.raises(ValidationError):
            ClipMessage.from_wire(wire)

        with pytest.raises(ValidationError):
            ClipMessage(origin="dev", seq=0, payload=payload, created_at=bad_ts)  # type: ignore[arg-type]


def test_message_validation_non_mapping_and_missing_fields() -> None:
    with pytest.raises(ValidationError):
        ClipMessage.from_wire("not a dict")

    with pytest.raises(ValidationError):
        ClipMessage.from_wire([1, 2, 3])

    with pytest.raises(ValidationError):
        ClipMessage.from_wire(None)

    payload = text_payload("test")
    valid_wire = ClipMessage(origin="dev", seq=0, payload=payload).to_wire()

    for key in ("v", "id", "origin", "seq", "created_at", "content_hash", "payload"):
        wire_missing = dict(valid_wire)
        del wire_missing[key]
        with pytest.raises(ValidationError):
            ClipMessage.from_wire(wire_missing)


# -- Codecs and helpers tests -------------------------------------------------


def test_canonical_json_determinism() -> None:
    d1 = {"b": 1, "a": 2, "c": {"y": 3, "x": 4}}
    d2 = {"a": 2, "c": {"x": 4, "y": 3}, "b": 1}
    assert canonical_json(d1) == canonical_json(d2)
    assert canonical_json(d1) == b'{"a":2,"b":1,"c":{"x":4,"y":3}}'

    # Ensure non-ascii is encoded directly as UTF-8 without \u escapes
    d_unicode = {"text": "ümläut / 日本語"}
    json_bytes = canonical_json(d_unicode)
    assert json_bytes == '{"text":"ümläut / 日本語"}'.encode("utf-8")
    assert b"\\u" not in json_bytes


def test_encode_and_decode_frame_roundtrip() -> None:
    payload = text_payload("test frame")
    msg = ClipMessage(origin="laptop", seq=7, payload=payload)

    frame = encode_frame(msg)
    assert isinstance(frame, bytes)

    decoded = decode_frame(frame)
    assert decoded == msg

    # decode_frame accepts bytearray and memoryview
    assert decode_frame(bytearray(frame)) == msg
    assert decode_frame(memoryview(frame)) == msg

    with pytest.raises(ValidationError):
        encode_frame("not a message")  # type: ignore[arg-type]


def test_decode_frame_malformed() -> None:
    with pytest.raises(ValidationError):
        decode_frame("not bytes")

    with pytest.raises(ValidationError):
        decode_frame(b"not json at all")

    with pytest.raises(ValidationError):
        decode_frame(b"\xff\xfe\x00\x00")

    with pytest.raises(ValidationError):
        decode_frame(b"42")

    with pytest.raises(ValidationError):
        decode_frame(b"null")

    with pytest.raises(ValidationError):
        decode_frame(b"[]")
