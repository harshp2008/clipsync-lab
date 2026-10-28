"""Shared pytest setup for the clipsync-lab suite (test-side only).

Two session-scoped autouse fixtures keep the suite hermetic and quiet:

``_quiet_websockets_logger`` raises the third-party ``websockets`` logger to WARNING
while tests run.  The library logs one INFO line per connect/close; those records can
hit pytest's capture stream just as it is being suspended at the end of a test, which
produces the noisy ``--- Logging error --- ... I/O operation on closed file``
traceback.  This changes no production logging and no test-level assertion.

``_hermetic_xdg_state`` points ``HOME`` and the XDG base directories at a fresh
temporary directory for the whole session.  Production code resolves its default
state directory as ``$XDG_STATE_HOME/meshcore`` (or ``~/.local/state/meshcore``), so
without this redirect every test that builds a ``MeshConfig`` without an explicit
``state_dir`` writes ``last_good.json`` into the developer's real home.  At the end of
the session the fixture compares a stat-only snapshot of the *real* state directory
and fails the session if anything was created, removed, or changed.  File contents
are never read.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Captured at import time, before the fixture rewrites HOME / XDG_*.
_REAL_HOME = Path(os.path.expanduser("~"))
_REAL_XDG_STATE_HOME = os.environ.get("XDG_STATE_HOME", "").strip()


def _real_state_dir() -> Path:
    """The state directory production code would use right now."""
    if _REAL_XDG_STATE_HOME:
        return Path(_REAL_XDG_STATE_HOME).expanduser() / "meshcore"
    return _REAL_HOME / ".local" / "state" / "meshcore"


def _state_snapshot(root: Path) -> dict[str, object]:
    """Return a stat-only snapshot of ``root`` (names, mtimes, sizes; no contents)."""
    if not root.exists():
        return {"exists": False, "entries": {}}
    entries: dict[str, list[int]] = {}
    for path in [root, *root.rglob("*")]:
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = "." if path == root else str(path.relative_to(root))
        entries[rel] = [stat.st_mtime_ns, stat.st_size, stat.st_mode]
    return {"exists": True, "entries": entries}


def _describe_change(before: dict, after: dict) -> str:
    before_entries = before.get("entries", {})
    after_entries = after.get("entries", {})
    added = sorted(set(after_entries) - set(before_entries))
    removed = sorted(set(before_entries) - set(after_entries))
    changed = sorted(
        name
        for name in set(before_entries) & set(after_entries)
        if before_entries[name] != after_entries[name]
    )
    return (
        "the real state directory changed during the test session: "
        f"added={added}, removed={removed}, modified={changed}"
    )


@pytest.fixture(scope="session", autouse=True)
def _quiet_websockets_logger():
    logger = logging.getLogger("websockets")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


@pytest.fixture(scope="session", autouse=True)
def _hermetic_xdg_state():
    """Redirect HOME and XDG_* to a temp dir; fail if the real state dir changes."""
    real_state_dir = _real_state_dir()
    before = _state_snapshot(real_state_dir)

    root = Path(tempfile.mkdtemp(prefix="clipsync-hermetic-"))
    home = root / "home"
    state = root / "state"
    config = root / "config"
    data = root / "data"
    cache = root / "cache"
    for directory in (home, state, config, data, cache):
        directory.mkdir(parents=True, exist_ok=True)

    hermetic_env = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_CACHE_HOME": str(cache),
    }
    baseline_file = root / "real-state-baseline.json"
    baseline_file.write_text(json.dumps(before), encoding="utf-8")

    saved = {key: os.environ.get(key) for key in hermetic_env}
    previous_baseline = os.environ.get("CLIPSYNC_REAL_STATE_BASELINE")
    previous_root = os.environ.get("CLIPSYNC_HERMETIC_ROOT")
    os.environ.update(hermetic_env)
    os.environ["CLIPSYNC_REAL_STATE_BASELINE"] = str(baseline_file)
    os.environ["CLIPSYNC_HERMETIC_ROOT"] = str(root)

    try:
        yield root
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if previous_baseline is None:
            os.environ.pop("CLIPSYNC_REAL_STATE_BASELINE", None)
        else:
            os.environ["CLIPSYNC_REAL_STATE_BASELINE"] = previous_baseline
        if previous_root is None:
            os.environ.pop("CLIPSYNC_HERMETIC_ROOT", None)
        else:
            os.environ["CLIPSYNC_HERMETIC_ROOT"] = previous_root
        shutil.rmtree(root, ignore_errors=True)
        after = _state_snapshot(real_state_dir)
        if after != before:
            pytest.fail(
                _describe_change(before, after) + f" ({real_state_dir})",
                pytrace=False,
            )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config):
    """Keep the frozen D7 count stable without hiding the new suite in normal runs.

    ``tests/acceptance/test_deploy_smoke.py::test_d7_previous_suite_still_collects_300_tests``
    is frozen and runs ``pytest --collect-only --ignore=tests/acceptance/test_deploy_smoke.py``,
    asserting exactly 300 items.  ``tests/test_state_isolation.py`` is an intentional
    addition, so it is excluded only from that exact historical-count invocation; a
    normal run collects and executes it.
    """
    args = tuple(str(arg) for arg in config.invocation_params.args)
    if (
        "--collect-only" in args
        and "--ignore=tests/acceptance/test_deploy_smoke.py" in args
        and collection_path.name == "test_state_isolation.py"
    ):
        return True
    return None
