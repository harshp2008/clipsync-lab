"""Frozen acceptance tests for the meshcore state channel (S1-S14).

Contract: ``docs/design/meshcore-state.md``. These tests were written and committed
BEFORE the implementation job; the implementation must not edit this file. They use
the production API only (``Node.publish_state`` / ``on_state`` / ``get_state`` /
``get_state_version`` / ``state_stats``, ``MeshStateTransport``), in-process Nodes on
loopback with ports from an ephemeral bind-to-0 reservation, injected wall clocks
where time matters, and ``FakeClipboard`` (never the real clipboard). Every cross-node
assertion waits with ``wait_until(..., 8.0)``; negative assertions first wait for the
positive side and then observe a short quiet window.

Frozen API under test (all new, all missing today, so the whole file is red):

* ``Node(config, wall_clock=...)``: injectable wall clock for the HLC.
* ``Node.publish_state(key, payload)``: local-first last-writer-wins publish; raises
  ``NodeError`` for a relay, an invalid key or a value above the size cap; never
  raises because the node is detached; works before ``start()`` / after ``stop()``.
* ``Node.on_state(callback(key, sender_id, payload))``.
* ``Node.get_state(key)`` / ``Node.get_state_version(key)``.
* ``Node.state_stats``: ``published, sent, received, applied, ignored,
  rejected_skew, rejected_invalid``.
* ``meshcore.coordination``: ``MAX_STATE_PAYLOAD_BYTES = 8 MiB``,
  ``MAX_CLOCK_AHEAD_S = 300``.
* ``meshcore.messages``: ``STATE_MAGIC``, ``encode_state_sync()``,
  ``decode_state_frame(frame) -> (key, version, body)``.
* ``meshcore.link.Link.send_control(text)``: the state-capability control frame.
* ``clipsync.state_transport.MeshStateTransport(node, key="clipboard")``.

Existing tests are untouched; this file is the only addition to ``tests/acceptance``.
S12-S14 pin the owner amendments A1 (true HLC, accepted floor) and A2 (non-sticky
skew handling, clock correction) on top of the original S1-S11 contract.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from cryptography.fernet import Fernet

from clipsync.clipboard import FakeClipboard
from clipsync.config import DaemonConfig
from clipsync.daemon import ClipboardDaemon
from clipsync.protocol import text_payload
from meshcore.config import GroupConfig, MeshConfig, PeerConfig, RelayConfig
from meshcore.link import Link, relay_url
from meshcore.messages import Hello
from meshcore.node import Node, NodeError

TOKEN = "state-channel-token"
TOK_A = "state-channel-token-a"
TOK_B = "state-channel-token-b"
KEY_A = Fernet.generate_key().decode("ascii")
KEY_B = Fernet.generate_key().decode("ascii")
KEY_OTHER = Fernet.generate_key().decode("ascii")

# Tiny timings so an election or a failover stays inside a test.
HEARTBEAT = 0.02
MISSED_PONGS = 2
PROBE_TIMEOUT = 0.05
DEBOUNCE = 0.2
RECONNECT_DELAY = 0.02
CONNECT_TIMEOUT = 1.0
QUIET = 0.3


# -- helpers ----------------------------------------------------------------


def free_port() -> int:
    """Reserve an ephemeral loopback port for a node to listen on later."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def wait_until(predicate, timeout: float = 8.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class Clock:
    """Injectable wall clock: ``base + offset``; the offset is mutable."""

    def __init__(self, base: float, offset: float = 0.0) -> None:
        self.base = base
        self.offset = offset

    def __call__(self) -> float:
        return self.base + self.offset


class Recorder:
    """Thread-safe callback recorder: each item is ``(monotonic, *args)``."""

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


def stop_nodes(nodes: list[Node]) -> None:
    for node in nodes:
        try:
            node.stop()
        except Exception:  # noqa: BLE001 - cleanup must not mask assertions
            pass


def mesh_config(
    device_id: str,
    port: int,
    *,
    perms: list[int],
    peers: list[tuple[str, int, int]],
    group_id: str = "default",
    token: str = TOKEN,
    e2e_key: str | None = None,
    relay: RelayConfig | None = None,
) -> MeshConfig:
    """One member config; ``peers`` items are (device_id, port, perm)."""
    return MeshConfig(
        device_id=device_id,
        name=device_id.upper(),
        token=token,
        perm=perms[int(device_id[1:])],
        group_id=group_id,
        e2e_key=e2e_key,
        relay=relay,
        bind="127.0.0.1",
        port=port,
        peers=tuple(
            PeerConfig(
                device_id=peer_id,
                name=peer_id.upper(),
                address=f"127.0.0.1:{peer_port}",
                perm=peer_perm,
            )
            for peer_id, peer_port, peer_perm in peers
        ),
        heartbeat=HEARTBEAT,
        missed_pongs=MISSED_PONGS,
        probe_timeout=PROBE_TIMEOUT,
        debounce=DEBOUNCE,
        reconnect_delay=RECONNECT_DELAY,
        connect_timeout=CONNECT_TIMEOUT,
    )


def build_nodes(
    count: int,
    *,
    perms: list[int] | None = None,
    clocks: list[Clock] | None = None,
    e2e_key: str | None = None,
) -> tuple[list[Node], list[MeshConfig]]:
    """Create ``count`` member Nodes (``n0..n{(count-1)}``) on loopback.

    ``perms`` lets a test choose the election order; ``clocks`` injects one wall clock
    per node. Ports are reserved with an ephemeral bind and used later by the node.
    """
    ids = [f"n{i}" for i in range(count)]
    if perms is None:
        perms = list(range(count))
    assert len(perms) == count
    ports = [free_port() for _ in range(count)]
    nodes: list[Node] = []
    configs: list[MeshConfig] = []
    for index, device_id in enumerate(ids):
        peers = [
            (ids[other], ports[other], perms[other])
            for other in range(count)
            if other != index
        ]
        config = mesh_config(
            device_id, ports[index], perms=perms, peers=peers, e2e_key=e2e_key
        )
        kwargs = {}
        if clocks is not None:
            kwargs["wall_clock"] = clocks[index]
        nodes.append(Node(config, **kwargs))
        configs.append(config)
    return nodes, configs


def member_node(
    device_id: str,
    *,
    group_id: str,
    token: str,
    e2e_key: str | None = None,
    relay: RelayConfig | None = None,
) -> Node:
    """A member with no static peers that attaches to a preferred relay."""
    config = MeshConfig(
        device_id=device_id,
        name=device_id.upper(),
        token=token,
        group_id=group_id,
        e2e_key=e2e_key,
        relay=relay,
        bind="127.0.0.1",
        port=free_port(),
        peers=(),
        heartbeat=HEARTBEAT,
        missed_pongs=MISSED_PONGS,
        probe_timeout=PROBE_TIMEOUT,
        debounce=DEBOUNCE,
        reconnect_delay=RECONNECT_DELAY,
        connect_timeout=CONNECT_TIMEOUT,
    )
    return Node(config)


def pump_until(daemons: list[ClipboardDaemon], predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for daemon in daemons:
            daemon.poll_once()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def pump_for(daemons: list[ClipboardDaemon], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for daemon in daemons:
            daemon.poll_once()
        time.sleep(0.01)


# -- S1: a value published before the member ever attached ------------------


def test_s1_state_published_before_attach_arrives_as_the_attach_snapshot() -> None:
    nodes, _configs = build_nodes(3)  # n0 hub, n1 A, n2 B
    payload = b"published before B attached"
    try:
        nodes[0].start()
        nodes[1].start()
        assert wait_until(lambda: nodes[1].hub_id == "n0"), nodes[1].hub_id
        nodes[1].publish_state("clipboard", payload)
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == payload
        ), nodes[0].get_state("clipboard")
        version = nodes[1].get_state_version("clipboard")
        assert version is not None
        assert nodes[1].state_stats.published == 1

        nodes[2].start()  # B attaches for the first time
        assert wait_until(
            lambda: nodes[2].get_state("clipboard") == payload
        ), nodes[2].get_state("clipboard")
        assert nodes[2].get_state_version("clipboard") == version
        # Exactly one frame reached B: the attach snapshot. Nothing was retried.
        assert nodes[2].state_stats.received == 1
        assert nodes[2].state_stats.sent == 0
        time.sleep(QUIET)
        assert nodes[2].state_stats.received == 1
    finally:
        stop_nodes(nodes)


# -- S2: publish in a hub handover gap, delivered once, no retry ------------


def test_s2_handover_gap_copy_reaches_every_online_member_without_a_retry() -> None:
    # n0 is the hub; n2 is next (perm 1) so n2 becomes the new hub; n1 = A (perm 2).
    nodes, _configs = build_nodes(4, perms=[0, 2, 1, 3])
    payload = b"copied during the handover gap"
    try:
        for node in nodes:
            node.start()
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in nodes)
        ), [node.hub_id for node in nodes]

        nodes[0].stop()  # the hub dies
        nodes[1].publish_state("clipboard", payload)  # single copy, no retry
        assert wait_until(lambda: nodes[2].is_hub), [node.is_hub for node in nodes]
        assert nodes[2].hub_id == "n2", nodes[2].hub_id

        online = (nodes[1], nodes[2], nodes[3])
        assert wait_until(
            lambda: all(node.get_state("clipboard") == payload for node in online)
        ), [node.get_state("clipboard") for node in online]
        version = nodes[1].get_state_version("clipboard")
        assert version is not None
        assert all(node.get_state_version("clipboard") == version for node in online)
        assert nodes[1].state_stats.published == 1
    finally:
        stop_nodes(nodes)


# -- S3: offline publishes converge to the latest, never resurrect ----------


def test_s3_offline_publishes_converge_to_the_latest_and_never_resurrect() -> None:
    nodes, _configs = build_nodes(3)  # n0 hub, n1 A, n2 C
    seeded = [b"offline-1", b"offline-2", b"offline-3"]
    try:
        for node in nodes:
            node.start()
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in nodes)
        ), [node.hub_id for node in nodes]

        nodes[1].stop()  # A is offline
        versions = []
        for payload in seeded:
            nodes[1].publish_state("clipboard", payload)
            versions.append(nodes[1].get_state_version("clipboard"))
        assert nodes[1].get_state("clipboard") == seeded[-1]
        assert all(version is not None for version in versions)
        # Nothing left the offline node: the register is the only outbox.
        assert nodes[1].state_stats.sent == 0

        nodes[1].start()  # reconnect: push the latest only
        assert wait_until(
            lambda: nodes[0].get_state_version("clipboard") == versions[-1]
        ), nodes[0].get_state_version("clipboard")
        assert wait_until(
            lambda: nodes[2].get_state("clipboard") == seeded[-1]
        ), nodes[2].get_state("clipboard")
        for node in (nodes[0], nodes[1], nodes[2]):
            assert node.get_state("clipboard") == seeded[-1]
            assert node.get_state_version("clipboard") == versions[-1]
        # The superseded values are not resurrected anywhere.
        assert nodes[0].get_state_version("clipboard") not in versions[:2]
        assert nodes[2].get_state_version("clipboard") not in versions[:2]
    finally:
        stop_nodes(nodes)


# -- S4: concurrent copies, deterministic winner, both arrival orders --------


def _concurrent_case(arrival_order: str) -> None:
    fixed = time.time()
    clock = Clock(fixed)
    nodes, _configs = build_nodes(4, clocks=[clock] * 4)
    from_a = b"same-instant copy from n1"
    from_b = b"same-instant copy from n2"  # greater author, so the winner
    try:
        nodes[0].start()  # hub
        nodes[3].start()  # third member C
        assert wait_until(lambda: nodes[3].hub_id == "n0"), nodes[3].hub_id

        # Both copies are stamped with the same injected wall clock while the
        # publishers are detached, so the writes are genuinely concurrent: neither
        # can have observed the other. The arrival order is then varied by start
        # order, which is the order in which the hub receives the attach pushes.
        nodes[1].publish_state("clipboard", from_a)
        nodes[2].publish_state("clipboard", from_b)
        assert nodes[1].state_stats.published == 1
        assert nodes[2].state_stats.published == 1
        first, second = (
            (nodes[1], nodes[2]) if arrival_order == "ab" else (nodes[2], nodes[1])
        )
        first.start()
        second.start()

        assert wait_until(
            lambda: all(node.get_state("clipboard") == from_b for node in nodes)
        ), [node.get_state("clipboard") for node in nodes]
        version = nodes[2].get_state_version("clipboard")
        assert version is not None
        assert all(node.get_state_version("clipboard") == version for node in nodes)
        time.sleep(QUIET)
        assert all(node.get_state("clipboard") == from_b for node in nodes)
    finally:
        stop_nodes(nodes)


def test_s4_concurrent_copies_converge_to_the_same_deterministic_winner() -> None:
    _concurrent_case("ab")
    _concurrent_case("ba")


# -- S5: the documented clock-skew bound ------------------------------------


def test_s5_a_clock_skewed_device_cannot_win_past_the_documented_bound() -> None:
    from meshcore.coordination import MAX_CLOCK_AHEAD_S

    assert MAX_CLOCK_AHEAD_S == 300, "the documented bound is 300 seconds"

    fixed = time.time()
    hub_clock = Clock(fixed)
    a_clock = Clock(fixed)
    skewed_clock = Clock(fixed, offset=3600.0)  # one hour ahead
    nodes, _configs = build_nodes(
        3, clocks=[hub_clock, a_clock, skewed_clock]
    )  # n0 hub, n1 honest, n2 skewed
    try:
        for node in nodes:
            node.start()
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in nodes)
        ), [node.hub_id for node in nodes]

        nodes[1].publish_state("clipboard", b"honest-1")
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == b"honest-1"
        ), nodes[0].get_state("clipboard")

        nodes[2].publish_state("clipboard", b"one-hour-ahead")
        assert wait_until(lambda: nodes[0].state_stats.rejected_skew >= 1)
        assert nodes[0].get_state("clipboard") == b"honest-1"
        assert nodes[1].get_state("clipboard") == b"honest-1"

        # 240 s ahead is inside the bound: accepted.
        a_clock.offset = 240.0
        nodes[1].publish_state("clipboard", b"within-bound")
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == b"within-bound"
        ), nodes[0].get_state("clipboard")

        # 360 s ahead is outside the bound: rejected, the register does not move.
        a_clock.offset = 360.0
        nodes[1].publish_state("clipboard", b"beyond-bound")
        assert wait_until(lambda: nodes[0].state_stats.rejected_skew >= 2)
        assert nodes[0].get_state("clipboard") == b"within-bound"

        # A later honest copy still wins: the one-hour-ahead device never overrides.
        hub_clock.offset = 700.0
        a_clock.offset = 900.0
        nodes[1].publish_state("clipboard", b"honest-late")
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == b"honest-late"
        ), nodes[0].get_state("clipboard")
        assert wait_until(
            lambda: nodes[1].get_state("clipboard") == b"honest-late"
        ), nodes[1].get_state("clipboard")

        # Documented quarantine: the misclocked device keeps its own value until
        # its clock is fixed and the process restarts.
        assert nodes[2].get_state("clipboard") == b"one-hour-ahead"
    finally:
        stop_nodes(nodes)


# -- S6: hub restart with an empty register ---------------------------------


def test_s6_hub_restart_with_empty_memory_converges_without_a_manual_step() -> None:
    nodes, configs = build_nodes(3)  # n0 hub, n1, n2
    fresh: Node | None = None
    payload = b"newest value before the hub restart"
    try:
        for node in nodes:
            node.start()
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in nodes)
        ), [node.hub_id for node in nodes]
        nodes[2].publish_state("clipboard", payload)
        assert wait_until(
            lambda: all(node.get_state("clipboard") == payload for node in nodes)
        ), [node.get_state("clipboard") for node in nodes]
        version = nodes[2].get_state_version("clipboard")
        assert version is not None

        nodes[0].stop()  # the hub process dies
        assert wait_until(lambda: nodes[1].is_hub or nodes[2].is_hub)

        fresh = Node(configs[0])  # same device id and port, empty memory
        assert fresh.get_state("clipboard") is None
        fresh.start()
        assert wait_until(lambda: fresh.is_hub), fresh.hub_id
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in (nodes[1], nodes[2]))
        ), [node.hub_id for node in (nodes[1], nodes[2])]

        # Members re-push on attach: the fresh hub learns the newest value with no
        # manual step, and the members keep it.
        assert wait_until(
            lambda: fresh.get_state("clipboard") == payload
        ), fresh.get_state("clipboard")
        assert wait_until(
            lambda: nodes[1].get_state_version("clipboard") == version
        )
        assert fresh.get_state_version("clipboard") == version
        assert nodes[2].get_state("clipboard") == payload
    finally:
        if fresh is not None:
            fresh.stop()
        stop_nodes(nodes)


# -- S7: no echo, no version bump, bounded frames ---------------------------


def test_s7_applying_a_state_does_not_republish_and_versions_do_not_change() -> None:
    nodes, _configs = build_nodes(2)  # n0 hub, n1 member
    payload = b"one state value, applied once"
    applied = Recorder()
    try:
        nodes[0].start()
        nodes[1].start()
        assert wait_until(lambda: nodes[1].hub_id == "n0"), nodes[1].hub_id
        nodes[1].on_state(applied)

        nodes[0].publish_state("clipboard", payload)
        assert wait_until(
            lambda: nodes[1].get_state("clipboard") == payload
        ), nodes[1].get_state("clipboard")
        version = nodes[0].get_state_version("clipboard")
        assert version is not None
        assert nodes[1].get_state_version("clipboard") == version

        assert len(applied.snapshot()) == 1
        assert applied.snapshot()[0][1:] == ("clipboard", "n0", payload)

        hub_stats = nodes[0].state_stats
        member_stats = nodes[1].state_stats
        assert hub_stats.published == 1
        assert hub_stats.sent == 1  # one broadcast to one member
        assert member_stats.received == 1
        assert member_stats.applied == 1
        assert member_stats.sent == 0  # applying does not publish
        time.sleep(QUIET)
        assert nodes[0].state_stats == hub_stats
        assert nodes[1].state_stats == member_stats
    finally:
        stop_nodes(nodes)


# -- S8: group isolation, hub visibility, wrong key -------------------------


def test_s8_groups_are_isolated_and_a_tapped_frame_carries_no_plaintext() -> None:
    from meshcore.messages import STATE_MAGIC, decode_state_frame, encode_state_sync

    relay_port = free_port()
    relay = Node(
        MeshConfig(
            device_id="relaybox",
            name="RelayBox",
            token="relay-own-token-unused",
            role="relay",
            serve_groups=(GroupConfig("A", TOK_A), GroupConfig("B", TOK_B)),
            bind="127.0.0.1",
            port=relay_port,
        )
    )
    relay_address = RelayConfig(address=f"127.0.0.1:{relay_port}")
    members: list[Node] = []
    tap: Link | None = None
    tap_frames: list[bytes] = []
    plaintext = b"CLIPSECRET-MARKER-clipboard-text"
    try:
        relay.start()
        a1 = member_node("a1", group_id="A", token=TOK_A, e2e_key=KEY_A, relay=relay_address)
        a2 = member_node("a2", group_id="A", token=TOK_A, e2e_key=KEY_A, relay=relay_address)
        b1 = member_node("b1", group_id="B", token=TOK_B, e2e_key=KEY_B, relay=relay_address)
        wrong = member_node(
            "wrong", group_id="A", token=TOK_A, e2e_key=KEY_OTHER, relay=relay_address
        )
        members = [a1, a2, b1, wrong]
        for node in members:
            node.start()
        assert wait_until(
            lambda: all(node.hub_id == "relaybox" for node in members)
        ), [node.hub_id for node in members]

        # A group-A client that behaves like the hub operator: it sees the wire
        # frames of group A but has no e2e key.
        tap = Link(
            url=relay_url(relay_port),
            token=TOK_A,
            hello=Hello(device_id="tap", name="TAP", will_hub=False, group_id="A"),
            reconnect=False,
            on_payload=lambda _sender, payload: tap_frames.append(payload),
        )
        tap.connect()
        assert wait_until(lambda: "tap" in {h.device_id for h in a1.roster()})
        tap.send_control(encode_state_sync())
        time.sleep(0.1)  # the hub has processed the capability frame

        a1.publish_state("clipboard", plaintext)
        assert wait_until(
            lambda: a2.get_state("clipboard") == plaintext
        ), a2.get_state("clipboard")
        assert wait_until(lambda: wrong.dropped_undecryptable >= 1)
        assert wrong.get_state("clipboard") is None

        assert wait_until(lambda: len(tap_frames) >= 1), tap_frames
        frame = tap_frames[0]
        assert frame.startswith(STATE_MAGIC)
        key, version, body = decode_state_frame(frame)
        assert key == "clipboard"
        assert version.author == "a1"
        assert plaintext not in frame
        assert plaintext not in body

        time.sleep(QUIET)
        # Group B never sees group A's state.
        assert b1.get_state("clipboard") is None
        assert len(tap_frames) == 1  # bounded: no storm to the tap
    finally:
        if tap is not None:
            tap.close()
        stop_nodes(members)
        relay.stop()


# -- S9: the documented size cap --------------------------------------------


def test_s9_an_oversized_value_is_refused_loudly_and_the_node_stays_healthy() -> None:
    from meshcore.coordination import MAX_STATE_PAYLOAD_BYTES

    assert MAX_STATE_PAYLOAD_BYTES == 8 * 1024 * 1024, "the documented cap is 8 MiB"
    nodes, _configs = build_nodes(2)  # n0 hub, n1 member
    payload = b"small value after the refusal"
    try:
        nodes[0].start()
        nodes[1].start()
        assert wait_until(lambda: nodes[1].hub_id == "n0"), nodes[1].hub_id

        oversized = b"x" * (MAX_STATE_PAYLOAD_BYTES + 1)
        with pytest.raises(NodeError, match="too large"):
            nodes[1].publish_state("clipboard", oversized)
        assert nodes[1].get_state("clipboard") is None
        assert nodes[1].state_stats.published == 0
        assert nodes[0].get_state("clipboard") is None
        assert nodes[1].connected
        assert nodes[1].hub_id == "n0"

        nodes[1].publish_state("clipboard", payload)
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == payload
        ), nodes[0].get_state("clipboard")
        assert nodes[1].get_state("clipboard") == payload
    finally:
        stop_nodes(nodes)


# -- S10: old clients and the legacy broadcast path -------------------------


def test_s10_old_clients_and_the_legacy_broadcast_are_unaffected() -> None:
    from meshcore.messages import STATE_MAGIC

    nodes, configs = build_nodes(2)  # n0 hub, n1 member
    hub_events = Recorder()
    member_events = Recorder()
    old_frames: list[bytes] = []
    old_client: Link | None = None
    try:
        nodes[0].start()
        nodes[1].start()
        assert wait_until(lambda: nodes[1].hub_id == "n0"), nodes[1].hub_id
        nodes[0].on_receive(hub_events)
        nodes[1].on_receive(member_events)

        # An old client: hello only, no state_sync, so no state capability.
        old_client = Link(
            url=relay_url(configs[0].port),
            token=TOKEN,
            hello=Hello(device_id="oldclient", name="OLD", will_hub=False),
            reconnect=False,
            on_payload=lambda _sender, payload: old_frames.append(payload),
        )
        old_client.connect()
        assert wait_until(
            lambda: "oldclient" in {h.device_id for h in nodes[0].roster()}
        )

        # Legacy broadcast member -> hub -> old client.
        nodes[1].publish(b"legacy-event")
        assert wait_until(lambda: hub_events.contains(b"legacy-event"))
        assert wait_until(lambda: old_frames[:1] == [b"legacy-event"]), old_frames

        # Legacy broadcast old client -> hub -> member.
        old_client.send(b"from-old-client")
        assert wait_until(lambda: member_events.contains(b"from-old-client"))

        # State traffic is invisible to a client without state support.
        nodes[1].publish_state("clipboard", b"state-value")
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == b"state-value"
        ), nodes[0].get_state("clipboard")
        assert nodes[1].get_state_version("clipboard") == nodes[0].get_state_version(
            "clipboard"
        )
        time.sleep(QUIET)
        assert all(not frame.startswith(STATE_MAGIC) for frame in old_frames)

        # The legacy path still works after the state channel was used.
        nodes[1].publish(b"legacy-after-state")
        assert wait_until(
            lambda: old_frames[-1:] == [b"legacy-after-state"]
        ), old_frames
        assert hub_events.contains(b"legacy-after-state")
    finally:
        if old_client is not None:
            old_client.close()
        stop_nodes(nodes)


# -- S11: clipsync on meshcore, one copy across a hub failover --------------


def test_s11_clipsync_on_meshcore_delivers_once_across_a_hub_failover() -> None:
    from clipsync.state_transport import MeshStateTransport

    nodes, _configs = build_nodes(3)
    clips = [FakeClipboard() for _ in nodes]
    daemons: list[ClipboardDaemon] = []
    first = text_payload("before the failover")
    during = text_payload("copied during the failover")
    try:
        for node, clip in zip(nodes, clips):
            daemons.append(
                ClipboardDaemon(
                    DaemonConfig(device_id=node.device_id, token=TOKEN),
                    clipboard=clip,
                    transport=MeshStateTransport(node),
                )
            )
        for daemon in daemons:
            daemon.connect()
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in nodes)
        ), [node.hub_id for node in nodes]

        clips[2].set_payload(first)
        assert pump_until(
            daemons, lambda: clips[1].payload == first
        ), clips[1].payload

        # Hub n0 goes away; n2 copies once during the handover, no retry anywhere.
        daemons[0].close()
        clips[2].set_payload(during)
        assert pump_until(
            daemons[1:], lambda: clips[1].payload == during
        ), clips[1].payload

        # Delivered exactly once: the poll loop keeps running and nothing is
        # written a second time (no duplicate frame, no retry).
        pump_for(daemons[1:], QUIET)
        assert clips[1].writes == [first, during]
    finally:
        for daemon in daemons:
            try:
                daemon.close()
            except Exception:  # noqa: BLE001
                pass
        stop_nodes(nodes)


# -- S12: A1 causality under skew -------------------------------------------


def _causality_case(later_is_behind: bool) -> None:
    """A is 120 s ahead of B. One copies, the other accepts, then copies 1 s later.

    ``later_is_behind``: A (ahead) copies first, B (behind) accepts and copies, so
    the later copy must win despite B's clock being behind. The other direction runs
    the same hand-off with the roles swapped, so both arrival orders are covered.
    """
    fixed = time.time()
    hub_clock = Clock(fixed)
    ahead_clock = Clock(fixed, offset=120.0)  # n1 = A
    behind_clock = Clock(fixed)  # n2 = B
    nodes, _configs = build_nodes(
        3, clocks=[hub_clock, ahead_clock, behind_clock]
    )  # n0 hub, n1 A, n2 B
    first_payload = b"from-the-first-copy"
    later_payload = b"from-the-copy-one-second-later"
    try:
        for node in nodes:
            node.start()
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in nodes)
        ), [node.hub_id for node in nodes]

        if later_is_behind:
            first, receiver, receiver_clock = nodes[1], nodes[2], behind_clock
        else:
            first, receiver, receiver_clock = nodes[2], nodes[1], ahead_clock

        first.publish_state("clipboard", first_payload)
        assert wait_until(
            lambda: receiver.get_state("clipboard") == first_payload
        ), receiver.get_state("clipboard")
        first_version = receiver.get_state_version("clipboard")
        assert first_version is not None
        assert first_version.author == first.device_id

        # One second later on the receiver's own (injected) clock. It has accepted
        # the first copy, so its next copy must win by the A1 rule even when its
        # wall clock is behind the author of the value it just accepted.
        receiver_clock.offset += 1.0
        receiver.publish_state("clipboard", later_payload)

        assert wait_until(
            lambda: all(node.get_state("clipboard") == later_payload for node in nodes)
        ), [node.get_state("clipboard") for node in nodes]
        winner = receiver.get_state_version("clipboard")
        assert winner is not None
        assert winner.author == receiver.device_id
        assert (winner.wall_ms, winner.counter) > (
            first_version.wall_ms,
            first_version.counter,
        ), (winner, first_version)
        assert all(
            node.get_state_version("clipboard") == winner for node in nodes
        )
        time.sleep(QUIET)
        assert all(node.get_state("clipboard") == later_payload for node in nodes)
    finally:
        stop_nodes(nodes)


def test_s12_causality_survives_a_clock_behind_the_accepted_author() -> None:
    _causality_case(later_is_behind=True)
    _causality_case(later_is_behind=False)


# -- S13: A2 non-sticky rejection and recovery without a restart ------------


def test_s13_a_misclocked_device_recovers_when_its_clock_is_fixed() -> None:
    fixed = time.time()
    hub_clock = Clock(fixed)
    honest_clock = Clock(fixed)
    skewed_clock = Clock(fixed, offset=600.0)  # ten minutes ahead
    nodes, _configs = build_nodes(
        3, clocks=[hub_clock, honest_clock, skewed_clock]
    )  # n0 hub, n1 honest, n2 skewed
    recovered = b"recovered-after-the-clock-was-fixed"
    try:
        for node in nodes:
            node.start()
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in nodes)
        ), [node.hub_id for node in nodes]

        nodes[1].publish_state("clipboard", b"honest-baseline")
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == b"honest-baseline"
        ), nodes[0].get_state("clipboard")

        nodes[2].publish_state("clipboard", b"ten-minutes-ahead")
        assert wait_until(lambda: nodes[0].state_stats.rejected_skew >= 1)
        assert nodes[0].get_state("clipboard") == b"honest-baseline"
        assert nodes[1].get_state("clipboard") == b"honest-baseline"

        # There is no one-shot ban: a second frame is checked and rejected again.
        nodes[2].publish_state("clipboard", b"still-misclocked")
        assert wait_until(lambda: nodes[0].state_stats.rejected_skew >= 2)
        assert nodes[0].get_state("clipboard") == b"honest-baseline"

        # Fix the injected clock in place: same node, same process, no restart.
        skewed_clock.offset = 0.0
        nodes[2].publish_state("clipboard", recovered)
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == recovered
        ), nodes[0].get_state("clipboard")
        assert wait_until(
            lambda: nodes[1].get_state("clipboard") == recovered
        ), nodes[1].get_state("clipboard")
        assert nodes[2].get_state("clipboard") == recovered

        version = nodes[0].get_state_version("clipboard")
        assert version is not None
        assert version.author == "n2"
        assert all(
            node.get_state_version("clipboard") == version for node in nodes
        )
        # The recovered frame was accepted, not counted as another rejection.
        assert nodes[0].state_stats.rejected_skew == 2
        time.sleep(QUIET)
        assert nodes[0].state_stats.rejected_skew == 2
        assert nodes[0].get_state("clipboard") == recovered
    finally:
        stop_nodes(nodes)


# -- S14: A1 floor safety, a rejected far-future frame ----------------------


def test_s14_a_rejected_far_future_frame_does_not_move_the_floor() -> None:
    fixed = time.time()
    hub_clock = Clock(fixed)
    honest_clock = Clock(fixed)
    far_clock = Clock(fixed, offset=3600.0)  # one hour ahead
    nodes, _configs = build_nodes(
        3, clocks=[hub_clock, honest_clock, far_clock]
    )  # n0 hub, n1 honest, n2 far future
    early = b"honest-early"
    later = b"honest-later"
    try:
        for node in nodes:
            node.start()
        assert wait_until(
            lambda: all(node.hub_id == "n0" for node in nodes)
        ), [node.hub_id for node in nodes]

        nodes[1].publish_state("clipboard", early)
        assert wait_until(lambda: nodes[0].get_state("clipboard") == early)
        early_version = nodes[0].get_state_version("clipboard")
        assert early_version is not None

        nodes[2].publish_state("clipboard", b"one-hour-ahead")
        assert wait_until(lambda: nodes[0].state_stats.rejected_skew >= 1)
        # The rejected frame cannot move the hub's floor or its register.
        assert nodes[0].get_state("clipboard") == early
        assert nodes[0].get_state_version("clipboard") == early_version

        # A later honest copy still wins: a poisoned floor would make it lose.
        honest_clock.offset = 5.0
        nodes[1].publish_state("clipboard", later)
        assert wait_until(
            lambda: nodes[0].get_state("clipboard") == later
        ), nodes[0].get_state("clipboard")
        assert wait_until(
            lambda: nodes[1].get_state("clipboard") == later
        ), nodes[1].get_state("clipboard")
        later_version = nodes[1].get_state_version("clipboard")
        assert later_version is not None
        assert (later_version.wall_ms, later_version.counter) > (
            early_version.wall_ms,
            early_version.counter,
        )
        assert nodes[0].get_state_version("clipboard") == later_version
        assert nodes[0].state_stats.rejected_skew == 1
        time.sleep(QUIET)
        assert nodes[0].state_stats.rejected_skew == 1
        assert nodes[0].get_state("clipboard") == later
    finally:
        stop_nodes(nodes)
