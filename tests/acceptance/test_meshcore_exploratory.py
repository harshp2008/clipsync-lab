"""Exploratory phase-2a check: copy made during a hub failover.

This is **not** part of the frozen acceptance gate. It is marked ``exploratory`` and
its result is reported in ``docs/orchestrator/REPORT.md``. It asks the design's own
question: when the hub has died but the replacement is not up yet, does a clipboard
copy survive and arrive after recovery?

Owner: orchestrator (frozen with the contract; never edited during the job).
"""

from __future__ import annotations

import socket
import time

import pytest

from clipsync.clipboard import FakeClipboard
from clipsync.config import DaemonConfig
from clipsync.daemon import ClipboardDaemon
from clipsync.mesh_transport import NodeTransport
from clipsync.protocol import text_payload
from meshcore.config import MeshConfig, PeerConfig
from meshcore.node import Node

TOKEN = "phase2a-exploratory-token"


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def pump_until(daemons, predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for daemon in daemons:
            daemon.poll_once()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.mark.exploratory
def test_copy_during_failover_is_delivered_after_recovery() -> None:
    ids = ["n0", "n1", "n2"]
    ports = [free_port() for _ in range(3)]
    nodes = []
    for i, device_id in enumerate(ids):
        peers = tuple(
            PeerConfig(
                device_id=other,
                name=other.upper(),
                address=f"127.0.0.1:{ports[j]}",
                perm=j,
            )
            for j, other in enumerate(ids)
            if j != i
        )
        nodes.append(
            Node(
                MeshConfig(
                    device_id=device_id,
                    name=device_id.upper(),
                    token=TOKEN,
                    perm=i,
                    bind="127.0.0.1",
                    port=ports[i],
                    peers=peers,
                    heartbeat=0.02,
                    missed_pongs=2,
                    probe_timeout=0.05,
                    debounce=0.2,
                    reconnect_delay=0.02,
                    connect_timeout=1.0,
                )
            )
        )
    clips = [FakeClipboard() for _ in nodes]
    daemons = [
        ClipboardDaemon(
            DaemonConfig(device_id=node.device_id, token=TOKEN),
            clipboard=clip,
            transport=NodeTransport(node),
        )
        for node, clip in zip(nodes, clips)
    ]
    try:
        for daemon in daemons:
            daemon.connect()
        assert wait_until(lambda: all(node.hub_id == "n0" for node in nodes))

        # Kill the hub, then copy while the replacement is still being elected.
        daemons[0].close()
        during_failover = text_payload("copied during the failover window")
        clips[2].set_payload(during_failover)
        assert nodes[1].hub_id != "n1", "new hub was already up; window missed"
        stats = daemons[2].poll_once()
        queued_during_failover = stats.queued

        assert wait_until(
            lambda: nodes[1].hub_id == "n1" and nodes[2].hub_id == "n1"
        ), [node.hub_id for node in nodes]
        delivered = pump_until(
            daemons[1:], lambda: clips[1].payload == during_failover
        )
        print(
            f"\nexploratory: queued_during_failover={queued_during_failover} "
            f"delivered={delivered}"
        )
        assert delivered, (
            "a copy taken after the hub died was lost during failover "
            f"(queued_during_failover={queued_during_failover})"
        )
    finally:
        for daemon in daemons:
            try:
                daemon.close()
            except Exception:  # noqa: BLE001
                pass
        for node in nodes:
            try:
                node.stop()
            except Exception:  # noqa: BLE001
                pass
