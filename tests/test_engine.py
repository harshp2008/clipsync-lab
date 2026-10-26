"""End-to-end sync-engine tests: loops, dedup, ordering, reconnect, encryption.

Everything uses fake clipboard backends and in-memory transports.  No phone, no
network, no real clipboard.
"""

from __future__ import annotations

import pytest

from clipsync.clipboard import FakeClipboard
from clipsync.crypto import FernetCodec
from clipsync.engine import SyncEngine
from clipsync.protocol import (
    ClipMessage,
    PayloadTooLarge,
    encode_frame,
    image_payload,
    text_payload,
)
from clipsync.transport import make_pair

PNG = b"\x89PNG\r\n\x1a\n" + b"\xab" * 64


def make_pair_of_engines(*, codec=None, seq_base: int = 0):
    clip_a, clip_b = FakeClipboard(), FakeClipboard()
    ta, tb = make_pair()
    a = SyncEngine("laptop", clip_a, ta, codec=codec, seq_base=seq_base)
    b = SyncEngine("phone", clip_b, tb, codec=codec, seq_base=seq_base)
    return a, b, clip_a, clip_b, ta, tb


def pump(a: SyncEngine, b: SyncEngine, rounds: int = 3) -> None:
    for _ in range(rounds):
        a.poll_once()
        b.poll_once()


def test_text_both_directions_without_echo() -> None:
    a, b, clip_a, clip_b, _, _ = make_pair_of_engines()

    clip_a.set_payload(text_payload("from laptop"))
    pump(a, b)
    assert clip_b.payload == text_payload("from laptop")

    clip_b.set_payload(text_payload("from phone"))
    pump(a, b)
    assert clip_a.payload == text_payload("from phone")

    # Nothing should be bouncing back: both outboxes stay empty on further polls.
    for _ in range(5):
        assert a.poll_once().sent == 0
        assert b.poll_once().sent == 0
    assert a.state_summary()["outbox"] == 0
    assert b.state_summary()["outbox"] == 0


def test_echo_loop_is_bounded() -> None:
    a, b, clip_a, _, _, _ = make_pair_of_engines()
    clip_a.set_payload(text_payload("loop me"))
    applied = 0
    for _ in range(10):
        applied += a.poll_once().applied
        applied += b.poll_once().applied
    assert applied == 1, "remote payload must be applied exactly once"


def test_image_roundtrip() -> None:
    a, b, clip_a, clip_b, _, _ = make_pair_of_engines()
    clip_a.set_payload(image_payload(PNG, "image/png"))
    pump(a, b)
    assert clip_b.payload is not None
    assert clip_b.payload.kind == "image"
    assert clip_b.payload.mime == "image/png"
    assert clip_b.payload.data == PNG


def test_large_image_roundtrip() -> None:
    big = PNG + b"\x00" * (2 * 1024 * 1024)  # 2 MiB, under the 8 MiB cap
    a, b, clip_a, clip_b, _, _ = make_pair_of_engines()
    clip_a.set_payload(image_payload(big, "image/png"))
    pump(a, b)
    assert clip_b.payload is not None and clip_b.payload.data == big


def test_duplicate_frame_applied_once() -> None:
    a, _, clip_a, clip_b, _, tb = make_pair_of_engines()
    msg = ClipMessage(origin="phone", seq=1, payload=text_payload("dup"))
    frame = encode_frame(msg)
    tb.send(frame)
    tb.send(frame)
    stats = a.poll_once()
    assert stats.applied == 1
    assert len(clip_a.writes) == 1
    assert stats.dropped_stale == 1  # second copy has a non-increasing seq


def test_repeated_content_is_not_rewritten() -> None:
    a, _, clip_a, _, _, tb = make_pair_of_engines()
    tb.send(encode_frame(ClipMessage(origin="phone", seq=1, payload=text_payload("same"))))
    a.poll_once()
    tb.send(encode_frame(ClipMessage(origin="phone", seq=2, payload=text_payload("same"))))
    stats = a.poll_once()
    assert stats.applied == 0
    assert stats.dropped_dup == 1
    assert len(clip_a.writes) == 1


def test_recopied_content_applies_again_after_a_change() -> None:
    a, _, clip_a, _, _, tb = make_pair_of_engines()
    tb.send(encode_frame(ClipMessage(origin="phone", seq=1, payload=text_payload("A"))))
    a.poll_once()
    tb.send(encode_frame(ClipMessage(origin="phone", seq=2, payload=text_payload("B"))))
    a.poll_once()
    tb.send(encode_frame(ClipMessage(origin="phone", seq=3, payload=text_payload("A"))))
    a.poll_once()
    assert [w.text for w in clip_a.writes] == ["A", "B", "A"]


def test_stale_and_out_of_order_seq_never_regress_clipboard() -> None:
    a, _, clip_a, _, _, tb = make_pair_of_engines()
    for seq, text in ((5, "five"), (3, "three"), (4, "four"), (6, "six")):
        tb.send(encode_frame(ClipMessage(origin="phone", seq=seq, payload=text_payload(text))))
    stats = a.poll_once()
    assert stats.applied == 2
    assert stats.dropped_stale == 2
    assert [w.text for w in clip_a.writes] == ["five", "six"]
    assert a.state_summary()["last_seq"]["phone"] == 6


def test_gap_is_counted_but_applied() -> None:
    a, _, clip_a, _, _, tb = make_pair_of_engines()
    tb.send(encode_frame(ClipMessage(origin="phone", seq=1, payload=text_payload("one"))))
    a.poll_once()
    tb.send(encode_frame(ClipMessage(origin="phone", seq=3, payload=text_payload("three"))))
    stats = a.poll_once()
    assert stats.applied == 1
    assert a.state_summary()["seq_gaps"] == 1
    assert [w.text for w in clip_a.writes] == ["one", "three"]


def test_own_frames_are_dropped_as_loop() -> None:
    a, _, clip_a, _, _, tb = make_pair_of_engines()
    msg = ClipMessage(origin="laptop", seq=1, payload=text_payload("mine"))
    tb.send(encode_frame(msg))
    stats = a.poll_once()
    assert stats.dropped_loop == 1
    assert clip_a.writes == []


def test_reconnect_flushes_queued_frame_once() -> None:
    a, b, clip_a, clip_b, ta, _ = make_pair_of_engines()
    ta.disconnect()
    clip_a.set_payload(text_payload("while offline"))
    stats = a.poll_once()
    assert stats.send_errors >= 1
    assert stats.queued == 1

    ta.reconnect()
    stats = a.poll_once()
    assert stats.sent == 1
    assert stats.queued == 0
    b.poll_once()
    assert clip_b.payload == text_payload("while offline")

    # A second reconnect must not replay the already-sent frame.
    ta.disconnect()
    ta.reconnect()
    assert a.poll_once().sent == 0
    assert b.poll_once().applied == 0


def test_outbox_overflow_is_bounded_and_counted() -> None:
    clip_a = FakeClipboard()
    ta, _tb = make_pair()
    ta.disconnect()
    a = SyncEngine("laptop", clip_a, ta, seq_base=0, max_outbox=2)
    for i in range(4):
        clip_a.set_payload(text_payload(f"m{i}"))
        a.poll_once()
    summary = a.state_summary()
    assert summary["outbox"] == 2
    assert summary["overflow_dropped"] == 2


def test_encrypted_engines_sync() -> None:
    codec = FernetCodec.generate()
    a, b, clip_a, clip_b, _, _ = make_pair_of_engines(codec=codec)
    clip_a.set_payload(text_payload("secret text"))
    pump(a, b)
    assert clip_b.payload == text_payload("secret text")
    assert b.state_summary()["outbox"] == 0


def test_mismatched_keys_drop_frames() -> None:
    a, b, clip_a, clip_b, _, _ = make_pair_of_engines(codec=FernetCodec.generate())
    b._codec = FernetCodec.generate()  # deliberate mismatch for the test
    clip_a.set_payload(text_payload("secret"))
    pump(a, b)
    assert clip_b.payload is None
    assert b.state_summary()["outbox"] == 0


def test_clipboard_read_failure_is_counted_not_raised() -> None:
    a, _, clip_a, _, _, _ = make_pair_of_engines()
    clip_a.fail_read = True
    stats = a.poll_once()
    assert stats.dropped_invalid == 1


def test_oversized_frame_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    a, _, clip_a, _, _, tb = make_pair_of_engines()

    def boom(_frame: bytes):
        raise PayloadTooLarge("too big")

    monkeypatch.setattr("clipsync.engine.decode_frame", boom)
    tb.send(b"anything")
    stats = a.poll_once()
    assert stats.rejected_too_large == 1
    assert clip_a.writes == []


def test_same_content_recopied_locally_is_not_resent() -> None:
    a, b, clip_a, clip_b, _, _ = make_pair_of_engines()
    clip_a.set_payload(text_payload("once"))
    pump(a, b)
    clip_a.set_payload(text_payload("once"))  # user copies identical content again
    assert a.poll_once().sent == 0
    assert b.poll_once().applied == 0
    assert clip_b.payload == text_payload("once")
