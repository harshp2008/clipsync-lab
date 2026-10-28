"""Unit tests for meshcore.coordination edge cases and behaviors."""

from __future__ import annotations

import dataclasses
import pytest

from meshcore.coordination import (
    Device,
    HubTracker,
    duplicate_temp_ranks,
    order,
    pick_hub,
    probe_hub,
    sort_key,
)


class SimpleClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def test_device_immutability_and_defaults() -> None:
    d = Device("dev-1", "Test")
    assert d.perm is None
    assert d.temp_rank is None
    assert d.will_hub is True

    with pytest.raises(dataclasses.FrozenInstanceError):
        d.name = "Mutated"  # type: ignore[misc]


def test_sort_key_edge_cases() -> None:
    # Explicit None vs values
    assert sort_key(Device("d1", "A", perm=None, temp_rank=None)) == (1, 0, 1, 0, "d1")
    assert sort_key(Device("d2", "B", perm=0, temp_rank=None)) == (0, 0, 1, 0, "d2")
    assert sort_key(Device("d3", "C", perm=1, temp_rank=0)) == (0, 1, 0, 0, "d3")
    assert sort_key(Device("d4", "D", perm=None, temp_rank=0)) == (1, 0, 0, 0, "d4")
    assert sort_key(Device("d5", "E", perm=None, temp_rank=5)) == (1, 0, 0, 5, "d5")

    # Perm devices always sort before temp devices regardless of temp_rank value
    d_perm = Device("z", "Z", perm=10)
    d_temp = Device("a", "A", temp_rank=0)
    assert sort_key(d_perm) < sort_key(d_temp)


def test_order_does_not_mutate_and_handles_iterables() -> None:
    original = [Device("c", "C"), Device("a", "A"), Device("b", "B")]
    snapshot = list(original)
    ordered = order(iter(original))
    assert [d.device_id for d in ordered] == ["a", "b", "c"]
    assert original == snapshot

    # Empty iterable
    assert order([]) == []


def test_pick_hub_edge_cases() -> None:
    d1 = Device("d1", "D1", perm=1, will_hub=True)
    d2 = Device("d2", "D2", perm=2, will_hub=False)
    d3 = Device("d3", "D3", perm=3, will_hub=True)

    # Empty inputs
    assert pick_hub([], {"d1", "d2"}) == (None, False)
    assert pick_hub([d1, d2], []) == (None, False)
    assert pick_hub([d1, d2], {"unknown"}) == (None, False)

    # Reachable with mix of will_hub
    assert pick_hub([d1, d2, d3], ["d2", "d3"]) == ("d3", False)

    # All reachable decline: forced hub on first reachable in sort order
    d_forced1 = Device("f1", "F1", perm=1, will_hub=False)
    d_forced2 = Device("f2", "F2", perm=2, will_hub=False)
    assert pick_hub([d_forced2, d_forced1], ["f1", "f2"]) == ("f1", True)


def test_probe_hub_edge_cases() -> None:
    self_id = "self"

    # Empty devices
    ping_calls: list[str] = []

    def ping(device_id: str, timeout: float) -> bool:
        ping_calls.append(device_id)
        return True

    assert probe_hub([], ping, 1.5, self_id) == (self_id, False)
    assert ping_calls == []

    # Two passes: first will_hub=True, then will_hub=False
    d_will_false1 = Device("wf1", "WF1", perm=1, will_hub=False)
    d_will_true1 = Device("wt1", "WT1", perm=2, will_hub=True)
    d_will_false2 = Device("wf2", "WF2", perm=3, will_hub=False)
    devices = [d_will_false1, d_will_true1, d_will_false2]

    probed: list[tuple[str, float]] = []

    def ping_only_wf2(device_id: str, timeout: float) -> bool:
        probed.append((device_id, timeout))
        return device_id == "wf2"

    # Pass 1 pings wt1 (fails); Pass 2 pings wf1 (fails), then wf2 (wins, forced)
    res = probe_hub(devices, ping_only_wf2, 3.5, self_id)
    assert res == ("wf2", True)
    assert probed == [("wt1", 3.5), ("wf1", 3.5), ("wf2", 3.5)]

    # Nobody answers
    assert probe_hub(devices, lambda d, t: False, 1.0, self_id) == (self_id, False)


def test_hub_tracker_debounce_and_edge_cases() -> None:
    clock = SimpleClock(100.0)
    tracker = HubTracker(clock, debounce=10.0)

    # Initial state
    assert tracker.current_hub is None

    # observe(None) on uninitialized tracker
    tracker.observe(None)
    assert tracker.current_hub is None

    # First non-None observation takes effect immediately
    tracker.observe("hub-a")
    assert tracker.current_hub == "hub-a"

    # Repeated observation of same current hub does nothing
    tracker.observe("hub-a")
    assert tracker.current_hub == "hub-a"

    # New candidate observed
    clock.advance(2.0)  # 102.0
    tracker.observe("hub-b")
    assert tracker.current_hub == "hub-a"

    # observe(None) while pending does not cancel pending or change state
    clock.advance(5.0)  # 107.0
    tracker.observe(None)
    assert tracker.current_hub == "hub-a"

    # Repeated observation of same candidate before debounce does not switch
    clock.advance(2.0)  # 109.0
    tracker.observe("hub-b")
    assert tracker.current_hub == "hub-a"

    # A third candidate replaces the pending candidate and resets timer
    clock.advance(0.5)  # 109.5
    tracker.observe("hub-c")
    assert tracker.current_hub == "hub-a"

    # 9.9s later for hub-c is not enough (pending since 109.5)
    clock.advance(9.9)  # 119.4
    tracker.observe("hub-c")
    assert tracker.current_hub == "hub-a"

    # Exactly debounce elapsed (10.0s since 109.5 -> 119.5)
    clock.advance(0.1)  # 119.5
    tracker.observe("hub-c")
    assert tracker.current_hub == "hub-c"

    # Switching candidate back to current cancels pending
    clock.advance(1.0)
    tracker.observe("hub-d")
    clock.advance(5.0)
    tracker.observe("hub-c")  # cancel
    clock.advance(6.0)
    tracker.observe("hub-d")  # restarts timer from here
    clock.advance(8.0)
    tracker.observe("hub-d")
    assert tracker.current_hub == "hub-c"
    clock.advance(2.1)
    tracker.observe("hub-d")
    assert tracker.current_hub == "hub-d"


def test_duplicate_temp_ranks_three_devices_and_ordering() -> None:
    # 3 colliding devices with temp_rank 1, 2 colliding with temp_rank 0
    devices = [
        Device("c", "C", temp_rank=1),
        Device("a", "A", temp_rank=1),
        Device("b", "B", temp_rank=1),
        Device("y", "Y", temp_rank=0),
        Device("x", "X", temp_rank=0),
        Device("perm_dev", "P", perm=1, temp_rank=1),  # has perm, ignored
        Device("solo", "S", temp_rank=5),              # single device, no collision
        Device("unranked", "U"),                       # no temp_rank
    ]

    dups = duplicate_temp_ranks(devices)
    # Expected: rank 0 pairs first: ("x", "y")
    # rank 1 pairs: ("a", "b"), ("a", "c"), ("b", "c")
    assert dups == [
        ("x", "y"),
        ("a", "b"),
        ("a", "c"),
        ("b", "c"),
    ]

    # Empty list
    assert duplicate_temp_ranks([]) == []
