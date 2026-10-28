"""Address types, ordering, and last-good address persistence for meshcore."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterable
import urllib.parse

__all__ = [
    "ADDRESS_KINDS",
    "DEFAULT_WS_PORT",
    "Address",
    "LastGoodStore",
    "default_state_dir",
    "ordered_addresses",
    "parse_address",
]

logger = logging.getLogger("meshcore.addressing")

ADDRESS_KINDS: tuple[str, ...] = ("tailnet", "lan", "hotspot", "static")
DEFAULT_WS_PORT: int = 8765

_KIND_RANKS: dict[str, int] = {kind: i for i, kind in enumerate(ADDRESS_KINDS)}


@dataclass(frozen=True, eq=False)
class Address:
    """A transport address for a node/peer."""

    kind: str
    host: str
    port: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ADDRESS_KINDS:
            raise ValueError(
                f"kind must be one of {ADDRESS_KINDS}, got {self.kind!r}"
            )
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError(f"host must be a non-empty string, got {self.host!r}")
        object.__setattr__(self, "host", self.host.strip())

        if self.port is not None:
            if (
                isinstance(self.port, bool)
                or not isinstance(self.port, int)
                or not (1 <= self.port <= 65535)
            ):
                raise ValueError(
                    f"port must be an int between 1 and 65535 or None, got {self.port!r}"
                )

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else DEFAULT_WS_PORT

    @property
    def key(self) -> tuple[str, int]:
        return (self.host.lower(), self.effective_port)

    @property
    def websocket_url(self) -> str:
        host = self.host
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            host = f"[{host}]"
        return f"ws://{host}:{self.effective_port}/"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Address):
            return self.key == other.key
        return False

    def __hash__(self) -> int:
        return hash(self.key)


def parse_address(value: str | Address) -> Address:
    """Parse a string or Address into an Address with kind 'static'.

    Accepts 'host:port', 'host', 'ws://host:port/path', 'wss://...', and '[::1]:port'.
    """
    if isinstance(value, Address):
        return Address("static", value.host, value.port)
    if not isinstance(value, str):
        raise ValueError(
            f"address must be a string or Address, got {type(value).__name__}"
        )
    val = value.strip()
    if not val:
        raise ValueError("address must be a non-empty string")

    url = val
    if not (url.startswith("ws://") or url.startswith("wss://")):
        url = f"ws://{url}"

    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as exc:
        raise ValueError(f"Invalid address {value!r}: {exc}") from exc

    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid address (missing host): {value!r}")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid port in {value!r}: {exc}") from exc

    return Address("static", host, port)


def _last_good_key(last_good: Any) -> tuple[str, int] | None:
    if last_good is None:
        return None
    if isinstance(last_good, Address):
        return last_good.key
    if isinstance(last_good, tuple) and len(last_good) == 2:
        host, port = last_good
        return (str(host).strip().lower(), int(port))
    if isinstance(last_good, str):
        try:
            parsed = parse_address(last_good)
            return parsed.key
        except ValueError:
            return None
    return None


def ordered_addresses(
    peer: Any,
    discovered: Iterable[Address] = (),
    last_good: Address | str | tuple[str, int] | None = None,
) -> list[Address]:
    """Pure address ordering for a peer candidate.

    Order:
    1. Configured peer.addresses in config order.
    2. Fallback peer.address (kind 'static') if non-empty.
    3. Merged discovered kind 'lan' entries (after configured 'lan' entries).
    4. Stable sort by kind rank: tailnet, lan, hotspot, static.
    5. Deduplicated by Address.key (first occurrence wins).
    6. If last_good key is present, move that entry to front.
    """
    configured = list(getattr(peer, "addresses", ()) or ())

    legacy_raw = getattr(peer, "address", "") or ""
    legacy_list: list[Address] = []
    if isinstance(legacy_raw, str) and legacy_raw.strip():
        try:
            legacy_list.append(parse_address(legacy_raw))
        except ValueError:
            pass

    lan_discovered = [
        addr for addr in discovered if isinstance(addr, Address) and addr.kind == "lan"
    ]

    candidates = configured + lan_discovered + legacy_list
    candidates.sort(key=lambda a: _KIND_RANKS.get(a.kind, 99))

    deduped: list[Address] = []
    seen: set[tuple[str, int]] = set()
    for a in candidates:
        if a.key not in seen:
            seen.add(a.key)
            deduped.append(a)

    lg_key = _last_good_key(last_good)
    if lg_key is not None and lg_key in seen:
        idx = next((i for i, a in enumerate(deduped) if a.key == lg_key), None)
        if idx is not None and idx > 0:
            target = deduped.pop(idx)
            deduped.insert(0, target)

    return deduped


def default_state_dir() -> Path:
    """Return default state directory ($XDG_STATE_HOME/meshcore or ~/.local/state/meshcore)."""
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home and xdg_state_home.strip():
        return Path(xdg_state_home.strip()) / "meshcore"
    return Path.home() / ".local" / "state" / "meshcore"


class LastGoodStore:
    """Thread-safe, atomic store for last-working addresses per peer."""

    def __init__(self, path: str | Path) -> None:
        self._path: Path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def get(self, device_id: str) -> Address | None:
        """Return remembered Address(kind='static', host, port) for device_id, or None."""
        if not isinstance(device_id, str) or not device_id.strip():
            return None
        dev_id = device_id.strip()
        with self._lock:
            try:
                if not self._path.is_file():
                    return None
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return None
                val = data.get(dev_id)
                if not isinstance(val, str) or not val.strip():
                    return None
                return parse_address(val)
            except Exception:
                return None

    def set(self, device_id: str, address: Address) -> None:
        """Persist host:port for device_id. Atomic, never raises."""
        if not isinstance(device_id, str) or not device_id.strip():
            return
        if not isinstance(address, Address):
            return
        dev_id = device_id.strip()
        val_str = f"{address.host}:{address.effective_port}"

        with self._lock:
            try:
                data: dict[str, str] = {}
                if self._path.is_file():
                    try:
                        raw = json.loads(self._path.read_text(encoding="utf-8"))
                        if isinstance(raw, dict):
                            data = {
                                str(k): str(v)
                                for k, v in raw.items()
                                if isinstance(k, str) and isinstance(v, str)
                            }
                    except Exception:
                        data = {}

                if data.get(dev_id) == val_str:
                    return

                data[dev_id] = val_str

                parent = self._path.parent
                parent.mkdir(parents=True, exist_ok=True)

                temp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        "w", dir=parent, delete=False, encoding="utf-8"
                    ) as tf:
                        temp_path = Path(tf.name)
                        json.dump(data, tf)
                        tf.flush()
                        os.fsync(tf.fileno())

                    os.replace(temp_path, self._path)
                    temp_path = None
                finally:
                    if temp_path is not None:
                        try:
                            temp_path.unlink(missing_ok=True)
                        except OSError:
                            pass
            except Exception as exc:
                logger.warning(
                    "Failed to save last_good store at %s: %s", self._path, exc
                )
