"""Unit tests for clipsync.relay_cli module."""

from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from clipsync.relay_cli import RelayConfig, build_server, main, run_relay
from clipsync.relay_server import RELAY_MAX_FRAME_BYTES, RelayServer


def test_relay_config_defaults_and_validation() -> None:
    cfg = RelayConfig(token="valid-token")
    assert cfg.token == "valid-token"
    assert cfg.port == 8765
    assert cfg.max_frame_bytes == RELAY_MAX_FRAME_BYTES

    with pytest.raises(ValueError, match="token"):
        RelayConfig(token="")

    with pytest.raises(ValueError, match="port"):
        RelayConfig(token="t", port=-1)

    with pytest.raises(ValueError, match="port"):
        RelayConfig(token="t", port=65536)

    with pytest.raises(ValueError, match="port"):
        RelayConfig(token="t", port=True)

    with pytest.raises(ValueError, match="max_frame_bytes"):
        RelayConfig(token="t", max_frame_bytes=0)


def test_build_server() -> None:
    cfg = RelayConfig(token="tok", port=9000, max_frame_bytes=1000)
    server = build_server(cfg)
    assert isinstance(server, RelayServer)
    assert server.port == 9000
    assert server.host == "127.0.0.1"


def test_run_relay_bind_failure(capsys) -> None:
    cfg = RelayConfig(token="tok", port=9000)
    with patch.object(RelayServer, "start", side_effect=OSError("Address already in use")):
        rc = run_relay(cfg)
        assert rc == 1
    err = capsys.readouterr().err
    assert "Relay server bind error" in err


def test_run_relay_clean_lifecycle(capsys) -> None:
    cfg = RelayConfig(token="tok", port=0)
    stop = threading.Event()
    on_started_called = threading.Event()

    def on_started(srv: RelayServer) -> None:
        on_started_called.set()
        stop.set()

    rc = run_relay(cfg, stop_event=stop, on_started=on_started)
    assert rc == 0
    assert on_started_called.is_set()
    out = capsys.readouterr().out
    assert "clipsync-relay listening on ws://127.0.0.1:" in out


def test_relay_main_cli_token_file(tmp_path: Path, capsys) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("   file-token-val   \n", encoding="utf-8")

    stop = threading.Event()
    rc = main(
        ["--token-file", str(token_file), "--port", "0"],
        stop_event=stop,
        on_started=lambda _srv: stop.set(),
    )
    assert rc == 0
    assert "clipsync-relay listening on ws://127.0.0.1:" in capsys.readouterr().out


def test_relay_main_cli_errors(tmp_path: Path, capsys) -> None:
    # No token
    assert main([], stop_event=threading.Event()) == 2
    # Both token sources
    tf = tmp_path / "token.txt"
    tf.write_text("token", encoding="utf-8")
    assert main(["--token", "tok", "--token-file", str(tf)], stop_event=threading.Event()) == 2
    # Empty token file
    empty_tf = tmp_path / "empty.txt"
    empty_tf.write_text("  \n", encoding="utf-8")
    assert main(["--token-file", str(empty_tf)], stop_event=threading.Event()) == 2
    # Missing token file
    assert main(["--token-file", str(tmp_path / "nonexistent.txt")], stop_event=threading.Event()) == 2
    # Invalid port
    assert main(["--token", "tok", "--port", "99999"], stop_event=threading.Event()) == 2
    assert capsys.readouterr().err.strip()
