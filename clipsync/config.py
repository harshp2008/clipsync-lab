"""Configuration loader and schema for the clipsync daemon.

Reads a TOML configuration file, resolves optional secret files relative to the
configuration file directory, validates all parameters strictly, and returns a
frozen :class:`DaemonConfig`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import urllib.parse

import tomllib

from meshcore.config import MeshConfig, PeerConfig
from .crypto import CryptoError, FernetCodec

__all__ = ["ConfigError", "DaemonConfig", "load_config"]

_ALLOWED_KEYS = frozenset(
    {
        "device_id",
        "name",
        "relay_url",
        "token",
        "token_file",
        "e2e_key",
        "e2e_key_file",
        "perm",
        "temp_rank",
        "will_hub",
        "bind",
        "port",
        "peers",
        "poll_interval",
        "reconnect_delay",
        "connect_timeout",
    }
)


class ConfigError(Exception):
    """Configuration parsing or validation error."""


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Validated runtime configuration for the clipboard daemon."""

    device_id: str
    token: str
    relay_url: str | None = None
    e2e_key: str | None = None
    name: str | None = None
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True
    bind: str = "127.0.0.1"
    port: int = 8765
    peers: tuple[PeerConfig, ...] = ()
    poll_interval: float = 0.5
    reconnect_delay: float = 0.2
    connect_timeout: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.peers, tuple):
            object.__setattr__(self, "peers", tuple(self.peers))

    def to_mesh_config(self) -> MeshConfig:
        """Convert this configuration into a meshcore MeshConfig."""
        return MeshConfig(
            device_id=self.device_id,
            name=self.name if self.name is not None else self.device_id,
            token=self.token,
            perm=self.perm,
            temp_rank=self.temp_rank,
            will_hub=self.will_hub,
            bind=self.bind,
            port=self.port,
            peers=self.peers,
            reconnect_delay=self.reconnect_delay,
            connect_timeout=self.connect_timeout,
        )


def _validate_positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a positive finite number, got {value!r}")
    val = float(value)
    if not math.isfinite(val) or val <= 0:
        raise ConfigError(f"{name} must be a positive finite number, got {value!r}")
    return val


def _resolve_secret(
    config_dir: Path,
    inline_val: Any,
    file_val: Any,
    name: str,
    file_key: str,
) -> str | None:
    if inline_val is not None and file_val is not None:
        raise ConfigError(f"Cannot specify both {name!r} and {file_key!r}")
    if inline_val is not None:
        if not isinstance(inline_val, str) or not inline_val.strip():
            raise ConfigError(f"{name!r} must be a non-empty string")
        return inline_val.strip()
    if file_val is not None:
        if not isinstance(file_val, str) or not file_val.strip():
            raise ConfigError(f"{file_key!r} must be a non-empty string path")
        file_path = Path(file_val.strip())
        if not file_path.is_absolute():
            file_path = config_dir / file_path
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Failed to read {file_key!r} at {file_path}: {exc}") from exc
        stripped = content.strip()
        if not stripped:
            raise ConfigError(f"Secret file {file_key!r} at {file_path} is empty")
        return stripped
    return None


def load_config(path: str | Path) -> DaemonConfig:
    """Load, validate, and return a :class:`DaemonConfig` from a TOML file.

    Parameters
    ----------
    path:
        Path to the TOML configuration file.

    Returns
    -------
    DaemonConfig:
        The validated configuration.

    Raises
    ------
    ConfigError:
        If the file cannot be read, contains invalid TOML, has unknown or
        invalid keys, or references missing/empty secret files.
    """
    config_path = Path(path)
    try:
        raw_bytes = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Failed to read config file at {config_path}: {exc}") from exc

    try:
        raw_text = raw_bytes.decode("utf-8")
        data = tomllib.loads(raw_text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Invalid TOML in config file {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a table, got {type(data).__name__}")

    unknown_keys = set(data.keys()) - _ALLOWED_KEYS
    if unknown_keys:
        raise ConfigError(
            f"Unknown configuration key(s): {', '.join(sorted(unknown_keys))}"
        )

    config_dir = config_path.parent

    # device_id
    if "device_id" not in data or data["device_id"] is None:
        raise ConfigError("Missing required key: 'device_id'")
    device_id_raw = data["device_id"]
    if not isinstance(device_id_raw, str):
        raise ConfigError("'device_id' must be a string")
    device_id = device_id_raw.strip()
    if not (1 <= len(device_id) <= 128):
        raise ConfigError(
            f"'device_id' must be between 1 and 128 characters after strip, got length {len(device_id)}"
        )

    # name
    name: str | None = None
    if "name" in data and data["name"] is not None:
        if not isinstance(data["name"], str):
            raise ConfigError("'name' must be a string")
        name = data["name"].strip()
        if not (1 <= len(name) <= 128):
            raise ConfigError(
                f"'name' must be between 1 and 128 characters after strip, got length {len(name)}"
            )

    # perm
    perm: int | None = None
    if "perm" in data and data["perm"] is not None:
        val = data["perm"]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ConfigError(f"'perm' must be a non-negative int, got {val!r}")
        perm = val

    # temp_rank
    temp_rank: int | None = None
    if "temp_rank" in data and data["temp_rank"] is not None:
        val = data["temp_rank"]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ConfigError(f"'temp_rank' must be a non-negative int, got {val!r}")
        temp_rank = val

    # will_hub
    will_hub: bool = True
    if "will_hub" in data and data["will_hub"] is not None:
        val = data["will_hub"]
        if not isinstance(val, bool):
            raise ConfigError(f"'will_hub' must be a bool, got {val!r}")
        will_hub = val

    # bind
    bind: str = "127.0.0.1"
    if "bind" in data and data["bind"] is not None:
        val = data["bind"]
        if not isinstance(val, str) or not val.strip():
            raise ConfigError("'bind' must be a non-empty string")
        bind = val.strip()

    # port
    port: int = 8765
    if "port" in data and data["port"] is not None:
        val = data["port"]
        if isinstance(val, bool) or not isinstance(val, int) or not (0 <= val <= 65535):
            raise ConfigError(f"'port' must be an int between 0 and 65535, got {val!r}")
        port = val

    # peers
    peers_list: list[PeerConfig] = []
    if "peers" in data and data["peers"] is not None:
        raw_peers = data["peers"]
        if not isinstance(raw_peers, list):
            raise ConfigError(f"'peers' must be a list of tables, got {type(raw_peers).__name__}")
        _peer_allowed_keys = frozenset({"device_id", "name", "address", "perm", "temp_rank", "will_hub"})
        for i, p in enumerate(raw_peers):
            if not isinstance(p, dict):
                raise ConfigError(f"peer #{i} must be a table")
            p_unknown = set(p.keys()) - _peer_allowed_keys
            if p_unknown:
                raise ConfigError(f"Unknown peer key(s) in peer #{i}: {', '.join(sorted(p_unknown))}")
            if "device_id" not in p or not isinstance(p["device_id"], str) or not p["device_id"].strip():
                raise ConfigError(f"peer #{i} missing required string 'device_id'")
            if "name" not in p or not isinstance(p["name"], str) or not p["name"].strip():
                raise ConfigError(f"peer #{i} missing required string 'name'")
            if "address" not in p or not isinstance(p["address"], str) or not p["address"].strip():
                raise ConfigError(f"peer #{i} missing required string 'address'")
            p_perm = None
            if "perm" in p and p["perm"] is not None:
                if isinstance(p["perm"], bool) or not isinstance(p["perm"], int) or p["perm"] < 0:
                    raise ConfigError(f"peer #{i} 'perm' must be a non-negative int, got {p['perm']!r}")
                p_perm = p["perm"]
            p_temp = None
            if "temp_rank" in p and p["temp_rank"] is not None:
                if isinstance(p["temp_rank"], bool) or not isinstance(p["temp_rank"], int) or p["temp_rank"] < 0:
                    raise ConfigError(f"peer #{i} 'temp_rank' must be a non-negative int, got {p['temp_rank']!r}")
                p_temp = p["temp_rank"]
            p_will = True
            if "will_hub" in p and p["will_hub"] is not None:
                if not isinstance(p["will_hub"], bool):
                    raise ConfigError(f"peer #{i} 'will_hub' must be a bool, got {p['will_hub']!r}")
                p_will = p["will_hub"]
            peers_list.append(
                PeerConfig(
                    device_id=p["device_id"].strip(),
                    name=p["name"].strip(),
                    address=p["address"].strip(),
                    perm=p_perm,
                    temp_rank=p_temp,
                    will_hub=p_will,
                )
            )
    peers = tuple(peers_list)

    # relay_url
    relay_url: str | None = None
    if "relay_url" in data and data["relay_url"] is not None:
        relay_url_raw = data["relay_url"]
        if not isinstance(relay_url_raw, str):
            raise ConfigError("'relay_url' must be a string")
        parsed_url = urllib.parse.urlsplit(relay_url_raw)
        if parsed_url.scheme not in ("ws", "wss") or not parsed_url.hostname:
            raise ConfigError(
                f"'relay_url' must have scheme 'ws' or 'wss' and a non-empty host, got {relay_url_raw!r}"
            )
        relay_url = relay_url_raw
    elif not peers:
        raise ConfigError("Missing required key: 'relay_url'")

    # token / token_file
    token = _resolve_secret(
        config_dir,
        data.get("token"),
        data.get("token_file"),
        "token",
        "token_file",
    )
    if token is None:
        raise ConfigError("Exactly one of 'token' or 'token_file' must be specified")

    # e2e_key / e2e_key_file
    e2e_key = _resolve_secret(
        config_dir,
        data.get("e2e_key"),
        data.get("e2e_key_file"),
        "e2e_key",
        "e2e_key_file",
    )
    if e2e_key is not None:
        try:
            FernetCodec(e2e_key)
        except CryptoError as exc:
            raise ConfigError("Invalid Fernet key in 'e2e_key' / 'e2e_key_file'") from exc

    # numeric values
    poll_interval = 0.5
    if "poll_interval" in data:
        poll_interval = _validate_positive_float(data["poll_interval"], "poll_interval")

    reconnect_delay = 0.2
    if "reconnect_delay" in data:
        reconnect_delay = _validate_positive_float(
            data["reconnect_delay"], "reconnect_delay"
        )

    connect_timeout = 5.0
    if "connect_timeout" in data:
        connect_timeout = _validate_positive_float(
            data["connect_timeout"], "connect_timeout"
        )

    return DaemonConfig(
        device_id=device_id,
        token=token,
        relay_url=relay_url,
        e2e_key=e2e_key,
        name=name,
        perm=perm,
        temp_rank=temp_rank,
        will_hub=will_hub,
        bind=bind,
        port=port,
        peers=peers,
        poll_interval=poll_interval,
        reconnect_delay=reconnect_delay,
        connect_timeout=connect_timeout,
    )
