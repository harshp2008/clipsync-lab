"""Pure, app-agnostic coordination core for meshcore."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import itertools
import re
import threading
import time
from typing import Callable, Iterable

MAX_STATE_PAYLOAD_BYTES: int = 8 * 1024 * 1024  # 8 MiB
MAX_CLOCK_AHEAD_S: int = 300  # 5 minutes
MAX_STATE_KEYS_PER_GROUP: int = 16

_STATE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def is_valid_state_key(key: str) -> bool:
    """Return True if key is 1..64 chars matching [a-z0-9][a-z0-9._-]*."""
    if not isinstance(key, str):
        return False
    return bool(_STATE_KEY_RE.fullmatch(key))


def validate_state_key(key: str) -> None:
    """Validate state key format; raise ValueError if invalid."""
    if not is_valid_state_key(key):
        raise ValueError(
            f"Invalid state key: {key!r}. Must be 1..64 chars matching [a-z0-9][a-z0-9._-]*"
        )


@dataclass(frozen=True, order=True)
class StateVersion:
    """Total-ordered state version (wall_ms, counter, author)."""

    wall_ms: int
    counter: int
    author: str

    def __post_init__(self) -> None:
        if isinstance(self.wall_ms, bool) or not isinstance(self.wall_ms, int) or self.wall_ms < 0:
            raise ValueError(f"wall_ms must be a non-negative int, got {self.wall_ms!r}")
        if isinstance(self.counter, bool) or not isinstance(self.counter, int) or self.counter < 0:
            raise ValueError(f"counter must be a non-negative int, got {self.counter!r}")
        if not isinstance(self.author, str) or not self.author:
            raise ValueError(f"author must be a non-empty str, got {self.author!r}")


@dataclass(frozen=True)
class StateEntry:
    """Single state register entry with version and payload."""

    version: StateVersion
    payload: bytes
    frame_bytes: bytes | None = None

    @property
    def ciphertext(self) -> bytes:
        return self.payload


@dataclass(frozen=True)
class StateStats:
    """Frozen snapshot of state channel counters."""

    published: int = 0
    sent: int = 0
    received: int = 0
    applied: int = 0
    ignored: int = 0
    rejected_skew: int = 0
    rejected_invalid: int = 0


class StateRegister:
    """In-memory key-value register with LWW merge and maximum 16 keys."""

    def __init__(self, max_keys: int = MAX_STATE_KEYS_PER_GROUP) -> None:
        self._max_keys = max_keys
        self._entries: dict[str, StateEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> StateEntry | None:
        with self._lock:
            return self._entries.get(key)

    def get_version(self, key: str) -> StateVersion | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.version if entry is not None else None

    def get_payload(self, key: str) -> bytes | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.payload if entry is not None else None

    def items(self) -> list[tuple[str, StateEntry]]:
        with self._lock:
            return list(self._entries.items())

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._entries.keys())

    def values(self) -> list[StateEntry]:
        with self._lock:
            return list(self._entries.values())

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def force_set(self, key: str, entry: StateEntry) -> bool:
        """Unconditionally set/replace entry for key, respecting key validation and max keys."""
        validate_state_key(key)
        with self._lock:
            if key not in self._entries and len(self._entries) >= self._max_keys:
                return False
            self._entries[key] = entry
            return True

    def merge(
        self,
        key: str,
        version: StateVersion,
        payload: bytes,
        frame_bytes: bytes | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """Merge a key-version-payload tuple using LWW rules.

        Returns True if the new value won and was stored, False otherwise.
        """
        validate_state_key(key)
        entry = StateEntry(version, payload, frame_bytes)
        with self._lock:
            if key not in self._entries:
                if len(self._entries) >= self._max_keys:
                    return False
                self._entries[key] = entry
                return True

            existing = self._entries[key]
            if force:
                self._entries[key] = entry
                return True

            if version > existing.version:
                self._entries[key] = entry
                return True

            return False


class HLC:
    """Hybrid Logical Clock with accepted floor and clock correction."""

    def __init__(
        self,
        device_id: str,
        wall_clock: Callable[[], float] | None = None,
        max_clock_ahead_s: int = MAX_CLOCK_AHEAD_S,
    ) -> None:
        if not isinstance(device_id, str) or not device_id:
            raise ValueError(f"device_id must be a non-empty string, got {device_id!r}")
        self.device_id = device_id
        self._wall_clock: Callable[[], float] = wall_clock if wall_clock is not None else time.time
        self._max_clock_ahead_s = max_clock_ahead_s

        self._accepted_floor: tuple[int, int] = (0, 0)
        self._last: tuple[int, int] = (0, 0)
        self._own_wall: int | None = None
        self._lock = threading.Lock()

    @property
    def accepted_floor(self) -> tuple[int, int]:
        with self._lock:
            return self._accepted_floor

    @property
    def last(self) -> tuple[int, int]:
        with self._lock:
            return self._last

    def now_ms(self) -> int:
        return int(self._wall_clock() * 1000)

    def publish(self, author: str | None = None) -> tuple[StateVersion, bool]:
        """Mint a new StateVersion for a local publish.

        Returns:
            (version, corrected): where corrected indicates the local clock moved
            backwards past its own future publish and self-authored future floor
            was dropped.
        """
        with self._lock:
            now_ms = self.now_ms()
            max_ahead_ms = self._max_clock_ahead_s * 1000
            corrected = False

            if self._own_wall is not None and self._own_wall > now_ms + max_ahead_ms:
                # Local clock moved backwards past its own future publish
                self._last = self._accepted_floor
                self._own_wall = None
                corrected = True

            w = max(now_ms, self._last[0])
            counter = (self._last[1] + 1) if w == self._last[0] else 0
            self._last = (w, counter)
            self._own_wall = w
            version = StateVersion(w, counter, author if author is not None else self.device_id)
            return (version, corrected)

    def is_skewed(self, wall_ms: int) -> bool:
        """Check if wall_ms is beyond the allowed clock ahead bound."""
        now_ms = self.now_ms()
        return wall_ms > now_ms + self._max_clock_ahead_s * 1000

    def accept(self, wall_ms: int, counter: int) -> bool:
        """Accept an incoming version after skew check, raising accepted_floor and last.

        Returns True if accepted, False if rejected due to skew.
        """
        with self._lock:
            now_ms = self.now_ms()
            if wall_ms > now_ms + self._max_clock_ahead_s * 1000:
                return False

            v_tuple = (wall_ms, counter)
            if v_tuple > self._accepted_floor:
                self._accepted_floor = v_tuple
            if self._accepted_floor > self._last:
                self._last = self._accepted_floor
            return True


@dataclass(frozen=True)
class Device:
    """Identity and priority declaration for a device."""

    device_id: str
    name: str
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True


def sort_key(d: Device) -> tuple[int, int, int, int, str]:
    """Compute the deterministic sorting key for a device.

    Tuple: (
        0 if perm is not None else 1,
        perm if perm is not None else 0,
        0 if temp_rank is not None else 1,
        temp_rank if temp_rank is not None else 0,
        device_id,
    )
    """
    return (
        0 if d.perm is not None else 1,
        d.perm if d.perm is not None else 0,
        0 if d.temp_rank is not None else 1,
        d.temp_rank if d.temp_rank is not None else 0,
        d.device_id,
    )


def order(devices: Iterable[Device]) -> list[Device]:
    """Sort devices by sort_key without mutating the input."""
    return sorted(devices, key=sort_key)


def pick_hub(
    devices: Iterable[Device], reachable_ids: Iterable[str]
) -> tuple[str | None, bool]:
    """Elect a hub among reachable devices according to sort order.

    Returns:
        (hub_id, False) for the first reachable device willing to hub.
        (hub_id, True) if reachable devices exist but all decline will_hub (forced).
        (None, False) if no reachable devices exist.
    """
    reachable_set = set(reachable_ids)
    ordered_reachable = [d for d in order(devices) if d.device_id in reachable_set]
    if not ordered_reachable:
        return (None, False)

    for d in ordered_reachable:
        if d.will_hub:
            return (d.device_id, False)

    return (ordered_reachable[0].device_id, True)


def probe_hub(
    devices: Iterable[Device],
    ping: Callable[[str, float], bool],
    timeout: float,
    self_id: str,
) -> tuple[str, bool]:
    """Probe devices top-down in sort order.

    First pass probes devices with will_hub=True.
    Second pass probes devices with will_hub=False (forced).
    If nobody responds, returns (self_id, False).
    """
    ordered = order(devices)

    for d in ordered:
        if d.will_hub:
            if ping(d.device_id, timeout):
                return (d.device_id, False)

    for d in ordered:
        if not d.will_hub:
            if ping(d.device_id, timeout):
                return (d.device_id, True)

    return (self_id, False)


class HubTracker:
    """Tracks current hub with debouncing against flapping."""

    def __init__(self, clock: Callable[[], float], debounce: float = 30.0) -> None:
        self._clock = clock
        self._debounce = debounce
        self._current_hub: str | None = None
        self._pending_hub: str | None = None
        self._pending_since: float | None = None

    @property
    def current_hub(self) -> str | None:
        return self._current_hub

    def observe(self, candidate_hub: str | None) -> None:
        """Observe a candidate hub.

        - observe(None) is ignored.
        - First non-None observation takes effect immediately.
        - Same candidate as current cancels any pending candidate.
        - Different candidate requires continuous observation for >= debounce seconds.
        """
        if candidate_hub is None:
            return

        if self._current_hub is None:
            self._current_hub = candidate_hub
            self._pending_hub = None
            self._pending_since = None
            return

        if candidate_hub == self._current_hub:
            self._pending_hub = None
            self._pending_since = None
            return

        now = self._clock()
        if candidate_hub != self._pending_hub:
            self._pending_hub = candidate_hub
            self._pending_since = now
            if (now - self._pending_since) >= self._debounce:
                self._current_hub = candidate_hub
                self._pending_hub = None
                self._pending_since = None
            return

        if self._pending_since is not None and (now - self._pending_since) >= self._debounce:
            self._current_hub = self._pending_hub
            self._pending_hub = None
            self._pending_since = None


def duplicate_temp_ranks(devices: Iterable[Device]) -> list[tuple[str, str]]:
    """Detect colliding temporary ranks among non-perm devices.

    Returns a list of (id_a, id_b) pairs where id_a < id_b,
    sorted by (temp_rank, first_id, second_id).
    Perm devices are never reported.
    """
    groups: dict[int, list[str]] = defaultdict(list)
    for d in devices:
        if d.perm is None and d.temp_rank is not None:
            groups[d.temp_rank].append(d.device_id)

    pairs: list[tuple[int, tuple[str, str]]] = []
    for rank in sorted(groups.keys()):
        dev_ids = sorted(groups[rank])
        if len(dev_ids) >= 2:
            for a, b in itertools.combinations(dev_ids, 2):
                pairs.append((rank, (a, b)))

    pairs.sort(key=lambda item: (item[0], item[1][0], item[1][1]))
    return [pair for _, pair in pairs]
