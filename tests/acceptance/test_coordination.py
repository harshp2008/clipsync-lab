"""Acceptance tests for meshcore coordination phase 1 (spec-first).

Contract: ``docs/design/coordination-v2.md`` sections 0, 1, 3, 4, 6 and the
phase-1 module contract in ``docs/orchestrator/PLAN.md`` (run 2026-10-28).

These tests are frozen before the implementation job: the job may not edit this
file. They drive the pure coordination core only -- no clipboard, no sockets, no
real clock and no sleeps. ``HubTracker`` takes an injected clock; ``probe_hub``
takes an injected ``ping``.

Interface under test (``meshcore`` is a new top-level, app-agnostic package):

* ``coordination.Device(device_id, name, perm=None, temp_rank=None,
  will_hub=True)``
* ``coordination.sort_key(d)`` -> the section 3 tuple
* ``coordination.order(devices)`` -> devices in that tuple order
* ``coordination.pick_hub(devices, reachable_ids)`` -> ``(hub_id | None,
  forced)``; no reachable device is ``(None, False)``, which the caller reads
  as "I am hub".
* ``coordination.probe_hub(devices, ping, timeout, self_id)`` -> ``(hub_id,
  forced)``; probes the order top-down with ``ping(device_id, timeout) -> bool``.
* ``coordination.HubTracker(clock)`` with ``observe(candidate_hub)`` and the
  ``current_hub`` property; only a candidate stable for 30 s takes effect.
* ``coordination.duplicate_temp_ranks(devices)`` -> list of
  ``(device_id, device_id)`` pairs with equal ``temp_rank`` and no ``perm``,
  each pair's ids sorted, the list sorted by ``(temp_rank, first id)``.
* ``messages.Hello(device_id, name, perm=None, temp_rank=None, will_hub=True)``,
  ``messages.encode_hello(hello) -> dict`` and
  ``messages.decode_hello(mapping) -> Hello``.

Owner: orchestrator (written and committed before the implementation job; do not
edit during it).
"""

from __future__ import annotations

import ast
import itertools
import random
from pathlib import Path

from meshcore.coordination import (
    Device,
    HubTracker,
    duplicate_temp_ranks,
    order,
    pick_hub,
    probe_hub,
    sort_key,
)
from meshcore.messages import Hello, decode_hello, encode_hello

REPO_ROOT = Path(__file__).resolve().parents[2]


# -- helpers ----------------------------------------------------------------


class FakeClock:
    """Deterministic clock for HubTracker; tests advance it explicitly."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def ids(devices: list[Device]) -> list[str]:
    return [d.device_id for d in devices]


def tvbox(**kwargs: object) -> Device:
    return Device("dev-tvbox", "TVBOX", **kwargs)


def iphone(**kwargs: object) -> Device:
    return Device("dev-iphone", "IPHONE", **kwargs)


def samsung(**kwargs: object) -> Device:
    return Device("dev-samsung", "SAMSUNG", **kwargs)


def laptop(**kwargs: object) -> Device:
    return Device("dev-laptop", "LAPTOP", **kwargs)


# -- 1. perms decide, unreachable hubs fall through -------------------------


def test_01_perm_order_and_reachability_fallback() -> None:
    devices = [
        tvbox(perm=0),
        iphone(perm=1),
        samsung(perm=2),
    ]
    assert order(devices) == [devices[0], devices[1], devices[2]]

    all_ids = {"dev-tvbox", "dev-iphone", "dev-samsung"}
    assert pick_hub(devices, all_ids) == ("dev-tvbox", False)
    assert pick_hub(devices, {"dev-iphone", "dev-samsung"}) == ("dev-iphone", False)
    assert pick_hub(devices, {"dev-samsung"}) == ("dev-samsung", False)
    assert pick_hub(devices, set()) == (None, False)


# -- 2. debounce: a returning old hub waits 30 s ----------------------------


def test_02_returning_hub_is_debounced_for_30s() -> None:
    clock = FakeClock()
    tracker = HubTracker(clock)

    # The first elected hub takes effect immediately (there is nothing to debounce).
    tracker.observe("dev-tvbox")
    assert tracker.current_hub == "dev-tvbox"

    # TVBOX goes away; IPHONE is the new candidate but must be stable for 30 s.
    clock.advance(5)
    tracker.observe("dev-iphone")
    clock.advance(29.9)
    tracker.observe("dev-iphone")
    assert tracker.current_hub == "dev-tvbox"
    clock.advance(0.2)
    tracker.observe("dev-iphone")
    assert tracker.current_hub == "dev-iphone"

    # Old hub returns: it stays a candidate, but nothing switches before 30 s.
    clock.advance(1)
    tracker.observe("dev-tvbox")
    clock.advance(20)
    tracker.observe("dev-tvbox")
    assert tracker.current_hub == "dev-iphone"

    # It drops out at 20 s: the pending change is discarded, nothing switches.
    tracker.observe("dev-iphone")
    clock.advance(100)
    tracker.observe("dev-iphone")
    assert tracker.current_hub == "dev-iphone"

    # Flipping back before 30 s resets the timer for the returning device.
    tracker.observe("dev-tvbox")
    clock.advance(20)
    tracker.observe("dev-iphone")
    clock.advance(5)
    tracker.observe("dev-tvbox")
    clock.advance(10)
    tracker.observe("dev-tvbox")
    assert tracker.current_hub == "dev-iphone"
    clock.advance(20.5)
    tracker.observe("dev-tvbox")
    assert tracker.current_hub == "dev-tvbox"

    # observe(None) means "no candidate right now" and is ignored.
    tracker.observe(None)
    assert tracker.current_hub == "dev-tvbox"


# -- 3. temp_rank beats device_id ------------------------------------------


def test_03_temp_rank_beats_device_id() -> None:
    # SAMSUNG's UUID sorts first, so device_id alone would make it the hub.
    devices = [
        Device("zzz-tvbox", "TVBOX", temp_rank=0),
        Device("yyy-iphone", "IPHONE", temp_rank=1),
        Device("aaa-samsung", "SAMSUNG", temp_rank=2),
        Device("bbb-laptop", "LAPTOP", temp_rank=3),
    ]
    assert ids(order(devices)) == [
        "zzz-tvbox",
        "yyy-iphone",
        "aaa-samsung",
        "bbb-laptop",
    ]
    assert pick_hub(devices, {d.device_id for d in devices}) == ("zzz-tvbox", False)


# -- 4. unranked devices order by device_id --------------------------------


def test_04_unranked_orders_by_device_id_only() -> None:
    devices = [
        Device("b", "B"),
        Device("a", "A"),
        Device("c", "C"),
    ]
    assert ids(order(devices)) == ["a", "b", "c"]
    assert pick_hub(devices, {"a", "b", "c"}) == ("a", False)

    # The section 3 sort key itself, including the "missing" flags.
    assert sort_key(Device("x", "X")) == (1, 0, 1, 0, "x")
    assert sort_key(Device("x", "X", perm=0)) == (0, 0, 1, 0, "x")
    assert sort_key(Device("x", "X", perm=5)) == (0, 5, 1, 0, "x")
    assert sort_key(Device("x", "X", temp_rank=0)) == (1, 0, 0, 0, "x")
    assert sort_key(Device("x", "X", temp_rank=7)) == (1, 0, 0, 7, "x")


# -- 5. a perm never loses to a temp rank ----------------------------------


def test_05_perm_device_beats_temp_rank_zero() -> None:
    perm_device = Device("zzz-perm", "PERM", perm=5)
    temp_device = Device("aaa-temp", "TEMP", temp_rank=0)
    assert order([temp_device, perm_device]) == [perm_device, temp_device]
    assert pick_hub([temp_device, perm_device], {"zzz-perm", "aaa-temp"}) == (
        "zzz-perm",
        False,
    )


# -- 6. two temp fragments merge without renumbering -----------------------


def test_06_fragment_merge_uses_device_id_tiebreak() -> None:
    # Phase 1 deliberately breaks a temp-rank tie by device_id. The alternative
    # from design section 12 open decision 2 -- "the larger fragment's order is
    # the reference" -- needs fragment membership and is phase 2, so it is NOT
    # implemented here.
    fragment_a = [
        Device("z-tvbox", "TVBOX", temp_rank=0),
        Device("c-iphone", "IPHONE", temp_rank=1),
    ]
    fragment_b = [
        Device("a-samsung", "SAMSUNG", temp_rank=0),
        Device("d-laptop", "LAPTOP", temp_rank=1),
    ]
    merged = fragment_a + fragment_b

    # Nobody is renumbered: the ranks stay exactly as each fragment set them.
    before = {d.device_id: (d.perm, d.temp_rank) for d in merged}
    merged_order = order(merged)
    assert {d.device_id: (d.perm, d.temp_rank) for d in merged_order} == before

    # Both rank-0 devices tie; device_id ("a-samsung" < "z-tvbox") decides.
    assert ids(merged_order) == [
        "a-samsung",
        "z-tvbox",
        "c-iphone",
        "d-laptop",
    ]
    assert [d.temp_rank for d in merged_order] == [0, 0, 1, 1]

    assert duplicate_temp_ranks(merged) == [
        ("a-samsung", "z-tvbox"),
        ("c-iphone", "d-laptop"),
    ]
    # A perm device is never reported as a duplicate temp rank.
    assert duplicate_temp_ranks([tvbox(perm=0, temp_rank=0), samsung(temp_rank=0)]) == []


# -- 7. every scenario is permutation invariant ----------------------------


def test_07_order_and_hub_are_permutation_invariant() -> None:
    perm_devices = [
        Device("dev-tvbox", "TVBOX", perm=0),
        Device("dev-iphone", "IPHONE", perm=1),
        Device("dev-samsung", "SAMSUNG", perm=2),
    ]
    temp_devices = [
        Device("zzz-tvbox", "TVBOX", temp_rank=0),
        Device("yyy-iphone", "IPHONE", temp_rank=1),
        Device("aaa-samsung", "SAMSUNG", temp_rank=2),
        Device("bbb-laptop", "LAPTOP", temp_rank=3),
    ]
    unranked = [Device("b", "B"), Device("a", "A"), Device("c", "C")]
    mixed = [Device("zzz-perm", "PERM", perm=5), Device("aaa-temp", "TEMP", temp_rank=0)]
    fragments = [
        Device("z-tvbox", "TVBOX", temp_rank=0),
        Device("c-iphone", "IPHONE", temp_rank=1),
        Device("a-samsung", "SAMSUNG", temp_rank=0),
        Device("d-laptop", "LAPTOP", temp_rank=1),
    ]
    declining = [
        Device("dev-tvbox", "TVBOX", perm=0, will_hub=False),
        Device("dev-iphone", "IPHONE", perm=1),
        Device("dev-samsung", "SAMSUNG", perm=2),
    ]

    scenarios = [
        (perm_devices, {d.device_id for d in perm_devices}),
        (temp_devices, {d.device_id for d in temp_devices}),
        (unranked, {"a", "b", "c"}),
        (mixed, {"zzz-perm", "aaa-temp"}),
        (fragments, {d.device_id for d in fragments}),
        (declining, {d.device_id for d in declining}),
    ]

    rng = random.Random(20261028)
    for devices, reachable in scenarios:
        expected_order = ids(order(devices))
        expected_hub = pick_hub(devices, reachable)
        for _ in range(50):
            shuffled = list(devices)
            rng.shuffle(shuffled)
            assert ids(order(shuffled)) == expected_order
            assert pick_hub(shuffled, reachable) == expected_hub


# -- 8. will_hub = false is skipped, then forced if everyone declines ------


def test_08_will_hub_false_is_skipped_then_forced() -> None:
    devices = [
        Device("dev-tvbox", "TVBOX", perm=0, will_hub=False),
        Device("dev-iphone", "IPHONE", perm=1),
        Device("dev-samsung", "SAMSUNG", perm=2),
    ]
    all_ids = {d.device_id for d in devices}
    assert pick_hub(devices, all_ids) == ("dev-iphone", False)

    calls: list[tuple[str, float]] = []

    def ping(device_id: str, timeout: float) -> bool:
        calls.append((device_id, timeout))
        return True

    assert probe_hub(devices, ping, 2.0, "dev-laptop") == ("dev-iphone", False)
    # The declining TVBOX is skipped rather than probed.
    assert calls == [("dev-iphone", 2.0)]

    everyone_declines = [
        Device("dev-tvbox", "TVBOX", perm=0, will_hub=False),
        Device("dev-iphone", "IPHONE", perm=1, will_hub=False),
        Device("dev-samsung", "SAMSUNG", perm=2, will_hub=False),
    ]
    assert pick_hub(everyone_declines, all_ids) == ("dev-tvbox", True)

    forced_calls: list[tuple[str, float]] = []

    def forced_ping(device_id: str, timeout: float) -> bool:
        forced_calls.append((device_id, timeout))
        return True

    assert probe_hub(everyone_declines, forced_ping, 2.0, "dev-laptop") == (
        "dev-tvbox",
        True,
    )
    assert forced_calls == [("dev-tvbox", 2.0)]


# -- 9. perm partitions: each fragment elects from its reachable members ---


def test_09_perm_partition_elects_per_fragment() -> None:
    tv = Device("dev-tvbox", "TVBOX", perm=0)
    phone = Device("dev-iphone", "IPHONE", perm=1)
    sam = Device("dev-samsung", "SAMSUNG", perm=2)
    lap = Device("dev-laptop", "LAPTOP", perm=3)

    fragment_a = [tv, phone]
    fragment_b = [sam, lap]

    assert pick_hub(fragment_a, {"dev-tvbox", "dev-iphone"}) == ("dev-tvbox", False)
    assert pick_hub(fragment_b, {"dev-samsung", "dev-laptop"}) == ("dev-samsung", False)
    assert pick_hub(fragment_a + fragment_b, {d.device_id for d in fragment_a + fragment_b}) == (
        "dev-tvbox",
        False,
    )
    # Merging fragments never rewrites a perm.
    assert [d.perm for d in fragment_a + fragment_b] == [0, 1, 2, 3]


# -- 10. probe_hub uses the injected ping ----------------------------------


def test_10_probe_hub_uses_injected_ping() -> None:
    devices = [
        Device("dev-tvbox", "TVBOX", perm=0),
        Device("dev-iphone", "IPHONE", perm=1),
        Device("dev-samsung", "SAMSUNG", perm=2),
    ]
    self_id = "dev-laptop"
    calls: list[str] = []

    def ping(device_id: str, timeout: float) -> bool:
        calls.append(device_id)
        return device_id == "dev-iphone"

    assert probe_hub(devices, ping, 2.0, self_id) == ("dev-iphone", False)
    assert calls == ["dev-tvbox", "dev-iphone"]

    assert probe_hub(devices, lambda device_id, timeout: False, 2.0, self_id) == (
        self_id,
        False,
    )


# -- 11. hello round-trips, optional fields default per field --------------


def test_11_hello_round_trips_and_defaults() -> None:
    cases = [
        Hello(
            "dev-1",
            "TV",
            perm=perm,
            temp_rank=temp_rank,
            will_hub=will_hub,
        )
        for perm, temp_rank, will_hub in itertools.product(
            [None, 3], [None, 2], [True, False]
        )
    ]
    for hello in cases:
        encoded = encode_hello(hello)
        assert isinstance(encoded, dict)
        assert decode_hello(encoded) == hello

    # A hello without the optional fields is unranked with will_hub true.
    assert decode_hello({"device_id": "dev-2", "name": "Phone"}) == Hello(
        "dev-2", "Phone", perm=None, temp_rank=None, will_hub=True
    )

    # Encode omits the absent optional ranks.
    minimal = encode_hello(Hello("dev-3", "Tablet"))
    assert "perm" not in minimal
    assert "temp_rank" not in minimal

    # Only perm, and only temp_rank: each defaults per field.
    perm_only = decode_hello(encode_hello(Hello("dev-4", "Laptop", perm=4)))
    assert perm_only.perm == 4
    assert perm_only.temp_rank is None
    assert perm_only.will_hub is True
    temp_only = decode_hello(encode_hello(Hello("dev-5", "Phone", temp_rank=1)))
    assert temp_only.perm is None
    assert temp_only.temp_rank == 1
    assert temp_only.will_hub is True

    # will_hub false survives the round trip.
    assert decode_hello(encode_hello(Hello("dev-6", "Phone", will_hub=False))).will_hub is False


# -- 13. dependency rule: meshcore never imports an app package ------------


def _import_roots(source: str, filename: str = "<test>") -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            roots.add(node.module.split(".")[0])
    return roots


def test_13_meshcore_does_not_import_app_packages() -> None:
    package_dir = REPO_ROOT / "meshcore"
    assert package_dir.is_dir(), "meshcore/ must be a top-level package"
    meshcore_files = sorted(package_dir.rglob("*.py"))
    assert meshcore_files, "meshcore/ must contain Python modules"

    # Every top-level package in this repo except meshcore itself is an app.
    app_packages = {
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_dir()
        and path.name != "meshcore"
        and (path / "__init__.py").is_file()
    }
    # Sanity-check that the scan can actually see the real app package.
    assert "clipsync" in app_packages
    assert _import_roots("import clipsync.protocol\n") == {"clipsync"}
    assert _import_roots("from clipsync import protocol\n") == {"clipsync"}

    offenders: dict[str, list[str]] = {}
    for path in meshcore_files:
        forbidden = _import_roots(
            path.read_text(encoding="utf-8"), filename=str(path)
        ) & app_packages
        if forbidden:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(forbidden)
    assert offenders == {}
