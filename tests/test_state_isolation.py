"""Hermetic state isolation: no test or script may write into the real home.

Guards two regressions that produced ``~/.local/state/meshcore/last_good.json`` on a
developer machine: (a) a node built from a config without ``state_dir`` falling back
to the real XDG state dir, and (b) helper scripts spawning daemons that inherit the
caller's HOME.  See ``tests/conftest.py`` for the session-wide redirect.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Captured at import (collection) time, before the session fixture rewrites HOME/XDG.
_REAL_HOME = Path(os.path.expanduser("~"))
_REAL_XDG_STATE_HOME = os.environ.get("XDG_STATE_HOME", "").strip()


def _real_state_dir() -> Path:
    if _REAL_XDG_STATE_HOME:
        return Path(_REAL_XDG_STATE_HOME).expanduser() / "meshcore"
    return _REAL_HOME / ".local" / "state" / "meshcore"


_REAL_STATE_DIR = _real_state_dir()


def _snapshot(root: Path) -> dict[str, object]:
    """Stat-only snapshot: names, mtimes, sizes.  Never reads file contents."""
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


_BASELINE_AT_IMPORT = _snapshot(_REAL_STATE_DIR)


_POISONED_NODE_SCRIPT = r'''
"""Start/stop a node with writes outside the allowed temp dir made fatal."""
import os
import sys
from pathlib import Path

allowed = Path(os.environ["POISON_ALLOWED"]).resolve()


def _under(path: object) -> bool:
    try:
        Path(os.fsdecode(path)).resolve().relative_to(allowed)
        return True
    except (ValueError, OSError, TypeError):
        return False


def _block(path: object) -> None:
    sys.stderr.write(f"POISON-WRITE {path!r}\n")
    sys.stderr.flush()
    os._exit(3)


def _guard(path: object) -> None:
    if isinstance(path, int):  # already-open file descriptor
        return
    if not _under(path):
        _block(path)


def _hook(event: str, args: tuple) -> None:
    if event == "open":
        path, mode, flags = args
        writing = False
        if mode:
            writing = any(ch in str(mode) for ch in "wax+")
        elif isinstance(flags, int):
            writing = bool(
                flags
                & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
            )
        if writing:
            _guard(path)
        return
    if event in (
        "os.mkdir",
        "os.rmdir",
        "os.remove",
        "os.rename",
        "os.replace",
        "os.symlink",
        "os.link",
        "os.truncate",
        "os.chmod",
        "os.utime",
    ):
        for arg in args[:2]:
            if isinstance(arg, (str, bytes)):
                _guard(arg)


sys.addaudithook(_hook)

from meshcore.addressing import Address, LastGoodStore, default_state_dir
from meshcore.config import MeshConfig
from meshcore.node import Node

state_dir = default_state_dir()
state_file = state_dir / "last_good.json"
LastGoodStore(state_file).set("probe", Address("static", "127.0.0.1", 9))

node = Node(
    MeshConfig(
        device_id="isolation",
        name="Isolation",
        token="isolation-token",
        port=0,
        discovery=False,
        heartbeat=0.05,
        probe_timeout=0.1,
    )
)
node.start()
node.stop()
print(f"POISON-OK {state_file}")
'''


def test_default_state_writes_cannot_escape_the_temp_dir(tmp_path: Path) -> None:
    poisoned_home = tmp_path / "poisoned-home"
    poisoned_home.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    script = tmp_path / "poisoned_node.py"
    script.write_text(_POISONED_NODE_SCRIPT, encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(poisoned_home),
            "XDG_STATE_HOME": str(allowed / "state"),
            "XDG_CONFIG_HOME": str(allowed / "config"),
            "XDG_DATA_HOME": str(allowed / "data"),
            "XDG_CACHE_HOME": str(allowed / "cache"),
            "POISON_ALLOWED": str(allowed),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"poisoned node wrote outside the allowed temp dir or failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "POISON-WRITE" not in result.stderr, result.stderr
    assert "POISON-OK" in result.stdout, result.stdout
    assert (allowed / "state" / "meshcore" / "last_good.json").is_file()
    assert list(poisoned_home.rglob("*")) == [], "node wrote under the poisoned HOME"


def test_real_state_dir_is_untouched_by_the_whole_suite() -> None:
    baseline_path = os.environ.get("CLIPSYNC_REAL_STATE_BASELINE")
    if baseline_path:
        before = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    else:  # standalone invocation of this file
        before = _BASELINE_AT_IMPORT

    after = _snapshot(_REAL_STATE_DIR)
    assert after == before, (
        f"the suite changed the real state directory {_REAL_STATE_DIR}: "
        f"before={before}, after={after}"
    )
