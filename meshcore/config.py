"""Configuration data classes and helpers for meshcore."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable
import urllib.parse

from meshcore.messages import Hello

DEFAULT_MAX_FRAME_BYTES: int = 4 * ((8 * 1024 * 1024 + 2) // 3) + 8192


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
class PeerConfig:
    """Static configuration for a known peer device."""

    device_id: str
    name: str
    address: str
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.device_id, "device_id")
        _validate_non_empty_str(self.name, "name")
        _validate_non_empty_str(self.address, "address")
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
    bind: str = "127.0.0.1"
    port: int = 8765
    peers: tuple[PeerConfig, ...] = ()
    heartbeat: float = 10.0
    missed_pongs: int = 3
    probe_timeout: float = 2.0
    debounce: float = 30.0
    reconnect_delay: float = 0.2
    connect_timeout: float = 5.0
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.device_id, "device_id")
        _validate_non_empty_str(self.name, "name")
        _validate_non_empty_str(self.token, "token")
        _validate_non_negative_int(self.perm, "perm")
        _validate_non_negative_int(self.temp_rank, "temp_rank")
        if not isinstance(self.will_hub, bool):
            raise ValueError(f"will_hub must be a bool, got {self.will_hub!r}")
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

        _validate_positive_float(self.heartbeat, "heartbeat")
        if isinstance(self.missed_pongs, bool) or not isinstance(self.missed_pongs, int) or self.missed_pongs < 1:
            raise ValueError(f"missed_pongs must be an int >= 1, got {self.missed_pongs!r}")
        _validate_positive_float(self.probe_timeout, "probe_timeout")
        _validate_non_negative_float(self.debounce, "debounce")
        _validate_positive_float(self.reconnect_delay, "reconnect_delay")
        _validate_positive_float(self.connect_timeout, "connect_timeout")

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
        )
