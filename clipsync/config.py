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

from .crypto import CryptoError, FernetCodec

__all__ = ["ConfigError", "DaemonConfig", "load_config"]

_ALLOWED_KEYS = frozenset(
    {
        "device_id",
        "relay_url",
        "token",
        "token_file",
        "e2e_key",
        "e2e_key_file",
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
    relay_url: str
    token: str
    e2e_key: str | None = None
    poll_interval: float = 0.5
    reconnect_delay: float = 0.2
    connect_timeout: float = 5.0


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

    # relay_url
    if "relay_url" not in data or data["relay_url"] is None:
        raise ConfigError("Missing required key: 'relay_url'")
    relay_url_raw = data["relay_url"]
    if not isinstance(relay_url_raw, str):
        raise ConfigError("'relay_url' must be a string")
    parsed_url = urllib.parse.urlsplit(relay_url_raw)
    if parsed_url.scheme not in ("ws", "wss") or not parsed_url.hostname:
        raise ConfigError(
            f"'relay_url' must have scheme 'ws' or 'wss' and a non-empty host, got {relay_url_raw!r}"
        )
    relay_url = relay_url_raw

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
        relay_url=relay_url,
        token=token,
        e2e_key=e2e_key,
        poll_interval=poll_interval,
        reconnect_delay=reconnect_delay,
        connect_timeout=connect_timeout,
    )
