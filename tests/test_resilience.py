"""Unit tests for daemon resilience, backoff policy, and wait_or_stop."""

from __future__ import annotations

import threading
import pytest

from clipsync.clipboard import FakeClipboard
from clipsync.config import DaemonConfig
from clipsync.daemon import BackoffPolicy, ClipboardDaemon, wait_or_stop
from clipsync.protocol import text_payload
from clipsync.transport import InMemoryTransport, TransportError


def test_backoff_policy_delay_calculation() -> None:
    policy = BackoffPolicy(base=0.5, max_delay=10.0, jitter=0.0)
    assert policy.delay(0) == 0.5
    assert policy.delay(1) == 1.0
    assert policy.delay(2) == 2.0
    assert policy.delay(3) == 4.0
    assert policy.delay(4) == 8.0
    assert policy.delay(5) == 10.0  # capped at max_delay


def test_backoff_policy_validation() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(base=0.0)
    with pytest.raises(ValueError):
        BackoffPolicy(base=-0.5)
    with pytest.raises(ValueError):
        BackoffPolicy(max_delay=0.1, base=0.2)
    with pytest.raises(ValueError):
        BackoffPolicy(jitter=-0.1)
    with pytest.raises(ValueError):
        BackoffPolicy(jitter=1.1)

    policy = BackoffPolicy()
    with pytest.raises(ValueError):
        policy.delay(-1)
    with pytest.raises(ValueError):
        policy.delay(True)  # type: ignore


def test_wait_or_stop_negative_delay() -> None:
    calls: list[float] = []
    assert wait_or_stop(None, -1.0, calls.append) is False
    assert calls == [0.0]


def test_wait_or_stop_with_event_set() -> None:
    event = threading.Event()
    event.set()
    calls: list[float] = []
    assert wait_or_stop(event, 5.0, calls.append) is True
    assert calls == []


class DummyTransport(InMemoryTransport):
    def __init__(self, fail_connect_count: int = 0) -> None:
        super().__init__()
        self.fail_connect_count = fail_connect_count
        self.connect_attempts = 0

    def connect(self) -> None:
        self.connect_attempts += 1
        if self.connect_attempts <= self.fail_connect_count:
            raise TransportError("Simulated connect failure")
        super().connect()


def test_connect_resilient_eventually_succeeds() -> None:
    t = DummyTransport(fail_connect_count=2)
    cfg = DaemonConfig(
        device_id="dev1",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        reconnect_delay=0.05,
    )
    daemon = ClipboardDaemon(
        cfg,
        clipboard=FakeClipboard(),
        transport=t,
        backoff=BackoffPolicy(base=0.01, max_delay=0.1, jitter=0.0),
    )
    sleeps: list[float] = []
    res = daemon.connect_resilient(sleep=sleeps.append)
    assert res is True
    assert t.connect_attempts == 3
    assert t.connected is True
    assert len(sleeps) == 2


def test_connect_resilient_hits_max_attempts() -> None:
    t = DummyTransport(fail_connect_count=5)
    cfg = DaemonConfig(
        device_id="dev1",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
    )
    daemon = ClipboardDaemon(
        cfg,
        clipboard=FakeClipboard(),
        transport=t,
        backoff=BackoffPolicy(base=0.01, max_delay=0.1, jitter=0.0),
    )
    res = daemon.connect_resilient(max_attempts=2, sleep=lambda _d: None)
    assert res is False
    assert t.connect_attempts == 2
    assert t.connected is False


def test_daemon_run_resilient_reconnects() -> None:
    t = DummyTransport(fail_connect_count=1)
    cfg = DaemonConfig(
        device_id="dev1",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        poll_interval=0.01,
    )
    daemon = ClipboardDaemon(
        cfg,
        clipboard=FakeClipboard(payload=text_payload("sync me")),
        transport=t,
        backoff=BackoffPolicy(base=0.01, max_delay=0.05, jitter=0.0),
    )
    sleeps: list[float] = []
    polls = daemon.run(max_iterations=3, sleep=sleeps.append)
    assert polls == 3
    assert t.connect_attempts == 2
    assert t.connected is True
