"""Unit tests for clipsync.config module."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipsync.config import ConfigError, DaemonConfig, load_config
from clipsync.crypto import FernetCodec


def test_daemon_config_defaults() -> None:
    cfg = DaemonConfig(
        device_id="laptop",
        relay_url="ws://127.0.0.1:8765/",
        token="tok123",
    )
    assert cfg.device_id == "laptop"
    assert cfg.relay_url == "ws://127.0.0.1:8765/"
    assert cfg.token == "tok123"
    assert cfg.e2e_key is None
    assert cfg.poll_interval == 0.5
    assert cfg.reconnect_delay == 0.2
    assert cfg.connect_timeout == 5.0


def test_load_config_minimal(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'device_id = "box"\n'
        'relay_url = "ws://127.0.0.1:8765/"\n'
        'token = "secret-token"\n',
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.device_id == "box"
    assert loaded.relay_url == "ws://127.0.0.1:8765/"
    assert loaded.token == "secret-token"
    assert loaded.e2e_key is None
    assert loaded.poll_interval == 0.5


def test_load_config_with_relative_and_absolute_secret_files(tmp_path: Path) -> None:
    sub_dir = tmp_path / "subdir"
    sub_dir.mkdir()
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()

    token_file = sub_dir / "my_token.txt"
    token_file.write_text("  token-from-sub  \n", encoding="utf-8")

    key = FernetCodec.generate().key.decode("ascii")
    abs_key_file = secret_dir / "key.fernet"
    abs_key_file.write_text(f"{key}\n", encoding="utf-8")

    cfg_file = sub_dir / "config.toml"
    cfg_file.write_text(
        'device_id = "laptop"\n'
        'relay_url = "wss://relay.example.com/ws"\n'
        'token_file = "my_token.txt"\n'
        f'e2e_key_file = "{abs_key_file}"\n'
        "poll_interval = 1.5\n",
        encoding="utf-8",
    )

    loaded = load_config(cfg_file)
    assert loaded.token == "token-from-sub"
    assert loaded.e2e_key == key
    assert loaded.poll_interval == 1.5


def test_load_config_rejects_both_inline_and_file_secrets(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'device_id = "laptop"\n'
        'relay_url = "ws://127.0.0.1:8765/"\n'
        'token = "inline"\n'
        'token_file = "token.txt"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="both 'token' and 'token_file'"):
        load_config(cfg_file)


def test_load_config_rejects_missing_secret_sources(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'device_id = "laptop"\n'
        'relay_url = "ws://127.0.0.1:8765/"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Exactly one of 'token' or 'token_file'"):
        load_config(cfg_file)


def test_load_config_rejects_invalid_urls(tmp_path: Path) -> None:
    for bad_url in [
        "http://127.0.0.1:8765/",
        "ftp://example.com/",
        "not-a-url",
        "ws:///",
        "wss://",
    ]:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            f'device_id = "laptop"\nrelay_url = "{bad_url}"\ntoken = "tok"\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(cfg_file)


def test_load_config_rejects_invalid_device_ids(tmp_path: Path) -> None:
    for bad_id in ["", "   ", "a" * 129]:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            f'device_id = "{bad_id}"\nrelay_url = "ws://127.0.0.1:8765/"\ntoken = "tok"\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(cfg_file)


def test_load_config_rejects_invalid_numbers(tmp_path: Path) -> None:
    for field in ["poll_interval", "reconnect_delay", "connect_timeout"]:
        for bad_val in ["0", "-1", "true", "false", '"fast"']:
            cfg_file = tmp_path / "config.toml"
            cfg_file.write_text(
                'device_id = "laptop"\n'
                'relay_url = "ws://127.0.0.1:8765/"\n'
                'token = "tok"\n'
                f"{field} = {bad_val}\n",
                encoding="utf-8",
            )
            with pytest.raises(ConfigError):
                load_config(cfg_file)


def test_load_config_never_leaks_secret(tmp_path: Path) -> None:
    secret = "classified-token-secret-xyz"
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'device_id = "laptop"\nrelay_url = "ws://127.0.0.1:8765/"\ntoken = "{secret}"\ninvalid_key = 123\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    assert secret not in str(exc_info.value)
