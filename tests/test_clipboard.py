"""Tests for the clipboard abstraction.  No real clipboard is touched."""

from __future__ import annotations

import pytest

from clipsync.clipboard import (
    ClipboardError,
    FakeClipboard,
    WlClipboardBackend,
    choose_mime,
)
from clipsync.protocol import (
    MAX_IMAGE_BYTES,
    PayloadTooLarge,
    image_payload,
    text_payload,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class CannedRunner:
    """Command runner that returns canned (rc, stdout, stderr) tuples."""

    def __init__(self, responses: list[tuple[int, bytes, bytes]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], bytes | None]] = []

    def run(self, argv: list[str], stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        self.calls.append((list(argv), stdin))
        if not self.responses:
            raise AssertionError("unexpected extra command")
        return self.responses.pop(0)


# -- FakeClipboard -----------------------------------------------------------
def test_fake_clipboard_roundtrip_and_write_log() -> None:
    clip = FakeClipboard()
    assert clip.read() is None
    payload = text_payload("hello")
    clip.write(payload)
    assert clip.read() == payload
    assert clip.writes == [payload]
    clip.set_payload(text_payload("user copy"))
    assert clip.read() == text_payload("user copy")
    assert len(clip.writes) == 1  # set_payload is not an engine write
    clip.clear()
    assert clip.read() is None


def test_fake_clipboard_injected_failures() -> None:
    clip = FakeClipboard(fail_read=True)
    with pytest.raises(ClipboardError):
        clip.read()
    clip = FakeClipboard(fail_write=True)
    with pytest.raises(ClipboardError):
        clip.write(text_payload("x"))


# -- choose_mime -------------------------------------------------------------
@pytest.mark.parametrize(
    ("types", "expected"),
    [
        (["text/plain"], "text/plain"),
        (["text/html", "text/plain"], "text/plain"),
        (["image/png", "text/html"], "image/png"),
        (["image/jpeg", "image/png"], "image/png"),
        (["image/gif"], "image/gif"),
        (["text/uri-list"], "text/uri-list"),
        (["application/x-foo", ""], None),
        ([], None),
    ],
)
def test_choose_mime(types: list[str], expected: str | None) -> None:
    assert choose_mime(types) == expected


# -- WlClipboardBackend (pure/command level only) ----------------------------
def test_command_builders() -> None:
    assert WlClipboardBackend.build_list_types_command() == ["wl-paste", "--list-types"]
    assert WlClipboardBackend.build_read_command("image/png") == [
        "wl-paste",
        "--no-newline",
        "--type",
        "image/png",
    ]
    assert WlClipboardBackend.build_write_command("text/plain") == [
        "wl-copy",
        "--type",
        "text/plain",
    ]


def test_constructor_never_runs_a_command() -> None:
    runner = CannedRunner([])
    WlClipboardBackend(runner=runner)
    assert runner.calls == []


def test_read_image_with_canned_runner() -> None:
    runner = CannedRunner(
        [
            (0, b"text/html\nimage/png\n", b""),
            (0, PNG, b""),
        ]
    )
    backend = WlClipboardBackend(runner=runner)
    payload = backend.read()
    assert payload is not None
    assert payload.kind == "image"
    assert payload.mime == "image/png"
    assert payload.data == PNG
    assert runner.calls[0][0] == ["wl-paste", "--list-types"]
    assert runner.calls[1][0] == ["wl-paste", "--no-newline", "--type", "image/png"]


def test_read_text_with_canned_runner() -> None:
    runner = CannedRunner(
        [
            (0, b"text/plain\n", b""),
            (0, "héllo".encode(), b""),
        ]
    )
    payload = WlClipboardBackend(runner=runner).read()
    assert payload == text_payload("héllo")


def test_read_empty_clipboard_returns_none() -> None:
    runner = CannedRunner([(0, b"text/plain\n", b""), (0, b"", b"")])
    assert WlClipboardBackend(runner=runner).read() is None
    runner = CannedRunner([(0, b"application/x-weird\n", b"")])
    assert WlClipboardBackend(runner=runner).read() is None


def test_read_too_large_image_raises() -> None:
    runner = CannedRunner(
        [
            (0, b"image/png\n", b""),
            (0, b"x" * (MAX_IMAGE_BYTES + 1), b""),
        ]
    )
    with pytest.raises(PayloadTooLarge):
        WlClipboardBackend(runner=runner).read()


def test_write_text_pipes_utf8_stdin() -> None:
    runner = CannedRunner([(0, b"", b"")])
    backend = WlClipboardBackend(runner=runner)
    backend.write(text_payload("héllo"))
    argv, stdin = runner.calls[0]
    assert argv == ["wl-copy", "--type", "text/plain"]
    assert stdin == "héllo".encode()


def test_write_image_pipes_raw_bytes() -> None:
    runner = CannedRunner([(0, b"", b"")])
    backend = WlClipboardBackend(runner=runner)
    backend.write(image_payload(PNG, "image/png"))
    argv, stdin = runner.calls[0]
    assert argv == ["wl-copy", "--type", "image/png"]
    assert stdin == PNG


def test_backend_error_includes_stderr() -> None:
    runner = CannedRunner([(1, b"", b"no compositor")])
    with pytest.raises(ClipboardError, match="no compositor"):
        WlClipboardBackend(runner=runner).read()
