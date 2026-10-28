"""Acceptance tests for meshcore phase 2a: elected hub (A0-A7).

Contract: ``docs/design/coordination-v2.md`` sections 0, 3, 4, 6, 10, 13 and the
phase-2a interface contract in ``docs/design/meshcore-phase2a.md``.

These tests are frozen before the implementation job. They use **only** ``meshcore``
(no ``clipsync`` import anywhere in this module), opaque byte payloads, in-process
``Node`` objects on loopback ports and tiny timings passed through ``MeshConfig``.
They never touch the real clipboard, a non-loopback address, or any service.

Owner: orchestrator (written and committed before the implementation job; do not
edit during it).
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import pytest

from meshcore.config import MeshConfig, PeerConfig
from meshcore.link import Link, relay_url
from meshcore.messages import (
    Hello,
    decode_leaving,
    decode_roster,
    encode_leaving,
    encode_roster,
)
from meshcore.node import Node

TOKEN = "phase2a-acceptance-token"

# Tiny timings so a full failover/handback stays inside a test.
HEARTBEAT = 0.02
MISSED_PONGS = 2
PROBE_TIMEOUT = 0.05
RECONNECT_DELAY = 0.02
CONNECT_TIMEOUT = 1.0


# -- helpers ----------------------------------------------------------------


def free_port() -> int:
    """Reserve an ephemeral loopback port for a node to listen on later."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def wait_until(predicate, timeout: float = 6.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class Recorder:
    """Thread-safe list of callback arguments (with arrival times)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[tuple] = []

    def __call__(self, *args) -> None:
        with self._lock:
            self.items.append((time.monotonic(), *args))

    def snapshot(self) -> list[tuple]:
        with self._lock:
            return list(self.items)

    def payloads(self) -> list[bytes]:
        return [item[-1] for item in self.snapshot()]

    def contains(self, payload: bytes) -> bool:
        return payload in self.payloads()

    def senders(self) -> list:
        return [item[-2] for item in self.snapshot()]


class NodeView:
    """A Node plus its callbacks, so tests never touch Node internals."""

    def __init__(self, node: Node) -> None:
        self.node = node
        self.received = Recorder()
        self.hubs = Recorder()
        node.on_receive(self.received)
        node.on_hub_changed(self.hubs)

    @property
    def device_id(self) -> str:
        return self.node.device_id

    @property
    def hub_id(self):
        return self.node.hub_id

    @property
    def is_hub(self) -> bool:
        return self.node.is_hub

    def start(self) -> None:
        self.node.start()

    def stop(self) -> None:
        self.node.stop()

    def publish(self, payload: bytes) -> None:
        self.node.publish(payload)

    def roster(self) -> list[Hello]:
        return self.node.roster()

    def hub_events(self) -> list[str]:
        return [item[1] for item in self.hubs.snapshot()]


class Cluster:
    def __init__(self, nodes: list[NodeView], ports: list[int], configs: list[MeshConfig]):
        self.nodes = nodes
        self.ports = ports
        self.configs = configs


def build_cluster(
    specs: list[dict],
    *,
    debounce: float = 0.2,
    can_reach=None,
    peer_overrides: dict[tuple[int, int], dict] | None = None,
) -> Cluster:
    """Build one Node per spec; every node statically knows every other node.

    ``spec`` keys: device_id, perm, temp_rank, will_hub, extra (MeshConfig kwargs).
    ``peer_overrides[(i, j)]`` overrides the *configured* peer values node i has
    for node j (a stale value that a real hello must later override).
    """
    overrides = peer_overrides or {}
    ports = [free_port() for _ in specs]
    configs: list[MeshConfig] = []
    for i, spec in enumerate(specs):
        peers = []
        for j, other in enumerate(specs):
            if i == j:
                continue
            override = overrides.get((i, j), {})
            peers.append(
                PeerConfig(
                    device_id=other["device_id"],
                    name=other.get("name", other["device_id"].upper()),
                    address=f"127.0.0.1:{ports[j]}",
                    perm=override.get("perm", other.get("perm")),
                    temp_rank=override.get("temp_rank", other.get("temp_rank")),
                    will_hub=override.get("will_hub", other.get("will_hub", True)),
                )
            )
        config = MeshConfig(
            device_id=spec["device_id"],
            name=spec.get("name", spec["device_id"].upper()),
            token=TOKEN,
            perm=spec.get("perm"),
            temp_rank=spec.get("temp_rank"),
            will_hub=spec.get("will_hub", True),
            bind="127.0.0.1",
            port=ports[i],
            peers=tuple(peers),
            heartbeat=HEARTBEAT,
            missed_pongs=MISSED_PONGS,
            probe_timeout=PROBE_TIMEOUT,
            debounce=debounce,
            reconnect_delay=RECONNECT_DELAY,
            connect_timeout=CONNECT_TIMEOUT,
            **spec.get("extra", {}),
        )
        configs.append(config)
    views = [NodeView(Node(config, can_reach=can_reach)) for config in configs]
    return Cluster(views, ports, configs)


def start_all(cluster: Cluster) -> Cluster:
    for view in cluster.nodes:
        view.start()
    return cluster


def stop_all(cluster: Cluster) -> None:
    for view in cluster.nodes:
        try:
            view.stop()
        except Exception:  # noqa: BLE001 - cleanup must never mask the assertion
            pass


def publish_retry(view: NodeView, payload: bytes, timeout: float = 5.0) -> None:
    """Publish, retrying while the node is still converging (no route yet)."""
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            view.publish(payload)
            return
        except Exception as exc:  # noqa: BLE001 - NodeError while converging
            last = exc
            time.sleep(0.01)
    raise AssertionError(f"publish never succeeded: {last!r}")


def receive(view: NodeView, payload: bytes, timeout: float = 6.0) -> bool:
    return wait_until(lambda: view.received.contains(payload), timeout=timeout)


@pytest.fixture()
def cluster_cleanup():
    created: list[Cluster] = []
    yield created
    for cluster in created:
        stop_all(cluster)


# -- A0: separation proof ---------------------------------------------------


def test_a0_three_meshcore_nodes_elect_and_fail_over(cluster_cleanup) -> None:
    """Three Nodes exchange arbitrary bytes, elect N0, and fail over to N1."""
    cluster = build_cluster(
        [
            {"device_id": "n0", "perm": 0},
            {"device_id": "n1", "perm": 1},
            {"device_id": "n2", "perm": 2},
        ]
    )
    cluster_cleanup.append(cluster)
    start_all(cluster)
    n0, n1, n2 = cluster.nodes

    assert wait_until(lambda: all(n.hub_id == "n0" for n in cluster.nodes)), [
        (n.device_id, n.hub_id) for n in cluster.nodes
    ]
    assert n0.hub_events() == ["n0"]

    first = b"\x00\x01meshcore opaque \xff\xfe" + bytes(range(32))
    publish_retry(n2, first)
    assert receive(n1, first)
    assert n1.received.senders()[-1] == "n2"

    n0.stop()
    assert wait_until(lambda: n1.hub_id == "n1" and n2.hub_id == "n1"), [
        n.hub_id for n in cluster.nodes
    ]
    assert n1.is_hub

    second = b"after-failover\x00payload"
    publish_retry(n2, second)
    assert receive(n1, second)


# -- A1: hello over the wire and roster -------------------------------------


def test_a1_hello_fields_reach_the_roster_and_override_stale_peer_config(
    cluster_cleanup,
) -> None:
    cluster = build_cluster(
        [
            {"device_id": "n0", "perm": 0},
            {"device_id": "n1", "perm": 1, "temp_rank": 7},
            {"device_id": "n2", "perm": 2},
        ],
        # N2's static config claims N1 is perm 7 and declines to hub. N1's real
        # hello says perm 1 / will_hub true and must win for this session.
        peer_overrides={(2, 1): {"perm": 7, "will_hub": False}},
    )
    cluster_cleanup.append(cluster)
    start_all(cluster)
    n0, n1, n2 = cluster.nodes

    assert wait_until(lambda: all(n.hub_id == "n0" for n in cluster.nodes))

    assert wait_until(
        lambda: {hello.device_id for hello in n0.roster()} >= {"n0", "n1", "n2"}
    )
    hub_roster = {hello.device_id: hello for hello in n0.roster()}
    assert hub_roster["n1"] == Hello("n1", "N1", perm=1, temp_rank=7, will_hub=True)
    assert hub_roster["n2"] == Hello("n2", "N2", perm=2)

    assert wait_until(
        lambda: {hello.device_id for hello in n2.roster()} >= {"n0", "n1", "n2"}
    )
    client_roster = {hello.device_id: hello for hello in n2.roster()}
    assert client_roster["n1"].perm == 1
    assert client_roster["n1"].will_hub is True

    # The roster/leaving encoders are the same ones used on the wire.
    hellos = [
        Hello("a", "A", perm=1),
        Hello("b", "B", temp_rank=2, will_hub=False),
        Hello("c", "C"),
    ]
    assert decode_roster(encode_roster(hellos)) == hellos
    assert decode_leaving(encode_leaving()) is True
    with pytest.raises(ValueError):
        decode_leaving("this is not a control message")

    # Leaving / disconnect removes the device from the roster on every node.
    n1.stop()
    assert wait_until(lambda: "n1" not in {h.device_id for h in n0.roster()})
    assert wait_until(lambda: "n1" not in {h.device_id for h in n2.roster()})


# -- A2: old client without Hello -------------------------------------------


def test_a2_old_client_without_hello_still_connects_and_syncs(
    cluster_cleanup,
) -> None:
    cluster = build_cluster(
        [{"device_id": "n0", "perm": 0}, {"device_id": "n1", "perm": 1}]
    )
    cluster_cleanup.append(cluster)
    start_all(cluster)
    n0, n1 = cluster.nodes
    assert wait_until(lambda: n1.hub_id == "n0")

    old = Link(
        relay_url(cluster.ports[0]),
        TOKEN,
        hello=None,
        reconnect=False,
        connect_timeout=CONNECT_TIMEOUT,
        thread_name="acceptance-old-client",
    )
    try:
        old.connect()
        assert old.connected

        first = b"old client says hello through the hub"
        old.send(first)
        assert receive(n1, first)
        assert receive(n0, first)
        assert n1.received.senders()[-1] is None

        reply = b"mesh client replies to the old client"
        publish_retry(n1, reply)
        assert wait_until(lambda: old.recv() == [reply])

        # The old client never announced itself, so it is not in the roster.
        assert wait_until(
            lambda: {h.device_id for h in n0.roster()} == {"n0", "n1"}
        )
        assert n0.received.contains(first)
    finally:
        old.close()


# -- A3: elected hub forwards between peers --------------------------------


def test_a3_payload_from_n2_reaches_n1_through_n0(cluster_cleanup) -> None:
    cluster = build_cluster(
        [
            {"device_id": "n0", "perm": 0},
            {"device_id": "n1", "perm": 1},
            {"device_id": "n2", "perm": 2},
        ]
    )
    cluster_cleanup.append(cluster)
    start_all(cluster)
    n0, n1, n2 = cluster.nodes

    assert wait_until(lambda: all(n.hub_id == "n0" for n in cluster.nodes))
    assert n0.is_hub and not n1.is_hub and not n2.is_hub

    payload = b"clipboard-ish bytes from n2"
    publish_retry(n2, payload)
    assert receive(n1, payload)
    assert receive(n0, payload)
    assert not n2.received.contains(payload)  # no echo to the sender
    assert n1.received.senders()[-1] == "n2"


# -- A4: hub failover -------------------------------------------------------


def test_a4_hub_failover_reconnects_without_restarting_clients(
    cluster_cleanup,
) -> None:
    cluster = build_cluster(
        [
            {"device_id": "n0", "perm": 0},
            {"device_id": "n1", "perm": 1},
            {"device_id": "n2", "perm": 2},
        ]
    )
    cluster_cleanup.append(cluster)
    start_all(cluster)
    n0, n1, n2 = cluster.nodes
    assert wait_until(lambda: all(n.hub_id == "n0" for n in cluster.nodes))

    n0.stop()
    assert wait_until(lambda: n1.hub_id == "n1" and n2.hub_id == "n1"), [
        n.hub_id for n in cluster.nodes
    ]
    assert n1.is_hub

    payload = b"sync resumes on the new hub"
    publish_retry(n2, payload)
    assert receive(n1, payload)


# -- A5: handback after the debounce, flap does not migrate ----------------


def test_a5_handback_after_debounce_and_flap_without_migration(
    cluster_cleanup,
) -> None:
    debounce = 0.8
    cluster = build_cluster(
        [
            {"device_id": "n0", "perm": 0},
            {"device_id": "n1", "perm": 1},
            {"device_id": "n2", "perm": 2},
        ],
        debounce=debounce,
    )
    cluster_cleanup.append(cluster)
    start_all(cluster)
    n0, n1, n2 = cluster.nodes
    assert wait_until(lambda: all(n.hub_id == "n0" for n in cluster.nodes))

    # Fail over to N1 first.
    n0.stop()
    assert wait_until(
        lambda: n1.hub_id == "n1" and n2.hub_id == "n1", timeout=8.0
    )
    baseline_events = len(n1.hub_events())
    baseline_hub = n1.hub_id

    # Flap: N0 is visible for less than the debounce window. Nothing may migrate.
    flap = NodeView(Node(cluster.configs[0]))
    cluster.nodes.append(flap)
    flap.start()
    time.sleep(0.3)
    assert n1.hub_id == baseline_hub, "migrated before the 30 s debounce equivalent"
    flap.stop()
    time.sleep(debounce + 0.4)
    assert n1.hub_id == baseline_hub, "a flap inside the debounce window migrated"
    assert n2.hub_id == baseline_hub
    assert len(n1.hub_events()) == baseline_events

    # N0 returns for good: nothing migrates before the debounce ...
    returned = NodeView(Node(cluster.configs[0]))
    cluster.nodes.append(returned)
    returned.start()
    time.sleep(debounce / 2)
    assert n1.hub_id == baseline_hub, "handback happened before the debounce"

    # ... then N1 and N2 migrate to N0.
    assert wait_until(
        lambda: n1.hub_id == "n0" and n2.hub_id == "n0", timeout=8.0
    ), [n.hub_id for n in cluster.nodes]
    assert returned.hub_id == "n0"

    payload = b"sync after handback"
    publish_retry(n2, payload)
    assert receive(n1, payload)


# -- A6: will_hub false and the forced-hub warning --------------------------


def test_a6_will_hub_false_is_skipped_and_all_false_forces_a_hub(
    cluster_cleanup,
) -> None:
    cluster = build_cluster(
        [
            {"device_id": "n0", "perm": 0, "will_hub": True},
            {"device_id": "n1", "perm": 1, "will_hub": False},
            {"device_id": "n2", "perm": 2, "will_hub": True},
        ]
    )
    cluster_cleanup.append(cluster)
    start_all(cluster)
    n0, n1, n2 = cluster.nodes
    assert wait_until(lambda: all(n.hub_id == "n0" for n in cluster.nodes))

    # Attach a handler directly to the meshcore logger instead of using caplog.
    # Third-party pytest plugins (ROS's launch_testing) set the global logger
    # class to launch.logging.LaunchLogger, which forces propagate=False on new
    # loggers, so their records never reach caplog's root handler. The contract
    # below is unchanged: a WARNING containing "forced" and the device id.
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    election_logger = logging.getLogger("meshcore.election")
    handler = _ListHandler()
    previous_level = election_logger.level
    election_logger.addHandler(handler)
    election_logger.setLevel(logging.WARNING)
    try:
        n0.stop()
        assert wait_until(lambda: n2.hub_id == "n2", timeout=8.0), [
            n.hub_id for n in cluster.nodes
        ]
        assert n1.hub_id == "n2"
        assert not n1.is_hub
        assert "n1" not in n1.hub_events()

        # Nobody willing is left: the first reachable device is forced and warns.
        n2.stop()
        assert wait_until(lambda: n1.hub_id == "n1", timeout=8.0), [
            n.hub_id for n in cluster.nodes
        ]
        assert n1.is_hub
    finally:
        election_logger.removeHandler(handler)
        election_logger.setLevel(previous_level)

    forced = [
        record.getMessage()
        for record in records
        if record.levelno >= logging.WARNING
        and "forced" in record.getMessage().lower()
    ]
    assert forced, [record.getMessage() for record in records]
    assert any("n1" in message for message in forced)


# -- A7: partition and heal -------------------------------------------------


class Partition:
    """Injected reachability seam: two fragments until ``heal()``."""

    def __init__(self, left: set[str], right: set[str]) -> None:
        self._lock = threading.Lock()
        self._groups = [left, right]
        self._healed = False

    def __call__(self, a: str, b: str) -> bool:
        with self._lock:
            if self._healed:
                return True
            return any(a in group and b in group for group in self._groups)

    def heal(self) -> None:
        with self._lock:
            self._healed = True


def test_a7_partition_elects_two_hubs_then_heals_to_one(
    cluster_cleanup,
) -> None:
    partition = Partition({"n0", "n1"}, {"n2", "n3"})
    cluster = build_cluster(
        [
            {"device_id": "n0", "perm": 0},
            {"device_id": "n1", "perm": 1},
            {"device_id": "n2", "perm": 2},
            {"device_id": "n3", "perm": 3},
        ],
        can_reach=partition,
    )
    cluster_cleanup.append(cluster)
    start_all(cluster)
    n0, n1, n2, n3 = cluster.nodes

    # Each fragment elects its own hub.
    assert wait_until(lambda: n0.hub_id == "n0" and n1.hub_id == "n0"), [
        n.hub_id for n in cluster.nodes
    ]
    assert wait_until(lambda: n2.hub_id == "n2" and n3.hub_id == "n2"), [
        n.hub_id for n in cluster.nodes
    ]

    left = b"inside the left fragment"
    publish_retry(n1, left)
    assert receive(n0, left)
    assert not n3.received.contains(left)

    right = b"inside the right fragment"
    publish_retry(n3, right)
    assert receive(n2, right)
    assert not n1.received.contains(right)

    # Heal: the sort order merges the two fragments onto the first device.
    partition.heal()
    assert wait_until(
        lambda: all(n.hub_id == "n0" for n in cluster.nodes), timeout=10.0
    ), [(n.device_id, n.hub_id) for n in cluster.nodes]
    assert [n.device_id for n in cluster.nodes if n.is_hub] == ["n0"]

    healed = b"payloads flow across the healed partition"
    publish_retry(n3, healed)
    assert receive(n1, healed)
    assert receive(n2, healed)
    assert not n3.received.contains(healed)

    from_n0 = b"and back the other way"
    publish_retry(n0, from_n0)
    assert receive(n3, from_n0)
