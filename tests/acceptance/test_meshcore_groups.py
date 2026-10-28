"""Acceptance tests for meshcore groups (G1-G7).

Contract: ``docs/design/meshcore-groups.md``. These tests are frozen before the
implementation job; the implementation must not edit them.

The tests use only ``meshcore`` (plus ``clipsync.config`` for G6 back-compat), opaque
byte payloads, in-process Nodes/HubServers on loopback and tiny timings. They never
touch the real clipboard, a non-loopback address, or any service. Every cross-node
assertion waits with ``wait_until(..., timeout=8.0)``; negative delivery assertions
first wait for the positive delivery and then observe a short quiet window.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib

import pytest
from cryptography.fernet import Fernet

from clipsync.config import load_config
from meshcore.config import GroupConfig, MeshConfig, PeerConfig, RelayConfig
from meshcore.crypto import GroupCipher
from meshcore.hub import HubServer
from meshcore.link import Link, LinkError, relay_url
from meshcore.messages import Hello
from meshcore.node import Node, NodeError

REPO_ROOT = Path(__file__).resolve().parents[2]

TOK_A = "groups-token-a"
TOK_B = "groups-token-b"
TOK_D = "groups-token-default"
TOK = "groups-single-token"

KEY_A = Fernet.generate_key().decode("ascii")
KEY_B = Fernet.generate_key().decode("ascii")
KEY_OTHER = Fernet.generate_key().decode("ascii")

# Tiny timings so a relay failover/handback stays inside a test.
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

    @property
    def dropped_undecryptable(self) -> int:
        return self.node.dropped_undecryptable

    def start(self) -> None:
        self.node.start()

    def stop(self) -> None:
        self.node.stop()

    def publish(self, payload: bytes) -> None:
        self.node.publish(payload)

    def roster(self) -> list[Hello]:
        return self.node.roster()


def member_config(
    device_id: str,
    *,
    group_id: str,
    token: str,
    port: int,
    peers: list[dict],
    perm: int | None = None,
    e2e_key: str | None = None,
    relay: RelayConfig | None = None,
    debounce: float = DEBOUNCE,
) -> MeshConfig:
    """Build one member MeshConfig; peers are always inside the same group."""
    peer_configs = tuple(
        PeerConfig(
            device_id=p["device_id"],
            name=p["device_id"].upper(),
            address=f"127.0.0.1:{p['port']}",
            perm=p.get("perm"),
        )
        for p in peers
    )
    return MeshConfig(
        device_id=device_id,
        name=device_id.upper(),
        token=token,
        perm=perm,
        group_id=group_id,
        e2e_key=e2e_key,
        relay=relay,
        bind="127.0.0.1",
        port=port,
        peers=peer_configs,
        heartbeat=HEARTBEAT,
        missed_pongs=MISSED_PONGS,
        probe_timeout=PROBE_TIMEOUT,
        debounce=debounce,
        reconnect_delay=RECONNECT_DELAY,
        connect_timeout=CONNECT_TIMEOUT,
    )


def build_group(
    prefix: str,
    group_id: str,
    token: str,
    *,
    count: int = 2,
    relay: RelayConfig | None = None,
    e2e_key: str | None = None,
) -> list[NodeView]:
    """Build ``count`` peers of one group with static peers and deterministic perms."""
    ids = [f"{prefix}{i}" for i in range(count)]
    ports = [free_port() for _ in range(count)]
    views: list[NodeView] = []
    for i, device_id in enumerate(ids):
        peers = [
            {"device_id": other, "port": ports[j], "perm": j}
            for j, other in enumerate(ids)
            if j != i
        ]
        config = member_config(
            device_id,
            group_id=group_id,
            token=token,
            port=ports[i],
            peers=peers,
            perm=i,
            e2e_key=e2e_key,
            relay=relay,
        )
        views.append(NodeView(Node(config)))
    return views


def start_all(*groups: list[NodeView]) -> None:
    for group in groups:
        for view in group:
            view.start()


def stop_all(*groups: list[NodeView]) -> None:
    for group in groups:
        for view in group:
            try:
                view.stop()
            except Exception:  # noqa: BLE001 - cleanup must not mask the assertion
                pass


def publish_retry(view: NodeView, payload: bytes, timeout: float = 8.0) -> None:
    """Publish, retrying while the node is still converging (no route yet)."""
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


def raw_hub(
    groups: list[tuple[str, str]],
    *,
    on_data: Recorder | None = None,
) -> HubServer:
    """Start a relay-style HubServer serving the given (id, token) groups."""
    hub = HubServer(
        groups=tuple(GroupConfig(group_id, token) for group_id, token in groups),
        host="127.0.0.1",
        port=0,
        on_data=on_data,
    )
    hub.start()
    return hub


def relay_for(port: int) -> RelayConfig:
    return RelayConfig(address=f"127.0.0.1:{port}")


def drain(link: Link, into: list[bytes]) -> bool:
    """Drain a Link into ``into``; return True once anything is buffered."""
    try:
        into.extend(link.recv())
    except LinkError:
        return False
    return bool(into)


# -- G1: isolation ----------------------------------------------------------


def test_g1_groups_are_isolated_on_one_hub() -> None:
    hub = raw_hub([("A", TOK_A), ("B", TOK_B)])
    group_a: list[NodeView] = []
    group_b: list[NodeView] = []
    try:
        relay = relay_for(hub.port)
        group_a = build_group("A", "A", TOK_A, count=2, relay=relay)
        group_b = build_group("B", "B", TOK_B, count=2, relay=relay)
        start_all(group_a, group_b)

        assert wait_until(
            lambda: all(view.hub_id == "relay" for view in group_a + group_b)
        ), [(view.device_id, view.hub_id) for view in group_a + group_b]

        # Each roster lists only its own group (and the relay is never a member).
        assert wait_until(
            lambda: {h.device_id for h in group_a[0].roster()} == {"A0", "A1"}
        ), [h.device_id for h in group_a[0].roster()]
        assert wait_until(
            lambda: {h.device_id for h in group_b[0].roster()} == {"B0", "B1"}
        ), [h.device_id for h in group_b[0].roster()]
        assert all(h.group_id == "A" for h in group_a[0].roster())
        assert all(h.group_id == "B" for h in group_b[0].roster())
        assert "relay" not in {h.device_id for h in group_a[0].roster()}
        assert "relay" not in {h.device_id for h in group_b[0].roster()}

        payload_a = b"family A payload \x00 only for A"
        publish_retry(group_a[0], payload_a)
        assert receive(group_a[1], payload_a)
        time.sleep(QUIET)
        assert not group_b[0].received.contains(payload_a)
        assert not group_b[1].received.contains(payload_a)

        payload_b = b"family B payload \x00 only for B"
        publish_retry(group_b[0], payload_b)
        assert receive(group_b[1], payload_b)
        time.sleep(QUIET)
        assert not group_a[0].received.contains(payload_b)
        assert not group_a[1].received.contains(payload_b)
    finally:
        stop_all(group_a, group_b)
        hub.stop()


# -- G2: token/group mapping and old clients --------------------------------


def test_g2_tokens_map_to_groups_and_old_clients_land_in_default() -> None:
    hub = raw_hub([("default", TOK_D), ("A", TOK_A)])
    default_group: list[NodeView] = []
    group_a: list[NodeView] = []
    old_default: Link | None = None
    old_a: Link | None = None
    intruder: Link | None = None
    bad_token: Link | None = None
    try:
        relay = relay_for(hub.port)
        default_group = build_group(
            "D", "default", TOK_D, count=1, relay=relay
        )
        group_a = build_group("A", "A", TOK_A, count=1, relay=relay)
        start_all(default_group, group_a)
        assert wait_until(
            lambda: all(
                view.hub_id == "relay" for view in default_group + group_a
            )
        )

        url = relay_url(hub.port)

        # A token for group A claiming group B must be rejected.
        intruder = Link(
            url,
            TOK_A,
            hello=Hello("intruder", "Intruder", group_id="B"),
            reconnect=False,
            connect_timeout=CONNECT_TIMEOUT,
        )
        try:
            try:
                intruder.connect()
            except LinkError:
                pass
            assert wait_until(lambda: not intruder.connected)
        finally:
            intruder.close()
        assert wait_until(
            lambda: all(
                "intruder" not in {h.device_id for h in hub.roster(group)}
                for group in ("A", "B", "default")
            )
        )

        # A token that belongs to no group is rejected at the handshake.
        bad_token = Link(
            url,
            "definitely-not-a-token",
            hello=None,
            reconnect=False,
            connect_timeout=CONNECT_TIMEOUT,
        )
        try:
            with pytest.raises(LinkError):
                bad_token.connect()
        finally:
            bad_token.close()

        # An old client (no hello) with the default token lands in "default".
        old_default = Link(
            url,
            TOK_D,
            hello=None,
            reconnect=False,
            connect_timeout=CONNECT_TIMEOUT,
        )
        old_default.connect()
        from_default = b"old default client says hello"
        old_default.send(from_default)
        assert receive(default_group[0], from_default)
        time.sleep(QUIET)
        assert not group_a[0].received.contains(from_default)

        # And an old client with A's token lands in A, not default.
        old_a = Link(
            url,
            TOK_A,
            hello=None,
            reconnect=False,
            connect_timeout=CONNECT_TIMEOUT,
        )
        old_a.connect()
        from_a = b"old A client says hello"
        old_a.send(from_a)
        assert receive(group_a[0], from_a)
        time.sleep(QUIET)
        assert not default_group[0].received.contains(from_a)

        # Old clients are grouped by token and never enter a roster.
        assert wait_until(
            lambda: {h.device_id for h in hub.roster("default")} == {"D0"}
        ), [h.device_id for h in hub.roster("default")]
        assert wait_until(
            lambda: {h.device_id for h in hub.roster("A")} == {"A0"}
        ), [h.device_id for h in hub.roster("A")]
    finally:
        for link in (old_default, old_a, intruder, bad_token):
            if link is not None:
                link.close()
        stop_all(default_group, group_a)
        hub.stop()


# -- G3: per-group election -------------------------------------------------


def test_g3_per_group_election_isolated_failover() -> None:
    group_a: list[NodeView] = []
    group_b: list[NodeView] = []
    try:
        group_a = build_group("A", "A", TOK_A, count=3)
        group_b = build_group("B", "B", TOK_B, count=3)
        start_all(group_a, group_b)

        assert wait_until(
            lambda: all(view.hub_id == "A0" for view in group_a)
        ), [(view.device_id, view.hub_id) for view in group_a]
        assert wait_until(
            lambda: all(view.hub_id == "B0" for view in group_b)
        ), [(view.device_id, view.hub_id) for view in group_b]

        payload_a = b"before A failover"
        publish_retry(group_a[2], payload_a)
        assert receive(group_a[1], payload_a)
        payload_b = b"before B failover"
        publish_retry(group_b[2], payload_b)
        assert receive(group_b[1], payload_b)

        # Kill only group A's hub.
        group_a[0].stop()
        assert wait_until(
            lambda: group_a[1].hub_id == "A1" and group_a[2].hub_id == "A1"
        ), [(view.device_id, view.hub_id) for view in group_a]
        assert group_a[1].is_hub

        # Group B's hub and traffic are unaffected.
        assert group_b[0].hub_id == "B0"
        assert group_b[0].is_hub
        assert group_b[1].hub_id == "B0"
        assert group_b[2].hub_id == "B0"

        after_a = b"A sync resumes on A1"
        publish_retry(group_a[2], after_a)
        assert receive(group_a[1], after_a)

        after_b = b"B never noticed A's hub dying"
        publish_retry(group_b[2], after_b)
        assert receive(group_b[1], after_b)
        assert wait_until(lambda: group_b[0].hub_id == "B0")
    finally:
        stop_all(group_a, group_b)


# -- G4: relay-only role, preferred hub, fallback, handback -----------------


def test_g4_relay_only_preferred_hub_fallback_and_handback() -> None:
    relay_port = free_port()
    relay_config = MeshConfig(
        device_id="relaybox",
        name="RelayBox",
        token="relay-own-token-is-unused",
        role="relay",
        serve_groups=(GroupConfig("A", TOK_A), GroupConfig("B", TOK_B)),
        bind="127.0.0.1",
        port=relay_port,
    )
    assert relay_config.e2e_key is None
    assert relay_config.role == "relay"

    relay = NodeView(Node(relay_config))
    relay.start()
    relay_address = relay_for(relay_port)
    group_a: list[NodeView] = []
    group_b: list[NodeView] = []
    restarted: NodeView | None = None
    try:
        group_a = build_group(
            "A", "A", TOK_A, count=2, relay=relay_address, e2e_key=KEY_A
        )
        group_b = build_group(
            "B", "B", TOK_B, count=2, relay=relay_address, e2e_key=KEY_B
        )
        start_all(group_a, group_b)

        # The relay is the preferred hub for both groups.
        assert wait_until(
            lambda: all(view.hub_id == "relay" for view in group_a + group_b)
        ), [(view.device_id, view.hub_id) for view in group_a + group_b]
        assert relay.dropped_undecryptable == 0

        assert wait_until(
            lambda: {h.device_id for h in group_a[0].roster()} == {"A0", "A1"}
        )
        assert wait_until(
            lambda: {h.device_id for h in group_b[0].roster()} == {"B0", "B1"}
        )
        roster_ids = {
            h.device_id for view in group_a + group_b for h in view.roster()
        }
        assert "relay" not in roster_ids
        assert "relaybox" not in roster_ids

        first_a = b"through the relay for A"
        publish_retry(group_a[0], first_a)
        assert receive(group_a[1], first_a)
        first_b = b"through the relay for B"
        publish_retry(group_b[0], first_b)
        assert receive(group_b[1], first_b)

        # Relay dies: each group elects among its own members and keeps syncing.
        relay.stop()
        assert wait_until(
            lambda: group_a[0].hub_id == "A0" and group_a[1].hub_id == "A0"
        ), [(view.device_id, view.hub_id) for view in group_a]
        assert wait_until(
            lambda: group_b[0].hub_id == "B0" and group_b[1].hub_id == "B0"
        ), [(view.device_id, view.hub_id) for view in group_b]

        fallback_a = b"fallback sync in A"
        publish_retry(group_a[1], fallback_a)
        assert receive(group_a[0], fallback_a)
        fallback_b = b"fallback sync in B"
        publish_retry(group_b[1], fallback_b)
        assert receive(group_b[0], fallback_b)

        # Relay returns: the debounce hands the hub role back to it.
        restarted = NodeView(Node(relay_config))
        restarted.start()
        relay = restarted
        assert wait_until(
            lambda: all(view.hub_id == "relay" for view in group_a + group_b),
            timeout=8.0,
        ), [(view.device_id, view.hub_id) for view in group_a + group_b]
        assert all(h.group_id in {"A", "B"} for view in group_a + group_b for h in view.roster())
        assert "relay" not in {
            h.device_id for view in group_a + group_b for h in view.roster()
        }

        handback_a = b"handback sync in A"
        publish_retry(group_a[0], handback_a)
        assert receive(group_a[1], handback_a)
        handback_b = b"handback sync in B"
        publish_retry(group_b[0], handback_b)
        assert receive(group_b[1], handback_b)
    finally:
        stop_all(group_a, group_b)
        if restarted is not None:
            restarted.stop()
        relay.stop()


# -- G5: encryption ---------------------------------------------------------


def test_g5_missing_key_refuses_non_loopback_and_allow_insecure_warns() -> None:
    refused = Node(
        MeshConfig(
            device_id="refused",
            name="Refused",
            token=TOK,
            bind="0.0.0.0",
            port=0,
        )
    )
    try:
        with pytest.raises(NodeError) as excinfo:
            refused.start()
    finally:
        refused.stop()
    message = str(excinfo.value)
    assert "e2e_key" in message
    assert "allow_insecure" in message

    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    node_logger = logging.getLogger("meshcore.node")
    handler = _ListHandler()
    previous_level = node_logger.level
    node_logger.addHandler(handler)
    node_logger.setLevel(logging.WARNING)

    warned = Node(
        MeshConfig(
            device_id="warned",
            name="Warned",
            token=TOK,
            group_id="g",
            bind="127.0.0.1",
            port=0,
            peers=(PeerConfig("phone", "Phone", "192.0.2.5:9"),),
            allow_insecure=True,
        ),
        can_reach=lambda _a, _b: False,
    )
    try:
        warned.start()
        assert wait_until(lambda: warned.is_hub)
    finally:
        warned.stop()
        node_logger.removeHandler(handler)
        node_logger.setLevel(previous_level)

    warnings = [
        record.getMessage()
        for record in records
        if record.levelno >= logging.WARNING
    ]
    assert any("allow_insecure" in message for message in warnings), warnings


def test_g5_wire_frames_are_ciphertext_only() -> None:
    tap = Recorder()
    hub = raw_hub([("g", TOK)], on_data=tap)
    group: list[NodeView] = []
    old_client: Link | None = None
    try:
        relay = relay_for(hub.port)
        group = build_group(
            "M", "g", TOK, count=2, relay=relay, e2e_key=KEY_A
        )
        start_all(group)
        assert wait_until(
            lambda: all(view.hub_id == "relay" for view in group)
        )
        old_client = Link(
            relay_url(hub.port),
            TOK,
            hello=None,
            reconnect=False,
            connect_timeout=CONNECT_TIMEOUT,
        )
        old_client.connect()

        plaintext = b"top secret clipboard text \x00 never on the wire"
        publish_retry(group[0], plaintext)
        assert receive(group[1], plaintext)

        wire: list[bytes] = []
        assert wait_until(lambda: drain(old_client, wire)), "no frame on the wire"
        assert wire
        assert all(plaintext not in frame for frame in wire)
        assert all(frame != plaintext for frame in wire)
        assert GroupCipher(KEY_A).decrypt(wire[-1]) == plaintext

        # The hub's own view of the forwarded frame is ciphertext too.
        assert wait_until(lambda: bool(tap.payloads()))
        tap_payloads = tap.payloads()
        assert all(plaintext not in payload for payload in tap_payloads)
        assert GroupCipher(KEY_A).decrypt(tap_payloads[-1]) == plaintext
    finally:
        if old_client is not None:
            old_client.close()
        stop_all(group)
        hub.stop()


def test_g5_wrong_key_is_dropped_and_counted() -> None:
    hub = raw_hub([("g", TOK)])
    group_a: list[NodeView] = []
    group_b: list[NodeView] = []
    try:
        relay = relay_for(hub.port)
        group_a = build_group(
            "A", "g", TOK, count=1, relay=relay, e2e_key=KEY_A
        )
        group_b = build_group(
            "B", "g", TOK, count=1, relay=relay, e2e_key=KEY_B
        )
        start_all(group_a, group_b)
        assert wait_until(
            lambda: all(view.hub_id == "relay" for view in group_a + group_b)
        )

        payload = b"only the right key may read this"
        publish_retry(group_a[0], payload)
        assert wait_until(lambda: group_b[0].dropped_undecryptable >= 1)
        time.sleep(QUIET)
        assert not group_b[0].received.contains(payload)
        assert not group_a[0].received.contains(payload)
    finally:
        stop_all(group_a, group_b)
        hub.stop()


def test_g5_forced_cross_group_delivery_cannot_decrypt() -> None:
    tap = Recorder()
    hub = raw_hub([("A", TOK_A), ("B", TOK_B)], on_data=tap)
    group_a: list[NodeView] = []
    group_b: list[NodeView] = []
    inject_b: Link | None = None
    inject_a: Link | None = None
    try:
        relay = relay_for(hub.port)
        group_a = build_group(
            "A", "A", TOK_A, count=2, relay=relay, e2e_key=KEY_A
        )
        group_b = build_group(
            "B", "B", TOK_B, count=2, relay=relay, e2e_key=KEY_B
        )
        start_all(group_a, group_b)
        assert wait_until(
            lambda: all(view.hub_id == "relay" for view in group_a + group_b)
        )

        # Capture group A's ciphertext from the hub's forwarded bytes.
        before_a = len(tap.payloads())
        secret_a = b"family A plaintext must never reach B"
        publish_retry(group_a[0], secret_a)
        assert receive(group_a[1], secret_a)
        assert wait_until(lambda: len(tap.payloads()) >= before_a + 1)
        captured_a = tap.payloads()[before_a]
        assert GroupCipher(KEY_A).decrypt(captured_a) == secret_a

        # Force that ciphertext into group B under B's token.
        inject_b = Link(
            relay_url(hub.port),
            TOK_B,
            hello=None,
            reconnect=False,
            connect_timeout=CONNECT_TIMEOUT,
        )
        inject_b.connect()
        inject_b.send(captured_a)
        assert wait_until(
            lambda: all(view.dropped_undecryptable >= 1 for view in group_b)
        )
        time.sleep(QUIET)
        assert all(not view.received.contains(secret_a) for view in group_b)

        # The reverse direction: B's ciphertext forced into A.
        before_b = len(tap.payloads())
        secret_b = b"family B plaintext must never reach A"
        publish_retry(group_b[0], secret_b)
        assert receive(group_b[1], secret_b)
        assert wait_until(lambda: len(tap.payloads()) >= before_b + 1)
        captured_b = tap.payloads()[before_b]
        assert GroupCipher(KEY_B).decrypt(captured_b) == secret_b

        inject_a = Link(
            relay_url(hub.port),
            TOK_A,
            hello=None,
            reconnect=False,
            connect_timeout=CONNECT_TIMEOUT,
        )
        inject_a.connect()
        inject_a.send(captured_b)
        assert wait_until(
            lambda: all(view.dropped_undecryptable >= 1 for view in group_a)
        )
        time.sleep(QUIET)
        assert all(not view.received.contains(secret_b) for view in group_a)
    finally:
        for link in (inject_a, inject_b):
            if link is not None:
                link.close()
        stop_all(group_a, group_b)
        hub.stop()


# -- G6: back-compat --------------------------------------------------------


def test_g6_old_single_group_config_keeps_phase2a_behaviour(tmp_path: Path) -> None:
    path = tmp_path / "old.toml"
    path.write_text(
        'device_id = "laptop"\n'
        'relay_url = "ws://127.0.0.1:9/"\n'
        f'token = "{TOK}"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.group_id == "default"
    assert config.role == "member"
    assert config.serve_groups == ()
    assert config.relay is None
    assert config.allow_insecure is False
    assert config.e2e_key is None
    assert config.uses_meshcore() is False

    mesh = config.to_mesh_config()
    assert mesh.group_id == "default"
    assert mesh.role == "member"
    assert mesh.serve_groups == ()
    assert mesh.relay is None
    assert mesh.allow_insecure is False
    assert mesh.e2e_key is None
    assert MeshConfig(device_id="solo", name="Solo", token=TOK).group_id == "default"


def test_g6_default_group_cluster_still_syncs() -> None:
    nodes = build_group("N", "default", TOK, count=2)
    try:
        start_all(nodes)
        assert wait_until(lambda: nodes[0].hub_id == "N0")
        assert wait_until(lambda: nodes[1].hub_id == "N0")
        assert wait_until(
            lambda: {h.device_id for h in nodes[0].roster()} == {"N0", "N1"}
        )
        assert all(h.group_id == "default" for h in nodes[0].roster())

        payload = b"phase 2a default-group sync"
        publish_retry(nodes[1], payload)
        assert receive(nodes[0], payload)
    finally:
        stop_all(nodes)


# -- G7: hygiene and dependency allowlist -----------------------------------


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


def test_g7_dependency_allowlist() -> None:
    package_dir = REPO_ROOT / "meshcore"
    files = sorted(package_dir.rglob("*.py"))
    names = {path.name for path in files}
    assert {"config.py", "coordination.py", "election.py", "messages.py", "node.py"} <= names
    assert "crypto.py" in names, "meshcore/crypto.py must exist (design point 5)"

    app_packages = {
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_dir()
        and path.name != "meshcore"
        and (path / "__init__.py").is_file()
    }
    assert "clipsync" in app_packages
    stdlib = set(sys.stdlib_module_names)

    # Explicit allowlist: nothing else may import third-party code.
    allow_third_party: dict[str, set[str]] = {
        "hub.py": {"websockets"},
        "link.py": {"websockets"},
        "crypto.py": {"cryptography"},
    }
    pure_core = {
        "__init__.py",
        "config.py",
        "coordination.py",
        "election.py",
        "messages.py",
        "node.py",
    }
    for path in files:
        roots = _import_roots(path.read_text(encoding="utf-8"), str(path))
        assert not (roots & app_packages), f"{path} imports an app package: {roots}"
        if path.name in pure_core:
            allowed = set()
        else:
            assert path.name in allow_third_party, (
                f"{path.name} is not in the dependency allowlist; add it explicitly"
            )
            allowed = allow_third_party[path.name]
        extra = roots - stdlib - {"meshcore"} - allowed
        assert not extra, f"{path.name} imports non-allowlisted roots: {extra}"

    # Both third-party packages are declared, not just installed.
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        re.split(r"[<>=!~ ]", requirement, maxsplit=1)[0].strip()
        for requirement in project["project"]["dependencies"]
    }
    assert "cryptography" in declared
    assert "websockets" in declared


def test_g7_clean_shell_has_no_logging_noise() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests",
        "--ignore=tests/acceptance/test_meshcore_groups.py",
    ]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"subprocess suite failed\nstdout tail:\n{result.stdout[-3000:]}\n"
        f"stderr tail:\n{result.stderr[-3000:]}"
    )
    assert "Logging error" not in result.stderr, result.stderr[-3000:]
    assert "I/O operation on closed file" not in result.stderr, result.stderr[-3000:]
