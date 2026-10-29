"""Clipboard-failure robustness: the poll loop must never die on a bad clipboard.

These tests use the fake xclip runner and in-memory transports only.  The
:class:`SubprocessRunner` tests run ``python -c`` helper processes (never a real
clipboard program) to check timeouts, stdin feeding and subprocess reaping.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

from clipsync.clipboard import ClipboardError, SubprocessRunner, WlClipboardBackend, XclipClipboardBackend
from clipsync.config import DaemonConfig
from clipsync.daemon import ClipboardDaemon, main
from clipsync.engine import SyncEngine
from clipsync.protocol import ClipMessage, encode_frame, text_payload
from clipsync.transport import make_pair

from tests.test_clipboard_xclip import FakeXclipRunner


def make_daemon(runner: FakeXclipRunner, *, poll_interval: float = 0.1, clock=None):
    clip = XclipClipboardBackend(runner=runner)
    t1, t2 = make_pair()
    cfg = DaemonConfig(
        device_id="lap",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        poll_interval=poll_interval,
    )
    kwargs = {} if clock is None else {"clock": clock}
    daemon = ClipboardDaemon(cfg, clipboard=clip, transport=t1, **kwargs)
    daemon.connect()
    return daemon, t1, t2


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_daemon_logs():
    """Capture ``clipsync.daemon`` records with a handler on that logger.

    Not pytest's ``caplog``: under the ROS PYTHONPATH a third-party logger class
    forces ``propagate=False`` on loggers, so root-handler capture sees nothing.
    """
    logger = logging.getLogger("clipsync.daemon")
    handler = _ListHandler()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def clipboard_warnings(records) -> list[logging.LogRecord]:
    return [
        record
        for record in records
        if record.levelno == logging.WARNING
        and record.name == "clipsync.daemon"
        and "Clipboard backend error" in record.getMessage()
    ]


# -- engine containment ------------------------------------------------------
def test_engine_read_timeout_is_counted_not_raised() -> None:
    runner = FakeXclipRunner()
    runner.read_error = subprocess.TimeoutExpired("xclip", 2.0)
    runner.fail_reads = 1
    t1, _t2 = make_pair()
    engine = SyncEngine("lap", XclipClipboardBackend(runner=runner), t1)
    stats = engine.poll_once()
    assert stats.clipboard_errors == 1
    assert engine.last_clipboard_error is not None
    assert "read" in engine.last_clipboard_error
    assert "timed out" in engine.last_clipboard_error


def test_engine_write_timeout_is_counted_not_raised() -> None:
    runner = FakeXclipRunner()
    runner.write_error = subprocess.TimeoutExpired("xclip", 3.0)
    runner.fail_writes = 1
    t1, t2 = make_pair()
    engine = SyncEngine("lap", XclipClipboardBackend(runner=runner), t1)
    t2.send(encode_frame(ClipMessage(origin="phone", seq=1, payload=text_payload("hi"))))
    stats = engine.poll_once()
    assert stats.clipboard_errors == 1
    assert engine.last_clipboard_error is not None
    assert "write" in engine.last_clipboard_error


def test_engine_read_missing_binary_is_counted_not_raised() -> None:
    runner = FakeXclipRunner()
    runner.read_error = FileNotFoundError(2, "No such file or directory")
    runner.fail_reads = 1
    t1, _t2 = make_pair()
    stats = SyncEngine("lap", XclipClipboardBackend(runner=runner), t1).poll_once()
    assert stats.clipboard_errors == 1


# -- daemon loop: read failures ---------------------------------------------
def test_daemon_survives_read_timeout_logs_once_backs_off_and_recovers() -> None:
    runner = FakeXclipRunner()
    runner.read_error = subprocess.TimeoutExpired("xclip", 2.0)
    runner.fail_reads = 4
    runner.set_text("recovered")
    daemon, _t1, t2 = make_daemon(runner, poll_interval=0.1)

    sleeps: list[float] = []
    with capture_daemon_logs() as records:
        polls = daemon.run(max_iterations=6, sleep=sleeps.append)

    assert polls == 6
    warnings = clipboard_warnings(records)
    assert len(warnings) == 1
    assert "timed out" in warnings[0].getMessage()
    # 0.1 then doubling to 0.8, then back to poll_interval on the first success.
    assert [round(delay, 3) for delay in sleeps] == pytest.approx([0.1, 0.2, 0.4, 0.8, 0.1])

    frames = t2.recv()
    assert len(frames) == 1
    from clipsync.protocol import decode_frame

    assert decode_frame(frames[0]).payload == text_payload("recovered")


def test_clipboard_backoff_is_capped_at_five_seconds() -> None:
    runner = FakeXclipRunner()
    runner.read_error = subprocess.TimeoutExpired("xclip", 2.0)
    runner.fail_reads = 8
    daemon, _t1, _t2 = make_daemon(runner, poll_interval=0.1)

    sleeps: list[float] = []
    with capture_daemon_logs():
        daemon.run(max_iterations=8, sleep=sleeps.append)

    assert [round(delay, 3) for delay in sleeps] == pytest.approx(
        [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0]
    )
    assert max(sleeps) <= 5.0


def test_same_clipboard_error_is_logged_once_per_minute() -> None:
    now = [100.0]
    runner = FakeXclipRunner()
    runner.read_error = subprocess.TimeoutExpired("xclip", 2.0)
    runner.fail_reads = 10
    daemon, _t1, _t2 = make_daemon(runner, poll_interval=0.1, clock=lambda: now[0])

    sleeps: list[float] = []
    with capture_daemon_logs() as records:
        daemon.run(max_iterations=3, sleep=sleeps.append)
        assert len(clipboard_warnings(records)) == 1

        now[0] += 30.0
        daemon.run(max_iterations=1, sleep=sleeps.append)
        assert len(clipboard_warnings(records)) == 1  # still inside the 60 s window

        now[0] += 31.0
        daemon.run(max_iterations=1, sleep=sleeps.append)
        assert len(clipboard_warnings(records)) == 2  # a new distinct time window


# -- daemon loop: write failures --------------------------------------------
def test_daemon_survives_write_timeout_and_applies_a_later_frame() -> None:
    runner = FakeXclipRunner()
    runner.write_error = subprocess.TimeoutExpired("xclip", 3.0)
    runner.fail_writes = 1
    daemon, _t1, t2 = make_daemon(runner, poll_interval=0.1)

    t2.send(encode_frame(ClipMessage(origin="phone", seq=1, payload=text_payload("first"))))
    t2.send(encode_frame(ClipMessage(origin="phone", seq=2, payload=text_payload("second"))))

    sleeps: list[float] = []
    with capture_daemon_logs() as records:
        polls = daemon.run(max_iterations=3, sleep=sleeps.append)

    assert polls == 3
    warnings = clipboard_warnings(records)
    assert len(warnings) == 1
    assert "write" in warnings[0].getMessage()
    # The second frame was applied once the backend recovered.
    assert runner.selection == {"UTF8_STRING": b"second"}
    # Nothing was re-published: the echo guard sees its own write on the clipboard.
    assert t2.recv() == []
    assert sleeps[0] == pytest.approx(0.1)


# -- CLI --once --------------------------------------------------------------
class ExplodingClipboard:
    """Simulates a backend that leaks a raw subprocess exception."""

    def read(self):
        raise subprocess.TimeoutExpired("wl-paste", 10)

    def write(self, payload):
        raise subprocess.TimeoutExpired("wl-copy", 10)


def test_once_mode_survives_a_leaked_timeout(tmp_path, capsys) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'device_id = "lap"\n'
        'relay_url = "ws://127.0.0.1:8765/"\n'
        'token = "tok"\n',
        encoding="utf-8",
    )
    t1, _t2 = make_pair()
    rc = main(
        ["--config", str(path), "--once"],
        clipboard_factory=lambda _cfg: ExplodingClipboard(),
        transport_factory=lambda _cfg: t1,
    )
    assert rc == 0
    out = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out
    stats = json.loads(out[-1])
    assert stats["clipboard_errors"] == 1


# -- backends convert runner failures ---------------------------------------
class RaisingRunner:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls: list[list[str]] = []

    def run(self, argv, stdin=None):
        self.calls.append(list(argv))
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired("wl-paste", 10),
        OSError(5, "Input/output error"),
    ],
)
def test_wl_backend_converts_runner_failure(exc: BaseException) -> None:
    backend = WlClipboardBackend(runner=RaisingRunner(exc))
    with pytest.raises(ClipboardError):
        backend.read()
    with pytest.raises(ClipboardError):
        backend.write(text_payload("x"))


def test_wl_backend_missing_binary_is_clipboard_error() -> None:
    backend = WlClipboardBackend(runner=RaisingRunner(FileNotFoundError(2, "nope")))
    with pytest.raises(ClipboardError, match="not installed"):
        backend.read()


# -- SubprocessRunner (real helper processes, never a clipboard program) -----
def test_subprocess_runner_captures_stdout() -> None:
    rc, out, err = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.write('hello')"]
    )
    assert (rc, out, err) == (0, b"hello", b"")


def test_subprocess_runner_feeds_stdin() -> None:
    rc, out, err = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        b"payload",
    )
    assert rc == 0
    # Writes deliberately discard stdout/stderr so a forked owner cannot block us.
    assert (out, err) == (b"", b"")


def test_subprocess_runner_timeout_raises_and_does_not_hang() -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.2
        )
    assert time.monotonic() - started < 3.0


def test_subprocess_runner_missing_binary_raises_oserror() -> None:
    with pytest.raises(FileNotFoundError):
        SubprocessRunner().run(["clipsync-definitely-not-installed-xyz"])


def test_subprocess_runner_write_does_not_wait_for_a_forked_child() -> None:
    # A child that forks and leaves the grandchild sleeping must not block us: the
    # forked owner would inherit any pipes, which is exactly the xclip/wl-copy trap.
    script = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(2)\n"
        "    os._exit(0)\n"
        "sys.exit(0)\n"
    )
    started = time.monotonic()
    rc, _out, _err = SubprocessRunner().run([sys.executable, "-c", script], b"")
    assert rc == 0
    assert time.monotonic() - started < 1.5
