"""Clipboard backend abstraction.

Two implementations:

* :class:`FakeClipboard` -- in-memory, used by every test in this repo.
* :class:`WlClipboardBackend` -- real Wayland (wl-clipboard) backend.

``WlClipboardBackend`` is **NOT executed in this run**.  Constructing it never spawns
a process; ``read()``/``write()`` do, and are only meant to be called by a human on a
real desktop session.  Tests exercise only the pure command builders and
:func:`choose_mime`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

from .protocol import (
    MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
    SUPPORTED_IMAGE_MIMES,
    SUPPORTED_TEXT_MIMES,
    ClipPayload,
    PayloadTooLarge,
    UnsupportedMime,
    image_payload,
    text_payload,
)

# Preferred order when several image types are offered by the source application.
_IMAGE_PREFERENCE = ("image/png", "image/webp", "image/jpeg", "image/gif")
_TEXT_PREFERENCE = ("text/plain", "text/html", "text/uri-list")


class ClipboardError(Exception):
    """A clipboard backend failed to read or write."""


@runtime_checkable
class ClipboardBackend(Protocol):
    def read(self) -> ClipPayload | None: ...

    def write(self, payload: ClipPayload) -> None: ...


@dataclass
class FakeClipboard:
    """In-memory clipboard for tests.  Never touches the real system clipboard."""

    payload: ClipPayload | None = None
    writes: list[ClipPayload] = field(default_factory=list)
    reads: int = 0
    fail_read: bool = False
    fail_write: bool = False

    def read(self) -> ClipPayload | None:
        self.reads += 1
        if self.fail_read:
            raise ClipboardError("injected read failure")
        return self.payload

    def write(self, payload: ClipPayload) -> None:
        if self.fail_write:
            raise ClipboardError("injected write failure")
        self.writes.append(payload)
        self.payload = payload

    def set_payload(self, payload: ClipPayload | None) -> None:
        """Simulate the user copying something (does not count as an engine write)."""
        self.payload = payload

    def clear(self) -> None:
        self.payload = None


@runtime_checkable
class CommandRunner(Protocol):
    def run(self, argv: list[str], stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        """Run ``argv``, optionally feeding ``stdin``; return (rc, stdout, stderr)."""


class SubprocessRunner:
    """Default runner.  Only used when a human explicitly runs the real backend."""

    def run(self, argv: list[str], stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        proc = subprocess.run(
            argv,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        return proc.returncode, proc.stdout or b"", proc.stderr or b""


def choose_mime(types: Iterable[str]) -> str | None:
    """Pick the best supported MIME type from a ``wl-paste --list-types`` listing.

    Text/plain takes precedence when plain text is offered. For other clipboard
    selections (such as images which may advertise text/html or text/uri-list fallbacks),
    supported images win over rich text.
    """
    offered = {t.strip().lower() for t in types if t and t.strip()}
    if "text/plain" in offered:
        return "text/plain"
    for mime in _IMAGE_PREFERENCE:
        if mime in offered:
            return mime
    for mime in SUPPORTED_IMAGE_MIMES:
        if mime in offered:
            return mime
    for mime in _TEXT_PREFERENCE:
        if mime in offered:
            return mime
    for mime in sorted(SUPPORTED_TEXT_MIMES):
        if mime in offered:
            return mime
    return None


class WlClipboardBackend:
    """Wayland clipboard backend based on ``wl-paste``/``wl-copy``.

    NOT EXECUTED IN THIS RUN.  Requires ``wl-clipboard`` on PATH and a live Wayland
    session.  ``runner`` is injectable so a future test could drive it with canned
    output without spawning processes.

    Known GNOME caveat: the core Wayland data device only delivers selection data to a
    focused client, so background reads can fail, hang or return stale offers; this is
    why the prototype ships a fake backend and a pluggable transport.
    """

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or SubprocessRunner()

    # -- pure helpers (safe to call anywhere) ---------------------------------
    @staticmethod
    def build_list_types_command() -> list[str]:
        return ["wl-paste", "--list-types"]

    @staticmethod
    def build_read_command(mime: str) -> list[str]:
        return ["wl-paste", "--no-newline", "--type", mime]

    @staticmethod
    def build_write_command(mime: str) -> list[str]:
        return ["wl-copy", "--type", mime]

    @staticmethod
    def choose_mime(types: Iterable[str]) -> str | None:
        return choose_mime(types)

    # -- real operations ------------------------------------------------------
    def _run(self, argv: list[str], stdin: bytes | None = None) -> bytes:
        rc, out, err = self._runner.run(argv, stdin)
        if rc != 0:
            raise ClipboardError(
                f"{argv[0]} exited {rc}: {err.decode('utf-8', 'replace').strip()[:200]}"
            )
        return out

    def read(self) -> ClipPayload | None:
        listing = self._run(self.build_list_types_command()).decode("utf-8", "replace")
        mime = choose_mime(listing.splitlines())
        if mime is None:
            return None
        raw = self._run(self.build_read_command(mime))
        if not raw:
            return None
        if mime.startswith("image/"):
            if len(raw) > MAX_IMAGE_BYTES:
                raise PayloadTooLarge(f"image is {len(raw)} bytes")
            return image_payload(raw, mime)
        text = raw.decode("utf-8")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise PayloadTooLarge("text payload is too large")
        return text_payload(text, mime)

    def write(self, payload: ClipPayload) -> None:
        argv = self.build_write_command(payload.mime)
        stdin = payload.text.encode("utf-8") if payload.kind == "text" else payload.data
        assert stdin is not None
        self._run(argv, stdin)


def wl_clipboard_available() -> bool:
    """True if ``wl-paste``/``wl-copy`` are installed (does not run them)."""
    return bool(shutil.which("wl-paste") and shutil.which("wl-copy"))


__all__ = [
    "ClipboardBackend",
    "ClipboardError",
    "CommandRunner",
    "FakeClipboard",
    "SubprocessRunner",
    "WlClipboardBackend",
    "choose_mime",
    "wl_clipboard_available",
]
