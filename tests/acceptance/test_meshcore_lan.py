"""Acceptance tests for phase 2b: peer addresses, try-in-order and LAN discovery.

Contract: ``docs/design/meshcore-lan.md`` (and ``docs/design/coordination-v2.md``
section 5). These tests are frozen before the implementation job; the implementation
must not edit them.

No real network beyond loopback is used: discovery always runs on an in-memory fake
bus, and every WebSocket link is on 127.0.0.1 (127.0.0.2 for the simulated hotspot
re-lease, still loopback). Every cross-node assertion waits with
``wait_until(..., 8.0)``; where time matters a clock is injected; there are no bare
sleeps used for correctness.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path
import re
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
import tomllib

import pytest

from clipsync.config import load_config
from meshcore.addressing import Address, LastGoodStore, ordered_addresses
from meshcore.config import MeshConfig, PeerConfig
from meshcore.discovery import (
    DEFAULT_DISCOVERY_PORT,
    Discovery,
    encode_beacon,
)
from meshcore.hub import HubServer
from meshcore.node import Node, NodeError

REPO_ROOT = Path(__file__).resolve().parents[2]

TOKEN = "lan-phase2b-token"
OTHER_TOKEN = "lan-phase2b-other-token"
GROUP = "family"

HEARTBEAT = 0.05
MISSED_PONGS = 2
PROBE_TIMEOUT = 0.5
DEBOUNCE = 0.1
RECONNECT_DELAY = 0.02
CONNECT_TIMEOUT = 1.0
ADDRESS_TIMEOUT = 0.2
DISCOVERY_INTERVAL = 0.2
QUIET = 0.3


# -- helpers ----------------------------------------------------------------


def free_port(host: str = "127.0.0.1") -> int:
    """Reserve an ephemeral loopback port for a node to listen on later."""
    sock = socket.socket()
    try:
        sock.bind((host, 0))
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


class ManualClock:
    """Deterministic monotonic clock for expiry and timeout assertions."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


class FakeBus:
    """In-memory replacement for the UDP broadcast/multicast bus."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._sockets: list[FakeSocket] = []

    def socket_for(self, source_ip: str) -> "FakeSocket":
        with self._condition:
            sock = FakeSocket(self, source_ip)
            self._sockets.append(sock)
            return sock

    def set_source(self, sock: "FakeSocket", source_ip: str) -> None:
        with self._condition:
            sock.source_ip = source_ip

    def deliver(self, sender: "FakeSocket", data: bytes) -> None:
        with self._condition:
            for other in self._sockets:
                if other is not sender and not other.closed:
                    other.inbox.append((bytes(data), sender.source_ip))
            self._condition.notify_all()


class FakeSocket:
    """One node's end of the fake bus; matches the DiscoverySocket protocol."""

    def __init__(self, bus: FakeBus, source_ip: str) -> None:
        self.bus = bus
        self.source_ip = source_ip
        self.inbox: list[tuple[bytes, str]] = []
        self.closed = False

    def send(self, data: bytes, dest: tuple[str, int]) -> None:
        if not self.closed:
            self.bus.deliver(self, data)

    def recv(self, timeout: float) -> tuple[bytes, str] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.bus._condition:
            while not self.inbox and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.bus._condition.wait(min(remaining, 0.05))
            if self.inbox:
                return self.inbox.pop(0)
            return None

    def close(self) -> None:
        with self.bus._condition:
            self.closed = True
            self.bus._condition.notify_all()


class _ProxyHandler(socketserver.BaseRequestHandler):
    """Forward one loopback TCP connection to the real hub port."""

    def handle(self) -> None:
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection(self.server.target, timeout=1.0)
            upstream.settimeout(None)
            self.server.track(self.request, upstream)
            peers = (self.request, upstream)
            while not self.server.stopping:
                readable, _, _ = select.select(peers, [], [], 0.05)
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is self.request else self.request).sendall(data)
        except OSError:
            return
        finally:
            for sock in (self.request, upstream):
                if sock is not None:
                    self.server.untrack(sock)


class LoopbackProxy(socketserver.ThreadingTCPServer):
    """A test-only TCP forwarder from one loopback IP:port to another.

    It simulates a hotspot re-lease: the old discovered address keeps working until
    the proxy is stopped, then the node must re-find the hub at the new address.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen: tuple[str, int], target: tuple[str, int]) -> None:
        self.target = target
        self.stopping = False
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        super().__init__(listen, _ProxyHandler)
        self._server_thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="test-lan-proxy",
            daemon=True,
        )
        self._server_thread.start()

    def track(self, *socks: socket.socket) -> None:
        with self._connections_lock:
            self._connections.update(socks)

    def untrack(self, *socks: socket.socket) -> None:
        with self._connections_lock:
            for sock in socks:
                self._connections.discard(sock)
                try:
                    sock.close()
                except OSError:
                    pass

    def stop_proxy(self) -> None:
        self.stopping = True
        with self._connections_lock:
            active = list(self._connections)
        for sock in active:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.shutdown()
        self.server_close()
        if self._server_thread.is_alive():
            self._server_thread.join(timeout=2.0)


class NodeView:
    """A Node plus its callbacks, so tests never touch Node internals."""

    def __init__(self, node: Node) -> None:
        self.node = node
        self.received = Recorder()
        node.on_receive(self.received)

    @property
    def device_id(self) -> str:
        return self.node.device_id

    @property
    def hub_id(self):
        return self.node.hub_id

    @property
    def is_hub(self) -> bool:
        return self.node.is_hub

    @property
    def connected(self) -> bool:
        return self.node.connected

    @property
    def connection_url(self):
        return self.node.connection_url

    @property
    def discovery(self):
        return self.node.discovery

    def start(self) -> None:
        self.node.start()

    def stop(self) -> None:
        self.node.stop()

    def publish(self, payload: bytes) -> None:
        self.node.publish(payload)

    def roster(self):
        return self.node.roster()


def mesh_threads(*names: str) -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name in names
    ]


def make_peer(
    device_id: str,
    *,
    dead_port: int,
    perm: int | None = None,
    addresses: tuple[Address, ...] = (),
    address: str | None = None,
) -> PeerConfig:
    return PeerConfig(
        device_id=device_id,
        name=device_id.upper(),
        address=address if address is not None else f"127.0.0.1:{dead_port}",
        perm=perm,
        addresses=addresses,
    )


def make_node(
    device_id: str,
    *,
    port: int,
    peers: tuple[PeerConfig, ...],
    perm: int | None = None,
    state_dir: Path,
    discovery: bool = True,
    sock: FakeSocket | None = None,
    **overrides,
) -> Node:
    config = MeshConfig(
        device_id=device_id,
        name=device_id.upper(),
        token=TOKEN,
        perm=perm,
        group_id=GROUP,
        bind="127.0.0.1",
        port=port,
        peers=peers,
        discovery=discovery,
        discovery_port=DEFAULT_DISCOVERY_PORT,
        discovery_interval=DISCOVERY_INTERVAL,
        state_dir=str(state_dir),
        heartbeat=HEARTBEAT,
        missed_pongs=MISSED_PONGS,
        probe_timeout=PROBE_TIMEOUT,
        debounce=DEBOUNCE,
        reconnect_delay=RECONNECT_DELAY,
        connect_timeout=CONNECT_TIMEOUT,
        address_timeout=ADDRESS_TIMEOUT,
        **overrides,
    )
    kwargs = {}
    if sock is not None:
        kwargs["discovery_socket_factory"] = lambda: sock
    return Node(config, **kwargs)


def start_all(views: list[NodeView]) -> None:
    for view in views:
        view.start()


def stop_all(views: list[NodeView]) -> None:
    for view in views:
        try:
            view.stop()
        except Exception:  # noqa: BLE001 - cleanup must not mask the assertion
            pass


def publish_retry(view: NodeView, payload: bytes, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            view.publish(payload)
            return
        except NodeError as exc:
            last = exc
            time.sleep(0.01)
    raise AssertionError(f"publish never succeeded: {last!r}")


def receive(view: NodeView, payload: bytes, timeout: float = 8.0) -> bool:
    return wait_until(lambda: view.received.contains(payload), timeout=timeout)


def pump_discovery(discovery: Discovery, timeout: float = 0.05) -> None:
    while discovery.poll_once(timeout=timeout):
        pass


# -- L1: address ordering ---------------------------------------------------


def test_l1_ordered_addresses_fixed_order_last_good_and_dedup() -> None:
    # Config order for equal kinds is preserved; the legacy address is the last
    # static fallback.
    peer = PeerConfig(
        device_id="peer",
        name="Peer",
        address="ws://10.0.0.9:9999/",
        addresses=(
            Address("static", "10.0.0.5", 7005),
            Address("hotspot", "172.20.10.2", 7004),
            Address("tailnet", "100.64.0.2", 7001),
            Address("lan", "192.168.1.10", 7003),
        ),
    )
    discovered = [
        Address("lan", "192.168.1.20", 7006),
        Address("static", "10.0.0.77", 7007),  # not a LAN discovery entry
    ]

    ordered = ordered_addresses(peer, discovered, last_good=None)
    assert [(a.kind, a.host, a.port) for a in ordered] == [
        ("tailnet", "100.64.0.2", 7001),
        ("lan", "192.168.1.10", 7003),
        ("lan", "192.168.1.20", 7006),
        ("hotspot", "172.20.10.2", 7004),
        ("static", "10.0.0.5", 7005),
        ("static", "10.0.0.9", 9999),
    ]

    # last_good moves to the front only while it is still in the set.
    first = ordered_addresses(
        peer, discovered, last_good=Address("lan", "192.168.1.20", 7006)
    )
    assert (first[0].kind, first[0].host, first[0].port) == (
        "lan",
        "192.168.1.20",
        7006,
    )
    assert sorted((a.kind, a.host, a.port) for a in first) == sorted(
        (a.kind, a.host, a.port) for a in ordered
    )
    unchanged = ordered_addresses(
        peer, discovered, last_good=Address("lan", "203.0.113.9", 7008)
    )
    assert unchanged == ordered

    # Duplicates are removed by host:port (first occurrence wins).
    with_dupes = PeerConfig(
        device_id="dupes",
        name="Dupes",
        address="127.0.0.1:8765",
        addresses=(
            Address("lan", "192.168.1.10", 7003),
            Address("lan", "192.168.1.10", 7003),
            Address("static", "127.0.0.1", 8765),
        ),
    )
    deduped = ordered_addresses(with_dupes, ())
    assert len(deduped) == 2
    assert [(a.kind, a.host, a.port) for a in deduped] == [
        ("lan", "192.168.1.10", 7003),
        ("static", "127.0.0.1", 8765),
    ]

    # An old single-address config yields exactly one static entry.
    old = PeerConfig(device_id="old", name="Old", address="127.0.0.1:9001")
    assert [(a.kind, a.host, a.port) for a in ordered_addresses(old, ())] == [
        ("static", "127.0.0.1", 9001)
    ]

    # Permuting equal-kind entries preserves that order (no sorting on host).
    a = PeerConfig(
        device_id="a",
        name="A",
        address="",
        addresses=(
            Address("static", "10.0.0.2", 1),
            Address("static", "10.0.0.1", 2),
        ),
    )
    b = PeerConfig(
        device_id="b",
        name="B",
        address="",
        addresses=(
            Address("static", "10.0.0.1", 2),
            Address("static", "10.0.0.2", 1),
        ),
    )
    assert [(x.host, x.port) for x in ordered_addresses(a, ())] == [
        ("10.0.0.2", 1),
        ("10.0.0.1", 2),
    ]
    assert [(x.host, x.port) for x in ordered_addresses(b, ())] == [
        ("10.0.0.1", 2),
        ("10.0.0.2", 1),
    ]


# -- L2: try in order, remember last_good -----------------------------------


def test_l2_try_in_order_uses_live_address_and_remembers_it(tmp_path: Path) -> None:
    hub = HubServer(token=TOKEN, port=0)
    hub.start()
    dead = free_port()
    live_port = hub.port
    live_url = f"ws://127.0.0.1:{live_port}/"
    state_dir = tmp_path / "state"
    nodes: list[Node] = []
    try:
        peer = make_peer(
            "hub",
            dead_port=dead,
            perm=0,
            addresses=(
                Address("tailnet", "127.0.0.1", dead),
                Address("lan", "127.0.0.1", live_port),
            ),
        )
        first = make_node(
            "me",
            port=free_port(),
            peers=(peer,),
            perm=1,
            state_dir=state_dir,
            discovery=False,
        )
        nodes.append(first)
        started = time.monotonic()
        first.start()
        assert time.monotonic() - started < 2.0

        assert wait_until(lambda: first.hub_id == "hub" and first.connected)
        assert first.connection_url == live_url

        store = LastGoodStore(state_dir / "last_good.json")
        remembered = store.get("hub")
        assert remembered is not None
        assert (remembered.host, remembered.port) == ("127.0.0.1", live_port)

        # A fresh node with the same state tries the remembered live LAN address
        # first, even though the dead tailnet entry comes first in config order.
        calls: list[tuple[str, float]] = []

        def recording_probe(url: str, timeout: float) -> bool:
            calls.append((url, timeout))
            return url == live_url

        second = Node(
            MeshConfig(
                device_id="second",
                name="Second",
                token=TOKEN,
                perm=1,
                group_id=GROUP,
                bind="127.0.0.1",
                port=free_port(),
                peers=(peer,),
                state_dir=str(state_dir),
                heartbeat=HEARTBEAT,
                missed_pongs=MISSED_PONGS,
                probe_timeout=PROBE_TIMEOUT,
                debounce=DEBOUNCE,
                reconnect_delay=RECONNECT_DELAY,
                connect_timeout=CONNECT_TIMEOUT,
                address_timeout=ADDRESS_TIMEOUT,
            ),
            probe_url=recording_probe,
        )
        nodes.append(second)
        second.start()
        assert calls, "the election never probed the peer"
        assert calls[0][0] == live_url
        assert calls[0][1] <= ADDRESS_TIMEOUT
        assert wait_until(lambda: second.hub_id == "hub" and second.connected)

        # A dead address may not consume more than its injected per-address
        # timeout, and the total is bounded by the election probe budget.
        clock = ManualClock()
        budget_calls: list[tuple[str, float, float]] = []

        def timing_probe(url: str, timeout: float) -> bool:
            budget_calls.append((url, timeout, clock()))
            if url == live_url:
                return True
            clock.advance(timeout)
            return False

        third = Node(
            MeshConfig(
                device_id="third",
                name="Third",
                token=TOKEN,
                perm=1,
                group_id=GROUP,
                bind="127.0.0.1",
                port=free_port(),
                peers=(peer,),
                state_dir=str(tmp_path / "timing"),
                heartbeat=HEARTBEAT,
                missed_pongs=MISSED_PONGS,
                probe_timeout=PROBE_TIMEOUT,
                debounce=DEBOUNCE,
                reconnect_delay=RECONNECT_DELAY,
                connect_timeout=CONNECT_TIMEOUT,
                address_timeout=ADDRESS_TIMEOUT,
            ),
            probe_url=timing_probe,
            clock=clock,
        )
        nodes.append(third)
        started_at = clock()
        third.start()
        assert len(budget_calls) >= 2
        assert budget_calls[0][0].endswith(f":{dead}/")
        assert budget_calls[0][1] <= ADDRESS_TIMEOUT
        assert budget_calls[1][0] == live_url
        assert clock() - started_at <= ADDRESS_TIMEOUT
    finally:
        for node in nodes:
            try:
                node.stop()
            except Exception:  # noqa: BLE001
                pass
        hub.stop()


# -- L3: last_good persistence ----------------------------------------------


def test_l3_last_good_is_atomic_owner_only_and_degrades_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meshcore.addressing as addressing

    state_dir = tmp_path / "state"
    path = state_dir / "last_good.json"
    store = LastGoodStore(path)

    assert store.get("peer") is None
    store.set("peer", Address("lan", "192.0.2.10", 8765))
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {"peer": "192.0.2.10:8765"}
    remembered = store.get("peer")
    assert remembered is not None
    assert (remembered.host, remembered.port) == ("192.0.2.10", 8765)

    text = path.read_text(encoding="utf-8")
    assert TOKEN not in text
    assert OTHER_TOKEN not in text
    assert "e2e" not in text.lower()
    assert "key" not in text.lower()

    # Corrupt and missing files both degrade to empty, never an exception.
    path.write_text("{this is not json", encoding="utf-8")
    assert store.get("peer") is None
    path.write_text("[]", encoding="utf-8")
    assert store.get("peer") is None
    path.unlink()
    assert store.get("peer") is None

    # Atomic rename: a failed os.replace leaves the previous file intact.
    store.set("peer", Address("lan", "192.0.2.10", 8765))
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        addressing.os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no"))
    )
    store.set("peer", Address("lan", "192.0.2.11", 9000))  # must not raise
    assert path.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in state_dir.iterdir()) == ["last_good.json"]
    monkeypatch.undo()

    # An unwritable location degrades to empty without an exception.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    blocked_store = LastGoodStore(blocked / "last_good.json")
    blocked_store.set("peer", Address("lan", "192.0.2.12", 9001))
    assert blocked_store.get("peer") is None


# -- L4: beacon authentication ---------------------------------------------


def test_l4_beacon_authentication_ignores_and_counts_bad_datagrams() -> None:
    clock = ManualClock()
    bus = FakeBus()
    socket_ = bus.socket_for("127.0.0.1")
    sender = bus.socket_for("192.0.2.7")
    discovery = Discovery(
        group_id=GROUP,
        device_id="me",
        token=TOKEN,
        port=8765,
        socket=socket_,
        peers={"peer"},
        clock=clock,
        interval=5.0,
        broadcast_addresses=["255.255.255.255"],
    )

    def deliver(datagram: bytes) -> None:
        sender.send(datagram, ("255.255.255.255", 8766))
        pump_discovery(discovery)

    now = clock()
    deliver(encode_beacon(group="other", device_id="peer", port=9000, ts=now, token=TOKEN))
    assert discovery.ignored_beacons == 1
    assert discovery.addresses_for("peer") == []

    deliver(
        encode_beacon(
            group=GROUP, device_id="peer", port=9000, ts=now, token="wrong-token"
        )
    )
    assert discovery.ignored_beacons == 2
    assert discovery.addresses_for("peer") == []

    deliver(
        encode_beacon(
            group=GROUP, device_id="peer", port=9000, ts=now - 31.0, token=TOKEN
        )
    )
    assert discovery.ignored_beacons == 3
    assert discovery.addresses_for("peer") == []

    deliver(
        encode_beacon(
            group=GROUP, device_id="stranger", port=9000, ts=now, token=TOKEN
        )
    )
    assert discovery.ignored_beacons == 4
    assert discovery.addresses_for("stranger") == []

    deliver(
        encode_beacon(group=GROUP, device_id="peer", port=9000, ts=now, token=TOKEN)
    )
    assert discovery.ignored_beacons == 4
    assert discovery.accepted_beacons == 1
    addresses = discovery.addresses_for("peer")
    assert len(addresses) == 1
    assert (addresses[0].kind, addresses[0].host, addresses[0].port) == (
        "lan",
        "192.0.2.7",
        9000,
    )


# -- L5: discovery end to end on the fake bus -------------------------------


def test_l5_discovery_finds_peers_elects_hub_and_syncs(tmp_path: Path) -> None:
    ids = ["n0", "n1", "n2"]
    ports = [free_port() for _ in ids]
    dead_ports = [free_port() for _ in ids]
    bus = FakeBus()
    socks = [bus.socket_for("127.0.0.1") for _ in ids]
    views: list[NodeView] = []
    try:
        for i, device_id in enumerate(ids):
            peers = tuple(
                make_peer(other, dead_port=dead_ports[j], perm=j)
                for j, other in enumerate(ids)
                if j != i
            )
            node = make_node(
                device_id,
                port=ports[i],
                peers=peers,
                perm=i,
                state_dir=tmp_path / device_id,
                sock=socks[i],
            )
            views.append(NodeView(node))

        start_all(views)

        assert wait_until(
            lambda: all(view.hub_id == "n0" for view in views)
        ), [(view.device_id, view.hub_id) for view in views]
        assert views[0].is_hub
        assert wait_until(
            lambda: {h.device_id for h in views[0].roster()} == set(ids)
        ), [h.device_id for h in views[0].roster()]

        payload = b"lan discovery payload"
        publish_retry(views[2], payload)
        assert receive(views[1], payload)
        payload_back = b"lan discovery payload back"
        publish_retry(views[1], payload_back)
        assert receive(views[2], payload_back)
    finally:
        stop_all(views)


# -- L6: hotspot re-lease ---------------------------------------------------


def test_l6_ip_change_is_replaced_and_old_entry_expires(tmp_path: Path) -> None:
    # Discovery bookkeeping: a new source IP replaces the entry immediately; without
    # beacons the entry survives 3 intervals and then expires.
    clock = ManualClock()
    bus = FakeBus()
    discovery = Discovery(
        group_id=GROUP,
        device_id="me",
        token=TOKEN,
        port=8765,
        socket=bus.socket_for("127.0.0.1"),
        peers={"phone"},
        clock=clock,
        interval=5.0,
        broadcast_addresses=["255.255.255.255"],
    )
    phone = bus.socket_for("192.0.2.10")

    def beacon(ts: float) -> bytes:
        return encode_beacon(
            group=GROUP, device_id="phone", port=9000, ts=ts, token=TOKEN
        )

    phone.send(beacon(clock()), ("255.255.255.255", 8766))
    pump_discovery(discovery)
    assert [(a.host, a.port) for a in discovery.addresses_for("phone")] == [
        ("192.0.2.10", 9000)
    ]

    bus.set_source(phone, "192.0.2.11")
    clock.advance(5.0)
    phone.send(beacon(clock()), ("255.255.255.255", 8766))
    pump_discovery(discovery)
    assert [(a.host, a.port) for a in discovery.addresses_for("phone")] == [
        ("192.0.2.11", 9000)
    ]

    clock.advance(14.0)
    assert len(discovery.addresses_for("phone")) == 1
    clock.advance(2.0)
    assert discovery.addresses_for("phone") == []

    # Node-level: the old route (the proxy) dies after the lease changes; the
    # running member re-finds the hub at the new address and sync resumes.
    hub_port = free_port()
    proxy = LoopbackProxy(("127.0.0.2", hub_port), ("127.0.0.1", hub_port))
    bus = FakeBus()
    hub_sock = bus.socket_for("127.0.0.2")
    member_sock = bus.socket_for("127.0.0.1")
    hub_view: NodeView | None = None
    member_view: NodeView | None = None
    try:
        hub_peer = make_peer("hub", dead_port=free_port(), perm=0)
        member_peer = make_peer("member", dead_port=free_port(), perm=1)
        hub_view = NodeView(
            make_node(
                "hub",
                port=hub_port,
                peers=(member_peer,),
                perm=0,
                state_dir=tmp_path / "hub",
                sock=hub_sock,
            )
        )
        member_view = NodeView(
            make_node(
                "member",
                port=free_port(),
                peers=(hub_peer,),
                perm=1,
                state_dir=tmp_path / "member",
                sock=member_sock,
            )
        )
        start_all([hub_view, member_view])
        assert wait_until(lambda: hub_view.hub_id == "hub" and hub_view.is_hub)
        assert wait_until(lambda: member_view.hub_id == "hub" and member_view.connected)
        assert member_view.connection_url == f"ws://127.0.0.2:{hub_port}/"

        # Hotspot re-lease: the same device now answers at 127.0.0.1; the old
        # 127.0.0.2 route disappears when the proxy stops.
        bus.set_source(hub_sock, "127.0.0.1")
        proxy.stop_proxy()

        def uses_new_address() -> bool:
            return (
                member_view.hub_id == "hub"
                and member_view.connected
                and member_view.connection_url == f"ws://127.0.0.1:{hub_port}/"
            )

        assert wait_until(uses_new_address), member_view.connection_url
        resumed = b"sync after the hotspot re-lease"
        publish_retry(member_view, resumed)
        assert receive(hub_view, resumed)
    finally:
        stop_all([v for v in (hub_view, member_view) if v is not None])
        try:
            if not proxy.stopping:
                proxy.stop_proxy()
        except Exception:  # noqa: BLE001
            pass


# -- L7: discovery never adds a device or changes election ------------------


def test_l7_discovery_never_adds_unknown_device_or_changes_hub(
    tmp_path: Path,
) -> None:
    bus = FakeBus()
    node_sock = bus.socket_for("127.0.0.1")
    stranger_sock = bus.socket_for("192.0.2.50")
    view: NodeView | None = None
    try:
        view = NodeView(
            make_node(
                "solo",
                port=free_port(),
                peers=(),
                state_dir=tmp_path / "solo",
                sock=node_sock,
            )
        )
        view.start()
        assert wait_until(lambda: view.is_hub)
        before_roster = [h.device_id for h in view.roster()]
        before_hub = view.hub_id
        assert before_roster == ["solo"]
        assert before_hub == "solo"

        stranger_sock.send(
            encode_beacon(
                group=GROUP,
                device_id="stranger",
                port=9000,
                ts=time.monotonic(),
                token=TOKEN,
            ),
            ("255.255.255.255", 8766),
        )

        assert wait_until(
            lambda: view.discovery is not None
            and view.discovery.ignored_beacons >= 1
        )
        assert view.hub_id == before_hub
        assert view.is_hub
        assert [h.device_id for h in view.roster()] == before_roster
        assert view.discovery is not None
        assert view.discovery.addresses_for("stranger") == []
    finally:
        if view is not None:
            view.stop()


# -- L8: default off and old configs ----------------------------------------


def test_l8_discovery_default_off_opens_no_socket(tmp_path: Path) -> None:
    old_toml = tmp_path / "old.toml"
    old_toml.write_text(
        'device_id = "laptop"\n'
        f'token = "{TOKEN}"\n'
        "[[peers]]\n"
        'device_id = "phone"\n'
        'name = "Phone"\n'
        'address = "127.0.0.1:9"\n',
        encoding="utf-8",
    )
    config = load_config(old_toml)
    assert config.discovery is False
    assert config.discovery_port == DEFAULT_DISCOVERY_PORT
    assert config.state_dir is None
    assert config.peers[0].address == "127.0.0.1:9"
    assert config.peers[0].addresses == ()

    mesh = config.to_mesh_config()
    assert mesh.discovery is False
    assert mesh.discovery_port == DEFAULT_DISCOVERY_PORT
    assert mesh.state_dir is None

    opened: list[FakeSocket] = []
    bus = FakeBus()

    def factory() -> FakeSocket:
        sock = bus.socket_for("127.0.0.1")
        opened.append(sock)
        return sock

    node = Node(mesh, discovery_socket_factory=factory)
    try:
        node.start()
        assert wait_until(lambda: node.is_hub)
        assert node.discovery is None
        assert opened == []
        assert not mesh_threads("meshcore-discovery")
    finally:
        node.stop()

    # Unset in the dataclass is the same as false.
    default_mesh = MeshConfig(
        device_id="solo",
        name="Solo",
        token=TOKEN,
        bind="127.0.0.1",
        port=free_port(),
        state_dir=str(tmp_path / "solo"),
    )
    assert default_mesh.discovery is False
    assert default_mesh.discovery_port == DEFAULT_DISCOVERY_PORT


# -- L9: lifecycle ----------------------------------------------------------


def test_l9_discovery_lifecycle_is_quiet_and_idempotent(tmp_path: Path) -> None:
    bus = FakeBus()
    sock = bus.socket_for("127.0.0.1")
    view = NodeView(
        make_node(
            "life",
            port=free_port(),
            peers=(),
            state_dir=tmp_path / "life",
            sock=sock,
        )
    )
    try:
        view.start()
        assert wait_until(lambda: view.is_hub)
        assert mesh_threads("meshcore-discovery")
        view.stop()
        view.stop()
    finally:
        view.stop()

    assert wait_until(
        lambda: not mesh_threads(
            "meshcore-discovery", "meshcore-election", "meshcore-hub-server"
        )
    ), [t.name for t in mesh_threads("meshcore-discovery", "meshcore-election", "meshcore-hub-server")]

    # A fresh subprocess proves stop() leaves no handlers writing after shutdown:
    # no "Logging error" and no "Task was destroyed" on stderr.
    script = tmp_path / "lifecycle_probe.py"
    script.write_text(
        "import threading, time\n"
        "from meshcore.config import MeshConfig\n"
        "from meshcore.node import Node\n"
        "\n"
        "class Sock:\n"
        "    def __init__(self):\n"
        "        self.closed = False\n"
        "        self.event = threading.Event()\n"
        "    def send(self, data, dest):\n"
        "        pass\n"
        "    def recv(self, timeout):\n"
        "        self.event.wait(timeout)\n"
        "        return None\n"
        "    def close(self):\n"
        "        self.closed = True\n"
        "        self.event.set()\n"
        "\n"
        "node = Node(MeshConfig(device_id='life', name='Life', token='t',\n"
        "    bind='127.0.0.1', port=0, discovery=True, discovery_interval=0.05,\n"
        "    state_dir='.state'), discovery_socket_factory=Sock)\n"
        "node.start()\n"
        "time.sleep(0.2)\n"
        "node.stop()\n"
        "node.stop()\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip() == "ok"
    assert "Logging error" not in result.stderr, result.stderr[-3000:]
    assert "Task was destroyed" not in result.stderr, result.stderr[-3000:]


# -- L10: dependency rule ---------------------------------------------------


def _import_roots(source: str, filename: str = "<test>") -> set[str]:
    tree = ast.parse(source, filename=filename)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            roots.add(node.module.split(".")[0])
    return roots


def test_l10_new_modules_are_stdlib_only_and_meshcore_stays_app_free() -> None:
    package_dir = REPO_ROOT / "meshcore"
    files = sorted(package_dir.rglob("*.py"))
    names = {path.name for path in files}
    assert {"addressing.py", "discovery.py"} <= names

    app_packages = {
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_dir()
        and path.name != "meshcore"
        and (path / "__init__.py").is_file()
    }
    assert "clipsync" in app_packages
    stdlib = set(sys.stdlib_module_names)

    for path in files:
        roots = _import_roots(path.read_text(encoding="utf-8"), str(path))
        assert not (roots & app_packages), f"{path} imports an app package: {roots}"

    for name in ("addressing.py", "discovery.py"):
        path = package_dir / name
        roots = _import_roots(path.read_text(encoding="utf-8"), str(path))
        extra = roots - stdlib - {"meshcore"}
        assert not extra, f"{name} imports non-stdlib roots: {extra}"

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        re.split(r"[<>=!~ ]", requirement, maxsplit=1)[0].strip()
        for requirement in project["project"]["dependencies"]
    }
    assert declared == {"cryptography", "websockets"}


# -- L11: failover over discovery when the hub's IP changes -----------------


@pytest.mark.exploratory
def test_l11_hub_ip_change_failover_over_discovery(tmp_path: Path) -> None:
    ids = ["h", "m1", "m2"]
    hub_port = free_port()
    ports = [hub_port, free_port(), free_port()]
    proxy = LoopbackProxy(("127.0.0.2", hub_port), ("127.0.0.1", hub_port))
    bus = FakeBus()
    hub_sock = bus.socket_for("127.0.0.2")
    member_socks = [bus.socket_for("127.0.0.1") for _ in range(2)]
    views: list[NodeView] = []
    try:
        # The hub listens on 127.0.0.1; discovery first advertises it through the
        # 127.0.0.2 proxy, i.e. the "old" hotspot address.
        for i, device_id in enumerate(ids):
            peers = tuple(
                make_peer(other, dead_port=free_port(), perm=j)
                for j, other in enumerate(ids)
                if j != i
            )
            node = make_node(
                device_id,
                port=ports[i],
                peers=peers,
                perm=i,
                state_dir=tmp_path / device_id,
                sock=hub_sock if i == 0 else member_socks[i - 1],
            )
            views.append(NodeView(node))

        start_all(views)
        assert wait_until(
            lambda: all(view.hub_id == "h" for view in views)
        ), [(view.device_id, view.hub_id) for view in views]
        assert views[0].is_hub
        assert wait_until(
            lambda: all(
                view.connected and view.connection_url == f"ws://127.0.0.2:{hub_port}/"
                for view in views[1:]
            )
        ), [(view.device_id, view.connection_url) for view in views[1:]]

        before = b"before the hub re-lease"
        publish_retry(views[1], before)
        assert receive(views[2], before)

        # The hub's lease changes; the old route dies with the proxy.
        bus.set_source(hub_sock, "127.0.0.1")
        proxy.stop_proxy()

        def all_reconnected() -> bool:
            return all(
                view.hub_id == "h"
                and view.connected
                and view.connection_url == f"ws://127.0.0.1:{hub_port}/"
                for view in views[1:]
            )

        assert wait_until(all_reconnected), [
            (view.device_id, view.hub_id, view.connection_url)
            for view in views[1:]
        ]
        # The hub itself never changed; this was an address failover, not an election.
        assert views[0].is_hub
        assert views[0].hub_id == "h"
        assert all(view.hub_id == "h" for view in views)

        after = b"after the hub re-lease"
        publish_retry(views[1], after)
        assert receive(views[2], after)
        back = b"reply after the hub re-lease"
        publish_retry(views[2], back)
        assert receive(views[1], back)
    finally:
        stop_all(views)
        try:
            if not proxy.stopping:
                proxy.stop_proxy()
        except Exception:  # noqa: BLE001
            pass
