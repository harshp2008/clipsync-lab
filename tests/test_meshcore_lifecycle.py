"""Lifecycle and log-noise tests for meshcore HubServer, Node and probe."""

from __future__ import annotations

import logging
import socket
import threading
import time

from meshcore.config import MeshConfig
from meshcore.hub import HubServer
from meshcore.link import probe_hub
from meshcore.node import Node

TOKEN = "lifecycle-test-token"


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def busy_threads(names: tuple[str, ...]) -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t.is_alive() and t.name in names
    ]


def test_hub_stop_is_idempotent_and_leaves_no_thread() -> None:
    """start()/stop() twice must close the server and join its loop thread."""
    server = HubServer(token=TOKEN, port=0)
    server.start()
    assert server.running
    assert busy_threads(("meshcore-hub-server",))

    server.stop()
    server.stop()

    assert not server.running
    assert wait_until(
        lambda: not busy_threads(("meshcore-hub-server",))
    ), "meshcore-hub-server thread survived stop()"


def test_node_stop_is_idempotent_and_leaves_no_thread() -> None:
    """A started-then-stopped Node leaves neither election nor hub threads."""
    node = Node(
        MeshConfig(device_id="solo", name="Solo", token=TOKEN, port=0, peers=())
    )
    node.start()
    try:
        assert wait_until(lambda: node.is_hub)
        assert busy_threads(("meshcore-election", "meshcore-hub-server"))
    finally:
        node.stop()
        node.stop()

    assert not node.is_hub
    assert wait_until(
        lambda: not busy_threads(("meshcore-election", "meshcore-hub-server"))
    ), "meshcore thread survived Node.stop()"


def test_probe_against_live_hub_emits_no_warning_or_error() -> None:
    """A probe against a live hub must not surface websockets/meshcore warnings."""
    records: list[logging.LogRecord] = []

    class _Recorder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Recorder()
    loggers = [logging.getLogger("websockets"), logging.getLogger("meshcore")]
    previous_levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

    server = HubServer(token=TOKEN, port=0)
    server.start()
    try:
        url = f"ws://127.0.0.1:{server.port}/"
        assert probe_hub(url, TOKEN, timeout=1.0) is True
    finally:
        for logger, level in zip(loggers, previous_levels):
            logger.removeHandler(handler)
            logger.setLevel(level)
        server.stop()

    # meshcore must not change another library's logger level (the probe is a
    # real handshake, so websockets' own INFO chatter stays at its default).
    assert logging.getLogger("websockets.server").level == logging.NOTSET

    noisy = [
        record
        for record in records
        if record.levelno >= logging.WARNING or record.exc_info is not None
    ]
    assert not noisy, [
        f"{record.name}: {record.levelname}: {record.getMessage()}"
        for record in noisy
    ]


def test_probe_timeout_is_bounded_for_a_dead_port() -> None:
    """A closed port fails within the requested probe timeout."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    finally:
        sock.close()

    started = time.monotonic()
    assert probe_hub(f"ws://127.0.0.1:{port}/", TOKEN, timeout=0.2) is False
    assert time.monotonic() - started < 2.0
