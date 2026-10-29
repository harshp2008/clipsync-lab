"""Clipboard backend abstraction.

Four implementations:

* :class:`FakeClipboard` -- in-memory, used by every test in this repo.
* :class:`WlClipboardBackend` -- real Wayland (wl-clipboard) backend, the default.
* :class:`XclipClipboardBackend` -- X11/XWayland (xclip) backend, selected explicitly
  with ``clipboard_backend = "xclip"``.  On GNOME Wayland the compositor has no
  data-control protocol, so every ``wl-paste`` opens a transient window (taskbar
  flicker) and can hang; xclip goes through XWayland instead and does neither.
* :class:`FileClipboardBackend` -- headless/test-only text file backend, selected
  explicitly with ``clipboard_backend = "file"`` in the daemon config.  It is never
  selected by default and never touches the real clipboard.

``WlClipboardBackend`` and ``XclipClipboardBackend`` are **NOT executed in this run**.
Constructing them never spawns a process; ``read()``/``write()`` do, and are only
meant to be called by a human on a real desktop session.  Tests drive them with a
fake :class:`CommandRunner` and exercise the pure command builders.

Failure contract (see :data:`CLIPBOARD_FAILURES`): a backend converts any operating
system failure (timeout, missing binary, permission error, bad output) into
:class:`ClipboardError`, so the engine and the daemon poll loop never see a raw
``subprocess``/``OSError``.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Protocol, runtime_checkable

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


# Every failure a clipboard backend can hit on the operating-system side.  The
# engine catches this tuple around its read/write calls and the daemon poll loop
# catches it around a whole poll, so none of these can ever kill the daemon.
CLIPBOARD_FAILURES: tuple[type[BaseException], ...] = (
    ClipboardError,
    subprocess.TimeoutExpired,
    OSError,  # includes FileNotFoundError (missing binary), PermissionError, ...
)


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


@dataclass
class FileClipboardBackend:
    """Text-only clipboard backed by a plain file.  Test/headless use only.

    Not the default: select it with ``clipboard_backend = "file"`` plus
    ``clipboard_file = "/path/to/clip.txt"``.  Every ``read()`` re-reads the file,
    so the daemon's normal poll loop is the polling mechanism.  Images are
    rejected with :class:`ClipboardError`.

    This is a production seam for running real daemon processes on a headless
    machine (the deployment smoke test).  It never touches Wayland, ``wl-paste``
    or ``wl-copy``, and must not be used as the user's real clipboard.
    """

    path: str | Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def read(self) -> ClipPayload | None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ClipboardError(f"cannot read clipboard file {self.path}: {exc}") from exc
        if not raw:
            return None
        if len(raw) > MAX_TEXT_BYTES:
            raise ClipboardError(
                f"clipboard file {self.path} is {len(raw)} bytes, over the text limit"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClipboardError(
                f"clipboard file {self.path} is not valid UTF-8 text: {exc}"
            ) from exc
        return text_payload(text, "text/plain")

    def write(self, payload: ClipPayload) -> None:
        if payload.kind != "text" or payload.text is None:
            raise ClipboardError("file clipboard backend supports text payloads only")
        raw = payload.text.encode("utf-8")
        if len(raw) > MAX_TEXT_BYTES:
            raise ClipboardError(
                f"text payload is {len(raw)} bytes, over the text limit"
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise ClipboardError(
                f"cannot write clipboard file {self.path}: {exc}"
            ) from exc


@runtime_checkable
class CommandRunner(Protocol):
    def run(
        self,
        argv: list[str],
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run ``argv``, optionally feeding ``stdin``; return (rc, stdout, stderr).

        ``timeout`` is honoured by :class:`SubprocessRunner`; simplified fake runners
        may accept and ignore it (the backends pass it only for xclip).
        """


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL a ``start_new_session=True`` child's whole process group."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _feed_stdin(proc: subprocess.Popen, data: bytes) -> None:
    """Write ``data`` to ``proc``'s stdin and close it (runs in a helper thread)."""
    stdin = proc.stdin
    if stdin is None:  # pragma: no cover - Popen was created with stdin=PIPE
        return
    try:
        stdin.write(data)
    except (BrokenPipeError, OSError, ValueError):
        pass  # the child exited or closed stdin; the return code still tells the story
    finally:
        try:
            stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


class SubprocessRunner:
    """Default runner: real processes, hardened for fork-and-daemonize tools.

    * Reads (``stdin is None``) capture stdout/stderr; they are expected to exit.
    * Writes (``stdin`` given) send stdout/stderr to ``/dev/null`` on purpose.
      ``xclip -i``/``wl-copy`` fork a background child that keeps owning the
      selection and inherits its parent's file descriptors; piping stdout/stderr
      would make the parent's ``communicate()`` wait for that child forever.  With
      ``/dev/null`` only the direct parent is waited for and reaped.
    * Every child runs in a new session/process group so a timeout can kill the
      whole group, and the parent is always reaped: no zombies, no hangs.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = float(timeout)

    def run(
        self,
        argv: list[str],
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        effective = self.timeout if timeout is None else float(timeout)
        if stdin is None:
            return self._run_capture(argv, effective)
        return self._run_feed(argv, stdin, effective)

    def _run_capture(self, argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            raise
        return proc.returncode, out or b"", err or b""

    def _run_feed(self, argv: list[str], stdin: bytes, timeout: float) -> tuple[int, bytes, bytes]:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        writer = threading.Thread(target=_feed_stdin, args=(proc, stdin), daemon=True)
        writer.start()
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            raise
        writer.join(timeout=1.0)
        return rc, b"", b""


def _run_command(
    runner: CommandRunner,
    argv: list[str],
    stdin: bytes | None = None,
    timeout: float | None = None,
) -> tuple[int, bytes, bytes]:
    """Call a runner, converting every OS-level failure into :class:`ClipboardError`."""
    try:
        if timeout is None:
            return runner.run(argv, stdin)
        return runner.run(argv, stdin, timeout=timeout)
    except ClipboardError:
        raise
    except subprocess.TimeoutExpired as exc:
        limit = exc.timeout
        detail = f"{limit:g} s" if isinstance(limit, (int, float)) else "the limit"
        raise ClipboardError(f"{argv[0]} timed out after {detail}") from exc
    except FileNotFoundError as exc:
        raise ClipboardError(f"{argv[0]} is not installed: {exc}") from exc
    except OSError as exc:
        raise ClipboardError(f"cannot run {argv[0]}: {exc}") from exc


def choose_mime(types: Iterable[str]) -> str | None:
    """Pick the best supported MIME type from a ``wl-paste --list-types`` listing.

    Images win over text: copying a screenshot or an image usually also advertises
    ``text/html``/``text/uri-list`` fallbacks, and the image is what the user meant.
    """
    offered = {t.strip().lower() for t in types if t and t.strip()}
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

    # -- real operations ------------------------------------------------------
    def _run(self, argv: list[str], stdin: bytes | None = None) -> bytes:
        rc, out, err = _run_command(self._runner, argv, stdin)
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
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClipboardError(f"clipboard holds invalid UTF-8 text: {exc}") from exc
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


# -- X11 / XWayland backend (xclip) --------------------------------------------
#
# GNOME's Wayland compositor does not implement the wl-clipboard data-control
# protocol.  ``wl-paste`` then goes through the normal data device, which shows a
# transient window on every poll (taskbar flicker) and can block for seconds.
# XWayland's clipboard bridge does not have that problem, and xclip is a tiny
# client for it.  This backend is opt-in: ``clipboard_backend = "xclip"``.

_XCLIP_READ_TIMEOUT = 2.0
_XCLIP_WRITE_TIMEOUT = 3.0

# X selection targets -> the text MIME type clipsync puts on the wire.
_TEXT_TARGET_ALIASES: dict[str, str] = {
    "utf8_string": "text/plain",
    "string": "text/plain",
    "text": "text/plain",
    "text/plain;charset=utf-8": "text/plain",
}
# Preference order between the three common text targets.
_XCLIP_TEXT_TARGET_ORDER = ("utf8_string", "string", "text/plain;charset=utf-8")
# xclip's way of saying "there is no selection" / "the owner went away".
_XCLIP_EMPTY_MARKERS = ("target not available", "no selection", "nothing is copied")


def _looks_like_empty(stderr: bytes) -> bool:
    text = stderr.decode("utf-8", "replace").lower()
    return any(marker in text for marker in _XCLIP_EMPTY_MARKERS)


def choose_xclip_target(targets: Iterable[str]) -> str | None:
    """Pick the best target from ``xclip -o -t TARGETS`` output.

    Same priority as :func:`choose_mime`: images win (a pasted screenshot also
    advertises text fallbacks), then ``UTF8_STRING``/``STRING``/
    ``text/plain;charset=utf-8``, then the plain text MIME types.  X selection
    targets are case-sensitive atoms, so the spelling xclip reported is returned
    unchanged.
    """
    offered: dict[str, str] = {}
    for raw in targets:
        name = raw.strip()
        if not name:
            continue
        offered.setdefault(name.lower(), name)
    for mime in _IMAGE_PREFERENCE:
        if mime in offered:
            return offered[mime]
    for mime in SUPPORTED_IMAGE_MIMES:
        if mime in offered:
            return offered[mime]
    for target in _XCLIP_TEXT_TARGET_ORDER:
        if target in offered:
            return offered[target]
    for mime in _TEXT_PREFERENCE:
        if mime in offered:
            return offered[mime]
    for mime in sorted(SUPPORTED_TEXT_MIMES):
        if mime in offered:
            return offered[mime]
    return None


def _xclip_text_mime(target: str) -> str:
    name = target.strip()
    mime = _TEXT_TARGET_ALIASES.get(name.lower(), name)
    if mime not in SUPPORTED_TEXT_MIMES:
        raise ClipboardError(f"xclip target {target!r} is not a supported text MIME type")
    return mime


def _xclip_write_target(mime: str) -> str:
    name = mime.strip()
    lowered = name.lower()
    if lowered in SUPPORTED_IMAGE_MIMES:
        return lowered
    if lowered == "text/plain":
        # UTF8_STRING is what X11 clients (and the XWayland bridge) expect for
        # UTF-8 text; the plain STRING target would be Latin-1.
        return "UTF8_STRING"
    if lowered in SUPPORTED_TEXT_MIMES:
        return lowered
    raise ClipboardError(f"cannot write unsupported MIME type {mime!r} with xclip")


class XclipClipboardBackend:
    """X11/XWayland clipboard backend based on ``xclip``.

    Selected with ``clipboard_backend = "xclip"`` on GNOME Wayland sessions where
    the compositor has no data-control protocol.  NOT EXECUTED IN THIS RUN: every
    test drives ``runner`` with canned output, so no real xclip call is made.

    Read/write timeouts are deliberately short (2 s / 3 s): a wedged X server must
    never stall a poll for longer than that.
    """

    READ_TIMEOUT = _XCLIP_READ_TIMEOUT
    WRITE_TIMEOUT = _XCLIP_WRITE_TIMEOUT

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or SubprocessRunner()

    # -- pure helpers (safe to call anywhere) ---------------------------------
    @staticmethod
    def build_list_targets_command() -> list[str]:
        return ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"]

    @staticmethod
    def build_read_command(target: str) -> list[str]:
        return ["xclip", "-selection", "clipboard", "-o", "-t", target]

    @staticmethod
    def build_write_command(target: str) -> list[str]:
        return ["xclip", "-selection", "clipboard", "-i", "-t", target]

    @staticmethod
    def write_target_for(mime: str) -> str:
        return _xclip_write_target(mime)

    # -- real operations ------------------------------------------------------
    def _run(
        self, argv: list[str], stdin: bytes | None = None, timeout: float | None = None
    ) -> tuple[int, bytes, bytes]:
        return _run_command(self._runner, argv, stdin, timeout)

    def _list_targets(self) -> list[str] | None:
        rc, out, err = self._run(
            self.build_list_targets_command(), timeout=self.READ_TIMEOUT
        )
        if rc != 0 or _looks_like_empty(err):
            # No selection owner (or xclip cannot see one) is an empty clipboard,
            # not a failure: the daemon must keep polling.
            return None
        return out.decode("utf-8", "replace").splitlines()

    def read(self) -> ClipPayload | None:
        targets = self._list_targets()
        if targets is None:
            return None
        target = choose_xclip_target(targets)
        if target is None:
            return None
        rc, raw, err = self._run(
            self.build_read_command(target), timeout=self.READ_TIMEOUT
        )
        if _looks_like_empty(err):
            return None
        if rc != 0:
            raise ClipboardError(
                f"xclip exited {rc}: {err.decode('utf-8', 'replace').strip()[:200]}"
            )
        if not raw:
            return None
        if target.lower().startswith("image/"):
            if len(raw) > MAX_IMAGE_BYTES:
                raise PayloadTooLarge(f"image is {len(raw)} bytes")
            return image_payload(raw, target.lower())
        if len(raw) > MAX_TEXT_BYTES:
            raise PayloadTooLarge("text payload is too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClipboardError(f"clipboard holds invalid UTF-8 text: {exc}") from exc
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise PayloadTooLarge("text payload is too large")
        return text_payload(text, _xclip_text_mime(target))

    def write(self, payload: ClipPayload) -> None:
        target = _xclip_write_target(payload.mime)
        data = payload.text.encode("utf-8") if payload.kind == "text" else payload.data
        if data is None:  # pragma: no cover - ClipPayload validates this
            raise ClipboardError("payload has no data to write")
        rc, _out, err = self._run(
            self.build_write_command(target), data, timeout=self.WRITE_TIMEOUT
        )
        if rc != 0:
            detail = err.decode("utf-8", "replace").strip()[:200]
            raise ClipboardError(f"xclip -i exited {rc}" + (f": {detail}" if detail else ""))


def xclip_unavailable_reason(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Why the xclip backend cannot run here, or ``None`` when it can.

    Empty clipboard data, a missing selection owner or a transient xclip failure are
    run-time conditions, not startup problems; only a missing binary or an unset
    ``DISPLAY`` prevents the backend from working at all.
    """
    lookup = shutil.which if which is None else which
    environ = os.environ if env is None else env
    if lookup("xclip") is None:
        return (
            "the xclip backend needs the 'xclip' program on PATH, but xclip was not found"
        )
    if not str(environ.get("DISPLAY") or "").strip():
        return (
            "the xclip backend needs DISPLAY to be set (XWayland on a Wayland "
            "session), but DISPLAY is not set"
        )
    return None


def backend_unavailable_reason(
    backend: str,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Startup check for the configured backend: a reason string or ``None``.

    The daemon turns a non-``None`` answer into a config error (exit code 2) instead
    of entering a poll loop that fails on every iteration.
    """
    name = (backend or "wayland").strip().lower()
    if name == "xclip":
        return xclip_unavailable_reason(env=env, which=which)
    if name in ("wayland", "wl"):
        lookup = shutil.which if which is None else which
        missing = [prog for prog in ("wl-paste", "wl-copy") if lookup(prog) is None]
        if missing:
            return (
                "the wl (Wayland) backend needs "
                + " and ".join(missing)
                + ' on PATH; install wl-clipboard or set clipboard_backend = "xclip"'
            )
    return None


def xclip_available() -> bool:
    """True if xclip is installed and ``DISPLAY`` is set (does not run xclip)."""
    return xclip_unavailable_reason() is None


__all__ = [
    "CLIPBOARD_FAILURES",
    "ClipboardBackend",
    "ClipboardError",
    "CommandRunner",
    "FakeClipboard",
    "FileClipboardBackend",
    "SubprocessRunner",
    "WlClipboardBackend",
    "XclipClipboardBackend",
    "backend_unavailable_reason",
    "choose_mime",
    "choose_xclip_target",
    "wl_clipboard_available",
    "xclip_available",
    "xclip_unavailable_reason",
]
