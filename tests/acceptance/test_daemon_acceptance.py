"""Acceptance tests for the clipsync laptop daemon and relay command (spec-first).

Contract: ``docs/design/interfaces.md`` § "daemon run — config, daemon loop, relay
command (2026-10-27)".

These tests are black-box against the public config/daemon/CLI API. They never touch
the real clipboard and never spawn ``wl-paste``/``wl-copy``: the Wayland backend is
only driven through a canned ``CommandRunner``. Relay tests use a loopback server on
an ephemeral port.

Owner: orchestrator (written and committed before the implementation job; do not edit
during it).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from clipsync.clipboard import FakeClipboard, WlClipboardBackend
from clipsync.crypto import FernetCodec
from clipsync.daemon import (
    ClipboardDaemon,
    build_codec,
    main as daemon_main,
)
from clipsync.protocol import ClipMessage, decode_frame, encode_frame, image_payload, text_payload
from clipsync.relay_cli import RelayConfig, main as relay_main, run_relay
from clipsync.relay_server import RELAY_MAX_FRAME_BYTES, RelayServer
from clipsync.relay_transport import RelayTransport
from clipsync.transport import make_pair

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "acceptance-daemon-token"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x11" * 96


# -- helpers ----------------------------------------------------------------


def relay_url(port: int) -> str:
    return f"ws://127.0.0.1:{port}/"


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def pump_until(a, b, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        a.poll_once()
        b.poll_once()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def write_toml(dirpath: Path, **values) -> Path:
    """Write a minimal TOML config; ``None`` values are omitted."""
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


def base_values(**overrides) -> dict:
    values = {
        "device_id": "laptop",
        "relay_url": "ws://127.0.0.1:9/",
        "token": TOKEN,
    }
    values.update(overrides)
    return values


def daemon_config(**overrides):
    from clipsync.config import DaemonConfig

    values = {
        "device_id": "laptop",
        "relay_url": "ws://127.0.0.1:9/",
        "token": TOKEN,
    }
    values.update(overrides)
    return DaemonConfig(**values)


class CannedRunner:
    """Fake subprocess runner: returns canned (rc, stdout, stderr) and records calls."""

    def __init__(self, responses: list[tuple[int, bytes, bytes]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], bytes | None]] = []

    def run(self, argv: list[str], stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        self.calls.append((list(argv), stdin))
        if not self.responses:
            raise AssertionError("unexpected extra command")
        return self.responses.pop(0)


# -- config loading ---------------------------------------------------------


def test_load_config_reads_all_values(tmp_path: Path) -> None:
    from clipsync.config import load_config

    key = FernetCodec.generate().key.decode("ascii")
    path = write_toml(
        tmp_path,
        **base_values(
            e2e_key=key,
            poll_interval=0.25,
            reconnect_delay=0.1,
            connect_timeout=2.5,
        ),
    )
    cfg = load_config(path)
    assert cfg == daemon_config(
        e2e_key=key,
        poll_interval=0.25,
        reconnect_delay=0.1,
        connect_timeout=2.5,
    )


def test_load_config_defaults_and_optional_e2e_key(tmp_path: Path) -> None:
    from clipsync.config import load_config

    cfg = load_config(write_toml(tmp_path, **base_values()))
    assert cfg.e2e_key is None
    assert cfg.poll_interval == 0.5
    assert cfg.reconnect_delay == 0.2
    assert cfg.connect_timeout == 5.0


def test_load_config_resolves_secret_files_relative_to_config(tmp_path: Path) -> None:
    from clipsync.config import load_config

    key = FernetCodec.generate().key.decode("ascii")
    (tmp_path / "token.txt").write_text(f"  {TOKEN}\n", encoding="utf-8")
    (tmp_path / "e2e.key").write_text(f"{key}\n", encoding="utf-8")
    path = write_toml(
        tmp_path,
        device_id="laptop",
        relay_url="wss://relay.example.test/",
        token_file="token.txt",
        e2e_key_file="e2e.key",
    )
    cfg = load_config(path)
    assert cfg.token == TOKEN
    assert cfg.e2e_key == key
    assert cfg.relay_url == "wss://relay.example.test/"


def test_load_config_raises_config_error_for_bad_input(tmp_path: Path) -> None:
    from clipsync.config import ConfigError, load_config

    (tmp_path / "token.txt").write_text("file-token\n", encoding="utf-8")
    (tmp_path / "e2e.key").write_text(FernetCodec.generate().key.decode("ascii"), encoding="utf-8")

    cases: list[dict] = [
        {"device_id": None},  # missing
        {"device_id": ""},
        {"device_id": "x" * 129},
        {"relay_url": None},
        {"relay_url": "http://127.0.0.1:9/"},
        {"relay_url": "not-a-url"},
        {"relay_url": "ws:///"},
        {"token": None},
        {"token": ""},
        {"token": "inline", "token_file": "token.txt"},
        {"e2e_key": "not-a-fernet-key"},
        {"e2e_key": "inline", "e2e_key_file": "e2e.key"},
        {"poll_interval": 0},
        {"poll_interval": -0.5},
        {"poll_interval": "fast"},
        {"reconnect_delay": 0},
        {"connect_timeout": 0},
        {"unknown_key": 1},
    ]
    for overrides in cases:
        path = write_toml(tmp_path, **base_values(**overrides))
        with pytest.raises(ConfigError):
            load_config(path)

    # Missing config file, invalid TOML and missing/empty secret files are errors too.
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = [not toml\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
    with pytest.raises(ConfigError):
        load_config(write_toml(tmp_path, **base_values(token=None, token_file="missing.txt")))
    (tmp_path / "empty.txt").write_text("  \n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(write_toml(tmp_path, **base_values(token=None, token_file="empty.txt")))
    with pytest.raises(ConfigError):
        load_config(
            write_toml(
                tmp_path,
                **base_values(e2e_key=None, e2e_key_file="missing.key"),
            )
        )


def test_config_errors_do_not_echo_secrets(tmp_path: Path) -> None:
    from clipsync.config import ConfigError, load_config

    secret = "super-secret-token-value"
    path = write_toml(tmp_path, **base_values(token=secret, unexpected="x"))
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert secret not in str(excinfo.value)


# -- codec builder ----------------------------------------------------------


def test_build_codec_from_config() -> None:
    assert build_codec(daemon_config()) is None
    codec = build_codec(daemon_config(e2e_key=FernetCodec.generate().key.decode("ascii")))
    assert codec is not None
    assert codec.decrypt(codec.encrypt(b"hello")) == b"hello"


# -- daemon loop ------------------------------------------------------------


def test_daemon_syncs_text_and_images_over_relay() -> None:
    with RelayServer(token=TOKEN, port=0) as server:
        url = relay_url(server.port)
        cfg_a = daemon_config(relay_url=url)
        cfg_b = replace(cfg_a, device_id="phone")
        clip_a, clip_b = FakeClipboard(), FakeClipboard()
        ta = RelayTransport(url, TOKEN, reconnect_delay=0.05)
        tb = RelayTransport(url, TOKEN, reconnect_delay=0.05)
        da = ClipboardDaemon(cfg_a, clipboard=clip_a, transport=ta)
        db = ClipboardDaemon(cfg_b, clipboard=clip_b, transport=tb)
        try:
            da.connect()
            db.connect()

            clip_a.set_payload(text_payload("hello from the daemon"))
            assert pump_until(
                da,
                db,
                lambda: clip_b.payload == text_payload("hello from the daemon"),
            )
            assert clip_b.writes == [text_payload("hello from the daemon")]
            assert clip_a.writes == []

            clip_b.set_payload(image_payload(PNG, "image/png"))
            assert pump_until(
                da,
                db,
                lambda: clip_a.payload == image_payload(PNG, "image/png"),
            )
            assert clip_a.payload is not None
            assert clip_a.payload.data == PNG
        finally:
            da.close()
            db.close()


def test_daemon_with_matching_e2e_codec_syncs() -> None:
    codec = FernetCodec.generate()
    with RelayServer(token=TOKEN, port=0) as server:
        url = relay_url(server.port)
        cfg = daemon_config(relay_url=url)
        clip_a, clip_b = FakeClipboard(), FakeClipboard()
        ta = RelayTransport(url, TOKEN, reconnect_delay=0.05)
        tb = RelayTransport(url, TOKEN, reconnect_delay=0.05)
        da = ClipboardDaemon(cfg, clipboard=clip_a, transport=ta, codec=codec)
        db = ClipboardDaemon(
            replace(cfg, device_id="phone"), clipboard=clip_b, transport=tb, codec=codec
        )
        try:
            da.connect()
            db.connect()
            clip_a.set_payload(text_payload("encrypted daemon text"))
            assert pump_until(
                da, db, lambda: clip_b.payload == text_payload("encrypted daemon text")
            )
        finally:
            da.close()
            db.close()


def test_daemon_drives_wl_backend_with_canned_runner_only() -> None:
    # Advertise text only: choose_mime intentionally prefers images when both are
    # offered, so a mixed listing would take the image path (covered elsewhere).
    runner = CannedRunner(
        [
            (0, b"text/plain\n", b""),
            (0, "copied on the laptop".encode("utf-8"), b""),
        ]
    )
    backend = WlClipboardBackend(runner=runner)
    local, peer = make_pair()
    daemon = ClipboardDaemon(daemon_config(), clipboard=backend, transport=local)
    stats = daemon.poll_once()
    assert stats.sent == 1
    assert runner.calls[0][0] == ["wl-paste", "--list-types"]
    assert runner.calls[1][0] == ["wl-paste", "--no-newline", "--type", "text/plain"]
    assert all(argv[0] != "wl-copy" for argv, _stdin in runner.calls)
    frames = peer.recv()
    assert len(frames) == 1
    msg = decode_frame(frames[0])
    assert msg.origin == "laptop"
    assert msg.payload == text_payload("copied on the laptop")


def test_daemon_run_is_bounded_and_only_sleeps_between_polls() -> None:
    clip = FakeClipboard()
    local, _peer = make_pair()
    daemon = ClipboardDaemon(daemon_config(poll_interval=0.25), clipboard=clip, transport=local)
    sleeps: list[float] = []
    assert daemon.run(max_iterations=3, sleep=sleeps.append) == 3
    assert sleeps == [0.25, 0.25]

    assert daemon.run(max_iterations=0, sleep=sleeps.append) == 0
    assert sleeps == [0.25, 0.25]

    with pytest.raises(ValueError):
        daemon.run(max_iterations=-1)
    with pytest.raises(ValueError):
        daemon.run(max_iterations=True)


def test_daemon_run_stops_on_event() -> None:
    clip = FakeClipboard()
    local, _peer = make_pair()
    daemon = ClipboardDaemon(daemon_config(poll_interval=0.01), clipboard=clip, transport=local)
    stop = threading.Event()

    def sleeper(_delay: float) -> None:
        stop.set()

    assert daemon.run(stop_event=stop, sleep=sleeper) == 1
    assert stop.is_set()


# -- daemon CLI -------------------------------------------------------------


def test_daemon_main_once_prints_stats_and_sends(tmp_path: Path, capsys) -> None:
    with RelayServer(token=TOKEN, port=0) as server:
        peer = RelayTransport(relay_url(server.port), TOKEN)
        peer.connect()
        try:
            path = write_toml(
                tmp_path,
                **base_values(
                    relay_url=relay_url(server.port),
                    poll_interval=0.01,
                ),
            )
            clip = FakeClipboard(payload=text_payload("from the cli"))
            rc = daemon_main(
                ["--config", str(path), "--once"],
                clipboard_factory=lambda _cfg: clip,
            )
            assert rc == 0
            out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
            assert out_lines, "expected a JSON stats line on stdout"
            stats = json.loads(out_lines[-1])
            assert {"sent", "received", "applied", "queued"} <= set(stats)
            deadline = time.monotonic() + 5.0
            frames: list[bytes] = []
            while not frames and time.monotonic() < deadline:
                frames = peer.recv()
                time.sleep(0.01)
            assert len(frames) == 1
            assert decode_frame(frames[0]).payload == text_payload("from the cli")
        finally:
            peer.close()


def test_daemon_main_missing_config_returns_2(tmp_path: Path, capsys) -> None:
    rc = daemon_main(
        ["--config", str(tmp_path / "absent.toml"), "--once"],
        clipboard_factory=lambda _cfg: FakeClipboard(),
    )
    assert rc == 2
    assert capsys.readouterr().err.strip()


def test_daemon_main_connect_failure_returns_1(tmp_path: Path, capsys) -> None:
    path = write_toml(
        tmp_path,
        **base_values(
            relay_url=relay_url(free_port()),
            connect_timeout=0.2,
            reconnect_delay=0.05,
        ),
    )
    rc = daemon_main(
        ["--config", str(path), "--once"],
        clipboard_factory=lambda _cfg: FakeClipboard(),
    )
    assert rc == 1
    assert capsys.readouterr().err.strip()


# -- relay CLI --------------------------------------------------------------


def test_relay_config_validation() -> None:
    assert RelayConfig(token=TOKEN).port == 8765
    assert RelayConfig(token=TOKEN).max_frame_bytes == RELAY_MAX_FRAME_BYTES
    with pytest.raises(ValueError):
        RelayConfig(token="")
    with pytest.raises(ValueError):
        RelayConfig(token=TOKEN, port=-1)
    with pytest.raises(ValueError):
        RelayConfig(token=TOKEN, port=65536)
    with pytest.raises(ValueError):
        RelayConfig(token=TOKEN, max_frame_bytes=0)


def test_run_relay_serves_until_stop_event() -> None:
    stop = threading.Event()
    started = threading.Event()
    holder: dict[str, int] = {}
    results: list[int] = []
    errors: list[BaseException] = []

    def on_started(server: RelayServer) -> None:
        holder["port"] = server.port
        started.set()

    def target() -> None:
        try:
            results.append(run_relay(RelayConfig(token=TOKEN, port=0), stop_event=stop, on_started=on_started))
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion below
            errors.append(exc)

    thread = threading.Thread(target=target, name="relay-cli-test")
    thread.start()
    assert started.wait(5.0), "relay command did not report that it started"
    a = RelayTransport(relay_url(holder["port"]), TOKEN, reconnect=False)
    b = RelayTransport(relay_url(holder["port"]), TOKEN, reconnect=False)
    try:
        a.connect()
        b.connect()
        assert wait_until(lambda: a.connected and b.connected)
        frame = encode_frame(ClipMessage(origin="a", seq=0, payload=text_payload("via cli")))
        a.send(frame)
        deadline = time.monotonic() + 5.0
        got: list[bytes] = []
        while not got and time.monotonic() < deadline:
            got = b.recv()
            time.sleep(0.01)
        assert got == [frame]
    finally:
        a.close()
        b.close()
        stop.set()
        thread.join(5.0)
    assert not thread.is_alive()
    assert errors == []
    assert results == [0]


def test_relay_main_honours_injected_stop_event(capsys) -> None:
    stop = threading.Event()
    results: list[int] = []
    thread = threading.Thread(
        target=lambda: results.append(
            relay_main(
                ["--token", TOKEN, "--port", "0"],
                stop_event=stop,
                on_started=lambda _server: stop.set(),
            )
        )
    )
    thread.start()
    thread.join(5.0)
    assert not thread.is_alive()
    assert results == [0]
    assert "clipsync-relay listening on ws://127.0.0.1:" in capsys.readouterr().out


def test_relay_main_errors_return_2(tmp_path: Path, capsys) -> None:
    (tmp_path / "token.txt").write_text(f"{TOKEN}\n", encoding="utf-8")
    assert relay_main([], stop_event=threading.Event()) == 2
    assert relay_main(["--token", TOKEN, "--token-file", str(tmp_path / "token.txt")], stop_event=threading.Event()) == 2
    assert relay_main(["--token", TOKEN, "--port", "70000"], stop_event=threading.Event()) == 2
    assert relay_main(["--token", TOKEN, "--max-frame-bytes", "0"], stop_event=threading.Event()) == 2
    assert relay_main(["--token-file", str(tmp_path / "missing.txt")], stop_event=threading.Event()) == 2
    assert capsys.readouterr().err.strip()


# -- packaging / entry points / docs ---------------------------------------


def test_pyproject_declares_console_scripts() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["clipsync"] == "clipsync.daemon:main"
    assert scripts["clipsync-relay"] == "clipsync.relay_cli:main"


def test_module_entry_points_accept_help() -> None:
    for module in ("clipsync.daemon", "clipsync.relay_cli"):
        proc = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert "usage:" in proc.stdout


def test_readme_quickstart_documents_both_commands() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Quickstart" in readme
    quickstart = readme.split("## Quickstart", 1)[1]
    assert "clipsync-relay" in quickstart
    assert "clipsync --config" in quickstart
    assert "--once" in quickstart
