"""Acceptance tests for phase 2a integration: clipsync on meshcore (A8-A10).

Contract: ``docs/design/coordination-v2.md`` sections 0, 3, 4, 6, 10, 13 and
``docs/design/meshcore-phase2a.md``.

A8-A9 are thin integration tests: real clipsync daemons (`FakeClipboard`, never the
real clipboard) on in-process meshcore Nodes over loopback. A10 is the dependency /
thin-wrapper gate.

Owner: orchestrator (written and committed before the implementation job; do not
edit during it).
"""

from __future__ import annotations

import ast
import socket
import sys
import time
from pathlib import Path

import pytest

from clipsync.clipboard import FakeClipboard
from clipsync.config import DaemonConfig, load_config
from clipsync.daemon import ClipboardDaemon, main as daemon_main
from clipsync.mesh_transport import NodeTransport
from clipsync.protocol import text_payload
from clipsync.relay_server import RELAY_MAX_FRAME_BYTES, RelayServer
from clipsync.relay_transport import RelayTransport
from meshcore.config import DEFAULT_MAX_FRAME_BYTES, MeshConfig, PeerConfig
from meshcore.hub import HubServer
from meshcore.link import Link
from meshcore.node import Node

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "phase2a-clipsync-token"

HEARTBEAT = 0.02
MISSED_PONGS = 2
PROBE_TIMEOUT = 0.05
RECONNECT_DELAY = 0.02
CONNECT_TIMEOUT = 1.0


# -- helpers ----------------------------------------------------------------


def free_port() -> int:
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


def build_mesh_nodes(
    count: int, *, debounce: float = 0.2, bind: str = "127.0.0.1"
) -> tuple[list[Node], list[int]]:
    ids = [f"n{i}" for i in range(count)]
    ports = [free_port() for _ in range(count)]
    nodes: list[Node] = []
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
        config = MeshConfig(
            device_id=device_id,
            name=device_id.upper(),
            token=TOKEN,
            perm=i,
            bind=bind,
            port=ports[i],
            peers=peers,
            heartbeat=HEARTBEAT,
            missed_pongs=MISSED_PONGS,
            probe_timeout=PROBE_TIMEOUT,
            debounce=debounce,
            reconnect_delay=RECONNECT_DELAY,
            connect_timeout=CONNECT_TIMEOUT,
        )
        nodes.append(Node(config))
    return nodes, ports


def stop_nodes(nodes: list[Node]) -> None:
    for node in nodes:
        try:
            node.stop()
        except Exception:  # noqa: BLE001 - cleanup must not mask assertions
            pass


def pump_until(daemons: list[ClipboardDaemon], predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for daemon in daemons:
            daemon.poll_once()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def write_old_config(path: Path, **overrides) -> Path:
    values = {
        "device_id": "laptop",
        "relay_url": "ws://127.0.0.1:9/",
        "token": TOKEN,
    }
    values.update(overrides)
    lines = []
    for key, value in values.items():
        if value is None:
            continue
        lines.append(f"{key} = {value!r}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# -- A8: three clipsync daemons on meshcore --------------------------------


def test_a8_three_clipsync_daemons_sync_and_survive_failover() -> None:
    nodes, _ports = build_mesh_nodes(3)
    clips = [FakeClipboard() for _ in nodes]
    daemons: list[ClipboardDaemon] = []
    try:
        for node, clip in zip(nodes, clips):
            daemons.append(
                ClipboardDaemon(
                    DaemonConfig(device_id=node.device_id, token=TOKEN),
                    clipboard=clip,
                    transport=NodeTransport(node),
                )
            )
        for daemon in daemons:
            daemon.connect()
        assert wait_until(lambda: all(n.hub_id == "n0" for n in nodes)), [
            n.hub_id for n in nodes
        ]

        clips[2].set_payload(text_payload("copied on n2"))
        assert pump_until(
            daemons, lambda: clips[1].payload == text_payload("copied on n2")
        ), clips[1].payload
        assert clips[0].payload == text_payload("copied on n2")
        assert clips[1].writes == [text_payload("copied on n2")]

        # Hub failover: n0 goes away, n1 takes over, n2 reconnects, sync resumes.
        daemons[0].close()
        assert wait_until(
            lambda: nodes[1].hub_id == "n1" and nodes[2].hub_id == "n1"
        ), [n.hub_id for n in nodes]

        clips[2].set_payload(text_payload("copied after failover"))
        assert pump_until(
            daemons[1:],
            lambda: clips[1].payload == text_payload("copied after failover"),
        ), clips[1].payload
        assert clips[1].writes[-1] == text_payload("copied after failover")
    finally:
        for daemon in daemons:
            try:
                daemon.close()
            except Exception:  # noqa: BLE001
                pass
        stop_nodes(nodes)


# -- A9: old configs still take the old single-relay path -------------------


def test_a9_old_config_and_single_relay_still_work(tmp_path: Path) -> None:
    path = write_old_config(tmp_path / "old.toml")
    config = load_config(path)
    assert config.device_id == "laptop"
    assert config.relay_url == "ws://127.0.0.1:9/"
    assert config.peers == ()
    assert config.name is None
    assert config.perm is None
    assert config.temp_rank is None
    assert config.will_hub is True
    assert config.bind == "127.0.0.1"
    assert config.port == 8765
    # New config fields map into the meshcore config; old ones keep working.
    mesh = config.to_mesh_config()
    assert mesh.device_id == "laptop"
    assert mesh.name == "laptop"
    assert mesh.token == TOKEN
    assert mesh.peers == ()

    with RelayServer(token=TOKEN, port=0) as server:
        url = f"ws://127.0.0.1:{server.port}/"
        peer = RelayTransport(url, TOKEN, reconnect=False)
        peer.connect()
        try:
            path = write_old_config(tmp_path / "old-relay.toml", relay_url=url)
            clip = FakeClipboard(payload=text_payload("old config still syncs"))
            rc = daemon_main(
                ["--config", str(path), "--once"],
                clipboard_factory=lambda _cfg: clip,
            )
            assert rc == 0
            assert wait_until(lambda: len(peer.recv()) >= 1)
        finally:
            peer.close()


def test_a9_mesh_config_fields_load_and_map(tmp_path: Path) -> None:
    path = tmp_path / "mesh.toml"
    path.write_text(
        f'device_id = "n0"\n'
        f'name = "Laptop"\n'
        f'token = "{TOKEN}"\n'
        f'perm = 0\n'
        f'temp_rank = 3\n'
        f'will_hub = false\n'
        f'bind = "127.0.0.1"\n'
        f'port = 8765\n'
        f'[[peers]]\n'
        f'device_id = "n1"\n'
        f'name = "Phone"\n'
        f'address = "127.0.0.1:8766"\n'
        f'perm = 1\n'
        f'temp_rank = 4\n'
        f'will_hub = false\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.peers == (
        PeerConfig(
            device_id="n1",
            name="Phone",
            address="127.0.0.1:8766",
            perm=1,
            temp_rank=4,
            will_hub=False,
        ),
    )
    mesh = config.to_mesh_config()
    assert mesh.perm == 0
    assert mesh.temp_rank == 3
    assert mesh.will_hub is False
    assert mesh.bind == "127.0.0.1"
    assert mesh.port == 8765
    assert mesh.peers == config.peers


# -- A10: dependency rule and thin compatibility wrappers -------------------


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


def test_a10_meshcore_never_imports_apps_and_core_is_stdlib_only() -> None:
    package_dir = REPO_ROOT / "meshcore"
    files = sorted(package_dir.rglob("*.py"))
    assert files

    app_packages = {
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_dir()
        and path.name != "meshcore"
        and (path / "__init__.py").is_file()
    }
    assert "clipsync" in app_packages

    stdlib = set(sys.stdlib_module_names)
    pure_core = {
        "__init__.py",
        "config.py",
        "coordination.py",
        "election.py",
        "messages.py",
        "node.py",
    }
    transport_adapters = {"hub.py", "link.py"}
    for path in files:
        roots = _import_roots(path.read_text(encoding="utf-8"), str(path))
        assert not (roots & app_packages), f"{path} imports an app package: {roots}"
        if path.name in transport_adapters:
            extra = roots - stdlib - {"meshcore", "websockets"}
            assert not extra, f"meshcore transport {path} imports {extra}"
        else:
            # Every other meshcore module (the named pure core and any helper) must
            # stay standard-library-only; node.py and election.py are in this group.
            extra = roots - stdlib - {"meshcore"}
            assert not extra, f"pure meshcore module {path} imports {extra}"
    assert {"config.py", "election.py", "messages.py", "node.py"} <= {p.name for p in files}


def test_a10_relay_server_and_transport_are_thin_wrappers() -> None:
    assert RELAY_MAX_FRAME_BYTES == DEFAULT_MAX_FRAME_BYTES
    assert RELAY_MAX_FRAME_BYTES == 4 * ((8 * 1024 * 1024 + 2) // 3) + 8192

    server_src = (REPO_ROOT / "clipsync" / "relay_server.py").read_text(
        encoding="utf-8"
    )
    transport_src = (REPO_ROOT / "clipsync" / "relay_transport.py").read_text(
        encoding="utf-8"
    )

    assert issubclass(RelayServer, HubServer)
    assert issubclass(RelayTransport, Link)

    # No forwarding loop / per-client queue management may be left in the wrapper.
    for needle in ("other_queue", "for other_ws", "def _handler", "def _client_sender"):
        assert needle not in server_src, f"forwarding logic left in relay_server: {needle}"
    # No WebSocket client loop may be left in the transport wrapper.
    for needle in ("websockets", "async def _connection_loop", "_run_loop"):
        assert needle not in transport_src, f"connection loop left in relay_transport: {needle}"

    assert len(server_src.splitlines()) < 140
    assert len(transport_src.splitlines()) < 160
