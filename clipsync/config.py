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

from meshcore.addressing import ADDRESS_KINDS, Address
from meshcore.config import GroupConfig, MeshConfig, PeerConfig, RelayConfig
from meshcore.discovery import DEFAULT_DISCOVERY_PORT
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
        "group_id",
        "allow_insecure",
        "role",
        "serve_groups",
        "relay",
        "bind",
        "port",
        "peers",
        "discovery",
        "discovery_port",
        "state_dir",
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
    group_id: str = "default"
    allow_insecure: bool = False
    role: str = "member"
    serve_groups: tuple[GroupConfig, ...] = ()
    relay: RelayConfig | None = None
    bind: str = "127.0.0.1"
    port: int = 8765
    peers: tuple[PeerConfig, ...] = ()
    discovery: bool = False
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    state_dir: str | None = None
    poll_interval: float = 0.5
    reconnect_delay: float = 0.2
    connect_timeout: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.peers, tuple):
            object.__setattr__(self, "peers", tuple(self.peers))
        if not isinstance(self.serve_groups, tuple):
            object.__setattr__(self, "serve_groups", tuple(self.serve_groups))

    def to_mesh_config(self) -> MeshConfig:
        """Convert this configuration into a meshcore MeshConfig."""
        return MeshConfig(
            device_id=self.device_id,
            name=self.name if self.name is not None else self.device_id,
            token=self.token,
            perm=self.perm,
            temp_rank=self.temp_rank,
            will_hub=self.will_hub,
            group_id=self.group_id,
            e2e_key=self.e2e_key,
            allow_insecure=self.allow_insecure,
            role=self.role,
            serve_groups=self.serve_groups,
            relay=self.relay,
            bind=self.bind,
            port=self.port,
            peers=self.peers,
            discovery=self.discovery,
            discovery_port=self.discovery_port,
            state_dir=self.state_dir,
            reconnect_delay=self.reconnect_delay,
            connect_timeout=self.connect_timeout,
        )

    def uses_meshcore(self) -> bool:
        """Whether this config must run on a meshcore Node (not the legacy relay)."""
        return bool(self.peers) or self.role == "relay" or self.relay is not None


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

    # group_id
    group_id: str = "default"
    if "group_id" in data and data["group_id"] is not None:
        val = data["group_id"]
        if not isinstance(val, str):
            raise ConfigError("'group_id' must be a string")
        group_id = val.strip()
        if not (1 <= len(group_id) <= 128):
            raise ConfigError(
                f"'group_id' must be between 1 and 128 characters after strip, got length {len(group_id)}"
            )

    # allow_insecure
    allow_insecure: bool = False
    if "allow_insecure" in data and data["allow_insecure"] is not None:
        val = data["allow_insecure"]
        if not isinstance(val, bool):
            raise ConfigError(f"'allow_insecure' must be a bool, got {val!r}")
        allow_insecure = val

    # role
    role: str = "member"
    if "role" in data and data["role"] is not None:
        val = data["role"]
        if not isinstance(val, str) or val.strip() not in ("member", "relay"):
            raise ConfigError(f"'role' must be 'member' or 'relay', got {val!r}")
        role = val.strip()

    # serve_groups (hub side: ids and tokens only, never keys)
    serve_groups_list: list[GroupConfig] = []
    if "serve_groups" in data and data["serve_groups"] is not None:
        raw_groups = data["serve_groups"]
        if not isinstance(raw_groups, list):
            raise ConfigError(
                f"'serve_groups' must be a list of tables, got {type(raw_groups).__name__}"
            )
        _group_allowed_keys = frozenset({"id", "token"})
        for i, group in enumerate(raw_groups):
            if not isinstance(group, dict):
                raise ConfigError(f"serve_groups #{i} must be a table")
            g_unknown = set(group.keys()) - _group_allowed_keys
            if g_unknown:
                raise ConfigError(
                    f"Unknown serve_groups key(s) in entry #{i}: {', '.join(sorted(g_unknown))}"
                )
            if "id" not in group or not isinstance(group["id"], str) or not group["id"].strip():
                raise ConfigError(f"serve_groups #{i} missing required string 'id'")
            if "token" not in group or not isinstance(group["token"], str) or not group["token"].strip():
                raise ConfigError(f"serve_groups #{i} missing required string 'token'")
            try:
                serve_groups_list.append(GroupConfig(group["id"].strip(), group["token"].strip()))
            except ValueError as exc:
                raise ConfigError(f"Invalid serve_groups #{i}: {exc}") from exc
        g_ids = [group.id for group in serve_groups_list]
        g_tokens = [group.token for group in serve_groups_list]
        if len(set(g_ids)) != len(g_ids):
            raise ConfigError("'serve_groups' ids must be unique")
        if len(set(g_tokens)) != len(g_tokens):
            raise ConfigError("'serve_groups' tokens must be unique")
    serve_groups = tuple(serve_groups_list)

    # relay (member side: preferred hub address)
    relay_obj: RelayConfig | None = None
    if "relay" in data and data["relay"] is not None:
        raw_relay = data["relay"]
        if not isinstance(raw_relay, dict):
            raise ConfigError(f"'relay' must be a table, got {type(raw_relay).__name__}")
        r_unknown = set(raw_relay.keys()) - frozenset({"address", "device_id"})
        if r_unknown:
            raise ConfigError(f"Unknown relay key(s): {', '.join(sorted(r_unknown))}")
        if "address" not in raw_relay or not isinstance(raw_relay["address"], str) or not raw_relay["address"].strip():
            raise ConfigError("'relay' missing required string 'address'")
        r_device_id = "relay"
        if "device_id" in raw_relay and raw_relay["device_id"] is not None:
            if not isinstance(raw_relay["device_id"], str) or not raw_relay["device_id"].strip():
                raise ConfigError("'relay.device_id' must be a non-empty string")
            r_device_id = raw_relay["device_id"].strip()
        try:
            relay_obj = RelayConfig(address=raw_relay["address"].strip(), device_id=r_device_id)
        except ValueError as exc:
            raise ConfigError(f"Invalid 'relay': {exc}") from exc

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
        _peer_allowed_keys = frozenset(
            {"device_id", "name", "address", "addresses", "perm", "temp_rank", "will_hub"}
        )
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

            p_address = ""
            if "address" in p and p["address"] is not None:
                if not isinstance(p["address"], str) or not p["address"].strip():
                    raise ConfigError(f"peer #{i} 'address' must be a non-empty string")
                p_address = p["address"].strip()

            p_addresses_list: list[Address] = []
            if "addresses" in p and p["addresses"] is not None:
                raw_addrs = p["addresses"]
                if not isinstance(raw_addrs, list):
                    raise ConfigError(f"peer #{i} 'addresses' must be a list of tables")
                _addr_allowed_keys = frozenset({"kind", "host", "port"})
                for j, a in enumerate(raw_addrs):
                    if not isinstance(a, dict):
                        raise ConfigError(f"peer #{i} address #{j} must be a table")
                    a_unknown = set(a.keys()) - _addr_allowed_keys
                    if a_unknown:
                        raise ConfigError(
                            f"Unknown key(s) in peer #{i} address #{j}: {', '.join(sorted(a_unknown))}"
                        )
                    if (
                        "kind" not in a
                        or not isinstance(a["kind"], str)
                        or a["kind"].strip() not in ADDRESS_KINDS
                    ):
                        raise ConfigError(
                            f"peer #{i} address #{j} 'kind' must be one of {ADDRESS_KINDS}"
                        )
                    if "host" not in a or not isinstance(a["host"], str) or not a["host"].strip():
                        raise ConfigError(f"peer #{i} address #{j} 'host' must be a non-empty string")
                    a_port = None
                    if "port" in a and a["port"] is not None:
                        if (
                            isinstance(a["port"], bool)
                            or not isinstance(a["port"], int)
                            or not (1 <= a["port"] <= 65535)
                        ):
                            raise ConfigError(
                                f"peer #{i} address #{j} 'port' must be an int between 1 and 65535"
                            )
                        a_port = a["port"]
                    try:
                        p_addresses_list.append(
                            Address(
                                kind=a["kind"].strip(),
                                host=a["host"].strip(),
                                port=a_port,
                            )
                        )
                    except ValueError as exc:
                        raise ConfigError(f"Invalid address in peer #{i}: {exc}") from exc

            if not p_address and not p_addresses_list:
                raise ConfigError(
                    f"peer #{i} requires at least one of 'address' or 'addresses'"
                )

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
                    address=p_address,
                    perm=p_perm,
                    temp_rank=p_temp,
                    will_hub=p_will,
                    addresses=tuple(p_addresses_list),
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
    elif not peers and relay_obj is None and role != "relay":
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

    # cross-field validation for groups / relay role
    if role == "relay":
        if not serve_groups:
            raise ConfigError("'role = \"relay\"' requires at least one 'serve_groups' entry")
        if peers:
            raise ConfigError("'role = \"relay\"' must not configure 'peers'")
        if e2e_key is not None:
            raise ConfigError("'role = \"relay\"' must not have an 'e2e_key'")
        if relay_obj is not None:
            raise ConfigError("'role = \"relay\"' must not configure a 'relay'")
    elif serve_groups:
        raise ConfigError("'serve_groups' is only valid with 'role = \"relay\"'")

    if relay_obj is not None:
        if relay_obj.device_id == device_id:
            raise ConfigError("'relay.device_id' must differ from 'device_id'")
        if any(peer.device_id == relay_obj.device_id for peer in peers):
            raise ConfigError("'relay.device_id' must differ from every peer 'device_id'")

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

    # discovery, discovery_port, state_dir
    discovery = False
    if "discovery" in data and data["discovery"] is not None:
        if not isinstance(data["discovery"], bool):
            raise ConfigError("'discovery' must be a bool")
        discovery = data["discovery"]

    discovery_port = DEFAULT_DISCOVERY_PORT
    if "discovery_port" in data and data["discovery_port"] is not None:
        val = data["discovery_port"]
        if isinstance(val, bool) or not isinstance(val, int) or not (1 <= val <= 65535):
            raise ConfigError(f"'discovery_port' must be an int between 1 and 65535, got {val!r}")
        discovery_port = val

    state_dir = None
    if "state_dir" in data and data["state_dir"] is not None:
        val = data["state_dir"]
        if not isinstance(val, str) or not val.strip():
            raise ConfigError("'state_dir' must be a non-empty string")
        state_dir = val.strip()

    return DaemonConfig(
        device_id=device_id,
        token=token,
        relay_url=relay_url,
        e2e_key=e2e_key,
        name=name,
        perm=perm,
        temp_rank=temp_rank,
        will_hub=will_hub,
        group_id=group_id,
        allow_insecure=allow_insecure,
        role=role,
        serve_groups=serve_groups,
        relay=relay_obj,
        bind=bind,
        port=port,
        peers=peers,
        discovery=discovery,
        discovery_port=discovery_port,
        state_dir=state_dir,
        poll_interval=poll_interval,
        reconnect_delay=reconnect_delay,
        connect_timeout=connect_timeout,
    )
