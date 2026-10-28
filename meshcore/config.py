"""Configuration data classes and helpers for meshcore."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable
import urllib.parse

from meshcore.addressing import Address
from meshcore.messages import Hello

DEFAULT_MAX_FRAME_BYTES: int = 4 * ((8 * 1024 * 1024 + 2) // 3) + 8192
GROUP_DEFAULT: str = "default"

__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "GROUP_DEFAULT",
    "GroupConfig",
    "MeshConfig",
    "PeerConfig",
    "RelayConfig",
    "format_ws_url",
]


def format_ws_url(address: str) -> str:
    """Format an address or URL as a WebSocket URL with trailing slash.

    If the address starts with ws:// or wss://, keeps the scheme.
    Otherwise defaults to ws://. Gains a trailing / only if it has no path.
    """
    if not isinstance(address, str) or not address.strip():
        raise ValueError("address must be a non-empty string")
    addr = address.strip()
    if not (addr.startswith("ws://") or addr.startswith("wss://")):
        addr = f"ws://{addr}"
    parsed = urllib.parse.urlsplit(addr)
    if not parsed.path:
        addr = f"{addr}/"
    return addr


def _validate_non_empty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _validate_non_negative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int, got {value!r}")
    return value


def _validate_positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    val = float(value)
    if not math.isfinite(val) or val <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return val


def _validate_non_negative_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-negative finite number, got {value!r}")
    val = float(value)
    if not math.isfinite(val) or val < 0:
        raise ValueError(f"{name} must be a non-negative finite number, got {value!r}")
    return val


@dataclass(frozen=True)
class GroupConfig:
    """Configuration for a served group on a HubServer."""

    id: str
    token: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.id, "id")
        _validate_non_empty_str(self.token, "token")


@dataclass(frozen=True)
class RelayConfig:
    """Member-side configuration for a preferred relay hub."""

    address: str
    device_id: str = "relay"

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.address, "address")
        _validate_non_empty_str(self.device_id, "device_id")


@dataclass(frozen=True)
class PeerConfig:
    """Static configuration for a known peer device."""

    device_id: str
    name: str
    address: str = ""
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True
    addresses: tuple[Address, ...] = ()

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.device_id, "device_id")
        _validate_non_empty_str(self.name, "name")
        if not isinstance(self.address, str):
            raise ValueError(f"address must be a str, got {type(self.address).__name__}")
        addr_clean = self.address.strip()
        object.__setattr__(self, "address", addr_clean)

        if not isinstance(self.addresses, tuple):
            object.__setattr__(self, "addresses", tuple(self.addresses))

        for a in self.addresses:
            if not isinstance(a, Address):
                raise ValueError(
                    f"addresses items must be Address instances, got {type(a).__name__}"
                )

        if not addr_clean and not self.addresses:
            raise ValueError("PeerConfig requires non-empty address or non-empty addresses")

        _validate_non_negative_int(self.perm, "perm")
        _validate_non_negative_int(self.temp_rank, "temp_rank")
        if not isinstance(self.will_hub, bool):
            raise ValueError(f"will_hub must be a bool, got {self.will_hub!r}")


@dataclass(frozen=True)
class MeshConfig:
    """Runtime configuration for a meshcore node."""

    device_id: str
    name: str
    token: str
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True
    group_id: str = GROUP_DEFAULT
    e2e_key: str | None = None
    allow_insecure: bool = False
    role: str = "member"
    serve_groups: tuple[GroupConfig, ...] = ()
    relay: RelayConfig | None = None
    bind: str = "127.0.0.1"
    port: int = 8765
    peers: tuple[PeerConfig, ...] = ()
    discovery: bool = False
    discovery_port: int = 8766
    discovery_interval: float = 5.0
    state_dir: str | None = None
    heartbeat: float = 10.0
    missed_pongs: int = 3
    probe_timeout: float = 2.0
    debounce: float = 30.0
    reconnect_delay: float = 0.2
    connect_timeout: float = 5.0
    address_timeout: float = 2.0
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.device_id, "device_id")
        _validate_non_empty_str(self.name, "name")
        _validate_non_empty_str(self.token, "token")
        _validate_non_negative_int(self.perm, "perm")
        _validate_non_negative_int(self.temp_rank, "temp_rank")
        if not isinstance(self.will_hub, bool):
            raise ValueError(f"will_hub must be a bool, got {self.will_hub!r}")

        _validate_non_empty_str(self.group_id, "group_id")
        if self.e2e_key is not None:
            if not isinstance(self.e2e_key, str) or not self.e2e_key.strip():
                raise ValueError("e2e_key must be None or a non-empty string")
        if not isinstance(self.allow_insecure, bool):
            raise ValueError(f"allow_insecure must be a bool, got {self.allow_insecure!r}")
        if self.role not in ("member", "relay"):
            raise ValueError(f"role must be 'member' or 'relay', got {self.role!r}")

        if isinstance(self.serve_groups, tuple):
            serve_groups_tuple = self.serve_groups
        else:
            serve_groups_tuple = tuple(self.serve_groups)
            object.__setattr__(self, "serve_groups", serve_groups_tuple)

        for g in serve_groups_tuple:
            if not isinstance(g, GroupConfig):
                raise ValueError(
                    f"All serve_groups must be GroupConfig instances, got {type(g).__name__}"
                )
        g_ids = [g.id for g in serve_groups_tuple]
        g_tokens = [g.token for g in serve_groups_tuple]
        if len(set(g_ids)) != len(g_ids):
            raise ValueError("serve_groups ids must be unique")
        if len(set(g_tokens)) != len(g_tokens):
            raise ValueError("serve_groups tokens must be unique")

        _validate_non_empty_str(self.bind, "bind")

        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (0 <= self.port <= 65535):
            raise ValueError(f"port must be an int between 0 and 65535, got {self.port!r}")

        if isinstance(self.peers, tuple):
            peers_tuple = self.peers
        else:
            peers_tuple = tuple(self.peers)
            object.__setattr__(self, "peers", peers_tuple)

        for p in peers_tuple:
            if not isinstance(p, PeerConfig):
                raise ValueError(f"All peers must be PeerConfig instances, got {type(p).__name__}")

        if self.role == "member":
            if serve_groups_tuple:
                raise ValueError("member role must not set serve_groups")
            if self.relay is not None:
                if not isinstance(self.relay, RelayConfig):
                    raise ValueError(f"relay must be a RelayConfig instance, got {type(self.relay).__name__}")
                if self.relay.device_id == self.device_id:
                    raise ValueError("relay device_id cannot match node device_id")
                peer_ids = {p.device_id for p in peers_tuple}
                if self.relay.device_id in peer_ids:
                    raise ValueError("relay device_id cannot match peer device_id")
        elif self.role == "relay":
            if not serve_groups_tuple:
                raise ValueError("relay role requires non-empty serve_groups")
            if peers_tuple:
                raise ValueError("relay role must not have peers")
            if self.e2e_key is not None:
                raise ValueError("relay role must not have e2e_key")
            if self.relay is not None:
                raise ValueError("relay role must not have relay")

        _validate_positive_float(self.heartbeat, "heartbeat")
        if isinstance(self.missed_pongs, bool) or not isinstance(self.missed_pongs, int) or self.missed_pongs < 1:
            raise ValueError(f"missed_pongs must be an int >= 1, got {self.missed_pongs!r}")
        _validate_positive_float(self.probe_timeout, "probe_timeout")
        _validate_non_negative_float(self.debounce, "debounce")
        _validate_positive_float(self.reconnect_delay, "reconnect_delay")
        _validate_positive_float(self.connect_timeout, "connect_timeout")
        _validate_positive_float(self.address_timeout, "address_timeout")

        if not isinstance(self.discovery, bool):
            raise ValueError(f"discovery must be a bool, got {self.discovery!r}")
        if (
            isinstance(self.discovery_port, bool)
            or not isinstance(self.discovery_port, int)
            or not (1 <= self.discovery_port <= 65535)
        ):
            raise ValueError(
                f"discovery_port must be an int between 1 and 65535, got {self.discovery_port!r}"
            )
        _validate_positive_float(self.discovery_interval, "discovery_interval")
        if self.state_dir is not None:
            if not isinstance(self.state_dir, str) or not self.state_dir.strip():
                raise ValueError("state_dir must be None or a non-empty string")

        if isinstance(self.max_frame_bytes, bool) or not isinstance(self.max_frame_bytes, int) or self.max_frame_bytes <= 0:
            raise ValueError(f"max_frame_bytes must be a positive int, got {self.max_frame_bytes!r}")

    def hello(self) -> Hello:
        """Construct the local Hello announcement for this node."""
        return Hello(
            device_id=self.device_id,
            name=self.name,
            perm=self.perm,
            temp_rank=self.temp_rank,
            will_hub=self.will_hub,
            group_id=self.group_id,
        )
