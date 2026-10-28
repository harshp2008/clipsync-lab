"""Pure, app-agnostic coordination core for meshcore."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import itertools
from typing import Callable, Iterable


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
