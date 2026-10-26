"""Unit tests for clipsync.daemon module."""

from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from clipsync.clipboard import FakeClipboard, WlClipboardBackend
from clipsync.config import DaemonConfig
from clipsync.crypto import FernetCodec
from clipsync.daemon import (
    ClipboardDaemon,
    build_clipboard,
    build_codec,
    build_transport,
    main,
)
from clipsync.protocol import text_payload
from clipsync.relay_transport import RelayTransport
from clipsync.transport import InMemoryTransport, TransportError, make_pair


def test_daemon_lifecycle() -> None:
    clip = FakeClipboard(payload=text_payload("initial"))
    t1, t2 = make_pair()
    cfg = DaemonConfig(device_id="lap", relay_url="ws://127.0.0.1:8765/", token="tok")
    daemon = ClipboardDaemon(cfg, clipboard=clip, transport=t1)

    daemon.connect()
    assert t1.connected

    stats = daemon.poll_once()
    assert stats.sent == 1
    assert stats.received == 0

    daemon.close()
    assert not t1.connected


def test_daemon_run_loop_bounds_and_sleeps() -> None:
    clip = FakeClipboard()
    t1, _ = make_pair()
    cfg = DaemonConfig(
        device_id="lap",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        poll_interval=0.1,
    )
    daemon = ClipboardDaemon(cfg, clipboard=clip, transport=t1)

    sleeps: list[float] = []
    polls = daemon.run(max_iterations=4, sleep=sleeps.append)
    assert polls == 4
    assert sleeps == [0.1, 0.1, 0.1]

    sleeps.clear()
    assert daemon.run(max_iterations=0, sleep=sleeps.append) == 0
    assert sleeps == []

    with pytest.raises(ValueError):
        daemon.run(max_iterations=-5)
    with pytest.raises(ValueError):
        daemon.run(max_iterations=False)


def test_daemon_run_honours_stop_event_immediately() -> None:
    clip = FakeClipboard()
    t1, _ = make_pair()
    cfg = DaemonConfig(device_id="lap", relay_url="ws://127.0.0.1:8765/", token="tok")
    daemon = ClipboardDaemon(cfg, clipboard=clip, transport=t1)

    stop = threading.Event()
    stop.set()
    polls = daemon.run(stop_event=stop)
    assert polls == 0


def test_builders() -> None:
    key = FernetCodec.generate().key.decode("ascii")
    cfg = DaemonConfig(
        device_id="lap",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        e2e_key=key,
        reconnect_delay=0.15,
        connect_timeout=3.0,
    )
    codec = build_codec(cfg)
    assert codec is not None
    assert isinstance(codec, FernetCodec)

    clip = build_clipboard(cfg)
    assert isinstance(clip, WlClipboardBackend)

    transport = build_transport(cfg)
    assert isinstance(transport, RelayTransport)
    assert transport.url == "ws://127.0.0.1:8765/"


class FailingConnectTransport(InMemoryTransport):
    def connect(self) -> None:
        raise TransportError("failed to connect to relay")


def test_main_cli_connect_failure(tmp_path: Path, capsys) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'device_id = "lap"\n'
        'relay_url = "ws://127.0.0.1:8765/"\n'
        'token = "tok"\n',
        encoding="utf-8",
    )
    rc = main(
        ["--config", str(cfg_file), "--once"],
        clipboard_factory=lambda _c: FakeClipboard(),
        transport_factory=lambda _c: FailingConnectTransport(),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Transport connection error" in err


def test_main_cli_once_success(tmp_path: Path, capsys) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'device_id = "lap"\n'
        'relay_url = "ws://127.0.0.1:8765/"\n'
        'token = "tok"\n',
        encoding="utf-8",
    )
    t1, _ = make_pair()
    rc = main(
        ["--config", str(cfg_file), "--once"],
        clipboard_factory=lambda _c: FakeClipboard(payload=text_payload("cli test")),
        transport_factory=lambda _c: t1,
    )
    assert rc == 0
    captured = capsys.readouterr()
    stats = json.loads(captured.out.strip())
    assert stats["sent"] == 1
    assert stats["applied"] == 0
