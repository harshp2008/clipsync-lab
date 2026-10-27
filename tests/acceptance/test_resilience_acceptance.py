"""Acceptance tests for daemon resilience (spec-first).

Contract: ``docs/design/interfaces.md`` § "resilience + peer run — backoff, resilient
daemon, ``clipsync-peer`` (2026-10-27)".

Black-box tests for the public daemon API only.  They use in-memory fake transports,
loopback WebSockets on an ephemeral port, and ``FakeClipboard``; nothing here reads or
writes the real clipboard and no test outside loopback uses the network.  The one
subprocess test runs the daemon against a closed loopback port, so it never polls a
clipboard.

Owner: orchestrator (written and committed before the implementation job; do not edit
during it).
"""

from __future__ import annotations

import contextlib
import json
import logging
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from clipsync.clipboard import FakeClipboard
from clipsync.config import DaemonConfig
from clipsync.daemon import (
    BackoffPolicy,
    ClipboardDaemon,
    main as daemon_main,
    wait_or_stop,
)
from clipsync.protocol import decode_frame, text_payload
from clipsync.relay_server import RelayServer
from clipsync.relay_transport import RelayTransport
from clipsync.transport import TransportError

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "acceptance-resilience-token"


# -- helpers ----------------------------------------------------------------


def relay_url(port: int) -> str:
    return f"ws://127.0.0.1:{port}/"


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def write_toml(dirpath: Path, **values) -> Path:
    lines: list[str] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        elif isinstance(value, (int, float)):
            encoded = repr(value)
        else:
            encoded = json.dumps(str(value))
        lines.append(f"{key} = {encoded}")
    path = dirpath / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def daemon_config(**overrides) -> DaemonConfig:
    values = {
        "device_id": "laptop",
        "relay_url": "ws://127.0.0.1:9/",
        "token": TOKEN,
    }
    values.update(overrides)
    return DaemonConfig(**values)


class RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def daemon_logs():
    logger = logging.getLogger("clipsync.daemon")
    handler = RecordHandler()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def warnings(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.levelno >= logging.WARNING]


class RecordingTransport:
    """Transport that records calls, can fail connects, and is otherwise silent."""

    def __init__(self, *, connected: bool = False, fail_connects: int = 0) -> None:
        self._connected = connected
        self.fail_connects = fail_connects
        self.connect_calls = 0
        self.close_calls = 0
        self.sent: list[bytes] = []

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self.connect_calls += 1
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise TransportError("relay is unreachable")
        self._connected = True

    def close(self) -> None:
        self.close_calls += 1
        self._connected = False

    def send(self, frame: bytes) -> None:
        if not self._connected:
            raise TransportError("relay is unreachable")
        self.sent.append(bytes(frame))

    def recv(self) -> list[bytes]:
        if not self._connected:
            raise TransportError("relay is unreachable")
        return []


class DroppingTransport(RecordingTransport):
    """Connected transport whose first ``recv`` simulates losing the relay."""

    def __init__(self, *, drops: int = 1) -> None:
        super().__init__(connected=True)
        self.drops = drops

    def recv(self) -> list[bytes]:
        if self.drops > 0:
            self.drops -= 1
            self._connected = False
            raise TransportError("relay connection lost")
        return super().recv()


def make_daemon(transport, *, poll_interval: float = 0.01, **backoff_kwargs) -> ClipboardDaemon:
    backoff = BackoffPolicy(**backoff_kwargs)
    return ClipboardDaemon(
        daemon_config(poll_interval=poll_interval),
        clipboard=FakeClipboard(),
        transport=transport,
        backoff=backoff,
    )


# -- BackoffPolicy ----------------------------------------------------------


def test_backoff_policy_defaults_and_validation() -> None:
    policy = BackoffPolicy()
    assert policy.base == 0.2
    assert policy.max_delay == 30.0
    assert policy.jitter == 0.5

    for kwargs in (
        {"base": 0},
        {"base": -1.0},
        {"base": float("nan")},
        {"base": 1.0, "max_delay": 0.5},
        {"jitter": -0.1},
        {"jitter": 1.5},
    ):
        with pytest.raises(ValueError):
            BackoffPolicy(**kwargs)

    with pytest.raises(ValueError):
        policy.delay(-1)
    with pytest.raises(ValueError):
        policy.delay(True)


def test_backoff_policy_is_capped_exponential_without_jitter() -> None:
    policy = BackoffPolicy(base=0.1, max_delay=2.0, jitter=0.0)
    delays = [policy.delay(attempt, rand=lambda: 0.5) for attempt in range(8)]
    assert delays == pytest.approx([0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0, 2.0])


def test_backoff_policy_jitter_bounds_and_cap() -> None:
    policy = BackoffPolicy(base=1.0, max_delay=8.0, jitter=0.5)
    assert policy.delay(0, rand=lambda: 0.0) == pytest.approx(0.5)
    assert policy.delay(0, rand=lambda: 1.0) == pytest.approx(1.5)
    assert policy.delay(0, rand=lambda: 0.5) == pytest.approx(1.0)
    assert policy.delay(0, rand=lambda: 9.0) == pytest.approx(1.5)  # rand clamped
    assert policy.delay(100, rand=lambda: 1.0) == pytest.approx(8.0)
    for attempt in range(20):
        value = policy.delay(attempt, rand=lambda: 0.25)
        assert 0.0 < value <= 8.0


# -- wait_or_stop -----------------------------------------------------------


def test_wait_or_stop_without_stop_event_sleeps_once() -> None:
    calls: list[float] = []
    assert wait_or_stop(None, 0.3, calls.append) is False
    assert calls == [0.3]


def test_wait_or_stop_returns_true_without_sleeping_when_already_stopped() -> None:
    calls: list[float] = []
    stop = threading.Event()
    stop.set()
    assert wait_or_stop(stop, 30.0, calls.append) is True
    assert calls == []


def test_wait_or_stop_interrupts_a_long_delay_promptly() -> None:
    stop = threading.Event()
    timer = threading.Timer(0.15, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        assert wait_or_stop(stop, 30.0, time.sleep) is True
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2.0


# -- ClipboardDaemon: backoff wiring and connect_resilient ------------------


def test_daemon_backoff_defaults_from_config_reconnect_delay() -> None:
    transport = RecordingTransport()
    daemon = ClipboardDaemon(
        daemon_config(reconnect_delay=0.25),
        clipboard=FakeClipboard(),
        transport=transport,
    )
    assert daemon.backoff.base == pytest.approx(0.25)
    assert daemon.backoff.max_delay >= daemon.backoff.base

    custom = BackoffPolicy(base=0.01, max_delay=0.5, jitter=0.0)
    daemon = ClipboardDaemon(
        daemon_config(), clipboard=FakeClipboard(), transport=transport, backoff=custom
    )
    assert daemon.backoff is custom


def test_connect_resilient_retries_with_capped_backoff() -> None:
    transport = RecordingTransport(fail_connects=2)
    daemon = make_daemon(transport, base=0.1, max_delay=1.0, jitter=0.0)
    sleeps: list[float] = []
    assert daemon.connect_resilient(sleep=sleeps.append) is True
    assert transport.connect_calls == 3
    assert sleeps == pytest.approx([0.1, 0.2])


def test_connect_resilient_stops_after_max_attempts() -> None:
    transport = RecordingTransport(fail_connects=100)
    daemon = make_daemon(transport, base=0.1, max_delay=1.0, jitter=0.0)
    sleeps: list[float] = []
    assert daemon.connect_resilient(max_attempts=2, sleep=sleeps.append) is False
    assert transport.connect_calls == 2
    assert sleeps == pytest.approx([0.1])

    with pytest.raises(ValueError):
        daemon.connect_resilient(max_attempts=-1)
    with pytest.raises(ValueError):
        daemon.connect_resilient(max_attempts=True)


def test_connect_resilient_returns_false_without_connecting_when_stopped() -> None:
    transport = RecordingTransport(fail_connects=100)
    daemon = make_daemon(transport, base=0.1, max_delay=1.0, jitter=0.0)
    stop = threading.Event()
    stop.set()
    assert daemon.connect_resilient(stop_event=stop, sleep=lambda _d: None) is False
    assert transport.connect_calls == 0


# -- ClipboardDaemon.run: outages do not crash, reconnect is backed off -----


def test_daemon_run_without_outage_does_not_reconnect_and_keeps_sleeps() -> None:
    transport = RecordingTransport(connected=True)
    daemon = make_daemon(transport, poll_interval=0.25)
    sleeps: list[float] = []
    assert daemon.run(max_iterations=3, sleep=sleeps.append) == 3
    assert sleeps == [0.25, 0.25]
    assert transport.connect_calls == 0


def test_daemon_run_survives_startup_outage_and_reconnects() -> None:
    transport = RecordingTransport(fail_connects=2)
    daemon = make_daemon(transport, base=0.1, max_delay=1.0, jitter=0.0)
    sleeps: list[float] = []
    with daemon_logs() as records:
        assert daemon.run(max_iterations=4, sleep=sleeps.append) == 4
    assert sleeps == pytest.approx([0.1, 0.2, 0.01])
    assert transport.connect_calls == 3
    assert len(warnings(records)) >= 2


def test_daemon_run_recovers_from_midrun_outage() -> None:
    transport = DroppingTransport(drops=1)
    clip = FakeClipboard()
    daemon = ClipboardDaemon(
        daemon_config(poll_interval=0.01),
        clipboard=clip,
        transport=transport,
        backoff=BackoffPolicy(base=0.05, max_delay=1.0, jitter=0.0),
    )
    sleeps: list[float] = []
    with daemon_logs() as records:
        assert daemon.run(max_iterations=4, sleep=sleeps.append) == 4
    assert sleeps == pytest.approx([0.05, 0.01, 0.01])
    assert transport.connect_calls == 1
    assert warnings(records)

    # After recovery the daemon still syncs a fresh local copy normally.
    clip.set_payload(text_payload("after reconnect"))
    stats = daemon.poll_once()
    assert stats.sent == 1
    assert len(transport.sent) == 1
    assert decode_frame(transport.sent[0]).payload == text_payload("after reconnect")


def test_daemon_run_stop_interrupts_a_long_backoff_wait() -> None:
    transport = RecordingTransport(fail_connects=10**6)
    daemon = make_daemon(transport, base=5.0, max_delay=5.0, jitter=0.0)
    stop = threading.Event()
    results: list[int] = []

    def run() -> None:
        results.append(daemon.run(stop_event=stop))

    thread = threading.Thread(target=run, name="resilience-long-backoff")
    thread.start()
    time.sleep(0.15)
    stop.set()
    thread.join(3.0)
    assert not thread.is_alive(), "run() did not honour stop_event during backoff"
    assert results and results[0] >= 1


def test_daemon_never_touches_clipboard_while_relay_is_down() -> None:
    transport = RecordingTransport(fail_connects=10**6)
    clip = FakeClipboard(payload=text_payload("must not leave the machine"))
    daemon = ClipboardDaemon(
        daemon_config(poll_interval=0.01),
        clipboard=clip,
        transport=transport,
        backoff=BackoffPolicy(base=0.01, max_delay=0.05, jitter=0.0),
    )
    stop = threading.Event()
    timer = threading.Timer(0.2, stop.set)
    timer.start()
    try:
        assert daemon.run(stop_event=stop) >= 1
    finally:
        timer.cancel()
    assert clip.reads == 0
    assert clip.writes == []


# -- clipsync CLI: continuous mode survives a relay that is down at startup --


def test_daemon_main_keeps_retrying_when_relay_is_down_at_startup(
    tmp_path: Path,
) -> None:
    path = write_toml(
        tmp_path,
        device_id="laptop",
        relay_url=relay_url(free_port()),
        token=TOKEN,
        reconnect_delay=0.02,
        poll_interval=0.01,
        connect_timeout=0.2,
    )
    transport = RecordingTransport(fail_connects=10**6)
    clip = FakeClipboard()
    stop = threading.Event()
    timer = threading.Timer(0.4, stop.set)
    timer.start()
    try:
        with daemon_logs() as records:
            rc = daemon_main(
                ["--config", str(path)],
                clipboard_factory=lambda _cfg: clip,
                transport_factory=lambda _cfg: transport,
                stop_event=stop,
            )
    finally:
        timer.cancel()

    assert rc == 0
    assert transport.connect_calls >= 2
    assert transport.close_calls == 1
    assert clip.reads == 0
    assert warnings(records)


def test_daemon_main_recovers_when_the_relay_starts_later(tmp_path: Path) -> None:
    port = free_port()
    path = write_toml(
        tmp_path,
        device_id="laptop",
        relay_url=relay_url(port),
        token=TOKEN,
        reconnect_delay=0.02,
        poll_interval=0.02,
        connect_timeout=0.2,
    )
    clip = FakeClipboard()
    stop = threading.Event()
    results: list[int] = []
    thread = threading.Thread(
        target=lambda: results.append(
            daemon_main(
                ["--config", str(path)],
                clipboard_factory=lambda _cfg: clip,
                stop_event=stop,
            )
        ),
        name="resilience-late-relay",
    )
    thread.start()
    time.sleep(0.3)

    peer: RelayTransport | None = None
    try:
        with RelayServer(token=TOKEN, port=port) as server:
            peer = RelayTransport(relay_url(server.port), TOKEN, connect_timeout=0.2)
            peer.connect()
            clip.set_payload(text_payload("relay came back"))

            frames: list[bytes] = []
            deadline = time.monotonic() + 10.0
            while not frames and time.monotonic() < deadline:
                frames = peer.recv()
                time.sleep(0.01)
            assert frames, "daemon did not reconnect and sync once the relay was up"
            message = decode_frame(frames[0])
            assert message.origin == "laptop"
            assert message.payload == text_payload("relay came back")
    finally:
        if peer is not None:
            peer.close()
        stop.set()
        thread.join(10.0)

    assert not thread.is_alive()
    assert results == [0]


def test_daemon_process_exits_cleanly_on_sigterm_while_retrying(
    tmp_path: Path,
) -> None:
    path = write_toml(
        tmp_path,
        device_id="laptop",
        relay_url=relay_url(free_port()),
        token=TOKEN,
        reconnect_delay=0.05,
        poll_interval=0.05,
        connect_timeout=0.2,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "clipsync.daemon", "--config", str(path)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1.5)
        assert proc.poll() is None, "daemon exited instead of retrying a down relay"
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, err
    assert "Traceback" not in err
