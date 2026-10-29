#!/usr/bin/env python3
"""Two-process laptop smoke test: relay + two members, no real clipboard.

Starts three real ``python -m clipsync.daemon`` processes from TOML files in a
temporary directory, talking over loopback only:

* one relay (``role = "relay"``) serving one group;
* member A and member B (the same group, shared e2e key, each other's address in
  ``[[peers]]``), both using the headless ``file`` clipboard backend.

It writes text into A's clipboard file and waits (bounded, 15 s) for it to appear
in B's file, then kills the relay and checks that the two members elect a hub among
themselves and keep syncing on loopback (bounded, 25 s).  Everything is terminated
in a ``finally`` block (SIGTERM, then SIGKILL after a grace period); the temp
directory is removed.  Exit code 0 means PASS.

Stdlib only.  Never touches the real Wayland clipboard.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
FIRST_TEXT_TIMEOUT = 15.0
ELECTION_TIMEOUT = 20.0
POST_FAILOVER_TEXT_TIMEOUT = 15.0
STOP_GRACE = 5.0
POLL = 0.02

STARTED = time.monotonic()


def log(message: str) -> None:
    print(f"[{time.monotonic() - STARTED:6.2f}s] {message}", flush=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def toml_str(value: object) -> str:
    # JSON string escaping is a valid TOML basic-string for these values.
    return json.dumps(str(value))


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return ""


def wait_until(predicate, timeout: float, description: str) -> float | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return time.monotonic()
        time.sleep(POLL)
    return time.monotonic() if predicate() else None


class Child:
    def __init__(self, name: str, proc: subprocess.Popen, log_path: Path, handle) -> None:
        self.name = name
        self.proc = proc
        self.log_path = log_path
        self.handle = handle

    def tail(self, lines: int = 200) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])


def start_child(name: str, config: Path, log_dir: Path, env: dict[str, str]) -> Child:
    log_path = log_dir / f"{name}.log"
    handle = open(log_path, "wb")  # noqa: SIM115 - kept open for the child's lifetime
    proc = subprocess.Popen(
        [sys.executable, "-m", "clipsync.daemon", "--config", str(config)],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return Child(name, proc, log_path, handle)


def stop_child(child: Child, *, grace: float = STOP_GRACE) -> None:
    proc = child.proc
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + grace
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(POLL)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        child.handle.flush()
        child.handle.close()
    except (OSError, ValueError):
        pass


def child_alive(child: Child) -> bool:
    return child.proc.poll() is None


def relay_toml(group: str, token: str, port: int, state_dir: Path) -> str:
    return f"""\
device_id = {toml_str("smoke-relay")}
name = {toml_str("Smoke Relay")}
role = "relay"
bind = "127.0.0.1"
port = {port}
token = {toml_str("smoke-relay-shell-token-unused")}
state_dir = {toml_str(state_dir)}

[[serve_groups]]
id = {toml_str(group)}
token = {toml_str(token)}
"""


def member_toml(
    *,
    device_id: str,
    name: str,
    group: str,
    token: str,
    key: str,
    bind_port: int,
    relay_port: int,
    peer_id: str,
    peer_name: str,
    peer_port: int,
    clipboard_file: Path,
    state_dir: Path,
) -> str:
    return f"""\
device_id = {toml_str(device_id)}
name = {toml_str(name)}
group_id = {toml_str(group)}
token = {toml_str(token)}
e2e_key = {toml_str(key)}
state_dir = {toml_str(state_dir)}
bind = "127.0.0.1"
port = {bind_port}
poll_interval = 0.05
reconnect_delay = 0.05
connect_timeout = 3.0
# Fast failover for a bounded smoke run; production defaults are 10/3/2/30.
heartbeat = 0.5
missed_pongs = 2
probe_timeout = 0.5
debounce = 0.5
clipboard_backend = "file"
clipboard_file = {toml_str(clipboard_file)}

[relay]
address = {toml_str(f"127.0.0.1:{relay_port}")}
device_id = "smoke-relay"

[[peers]]
device_id = {toml_str(peer_id)}
name = {toml_str(peer_name)}
address = {toml_str(f"127.0.0.1:{peer_port}")}
"""


def child_env(workdir: Path) -> dict[str, str]:
    """Build the child environment with HOME and all XDG roots inside ``workdir``.

    The daemons persist ``last_good.json`` under ``$XDG_STATE_HOME/meshcore`` when a
    peer address is learned.  Pointing every HOME/XDG default at the smoke temp dir
    keeps a developer's real ``~/.local/state/meshcore`` untouched; each generated
    config also carries an explicit ``state_dir`` as a second line of defence.
    """
    env = os.environ.copy()
    prefix = str(REPO_ROOT)
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = prefix + ((os.pathsep + inherited) if inherited else "")
    env["PYTHONUNBUFFERED"] = "1"

    xdg_dirs = {
        "HOME": workdir / "home",
        "XDG_STATE_HOME": workdir / "xdg-state",
        "XDG_CONFIG_HOME": workdir / "xdg-config",
        "XDG_DATA_HOME": workdir / "xdg-data",
        "XDG_CACHE_HOME": workdir / "xdg-cache",
    }
    for path in xdg_dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    env.update({key: str(path) for key, path in xdg_dirs.items()})
    return env


def run_smoke(workdir: Path) -> None:
    env = child_env(workdir)
    state_dir = workdir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    group = "smoke-group"
    token = secrets.token_urlsafe(32)
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    run_id = secrets.token_hex(4)

    relay_port = free_port()
    member_a_port = free_port()
    member_b_port = free_port()

    clip_a = workdir / "member-a-clipboard.txt"
    clip_b = workdir / "member-b-clipboard.txt"

    (workdir / "relay.toml").write_text(
        relay_toml(group, token, relay_port, state_dir), encoding="utf-8"
    )
    (workdir / "member-a.toml").write_text(
        member_toml(
            device_id="smoke-a",
            name="Smoke A",
            group=group,
            token=token,
            key=key,
            bind_port=member_a_port,
            relay_port=relay_port,
            peer_id="smoke-b",
            peer_name="Smoke B",
            peer_port=member_b_port,
            clipboard_file=clip_a,
            state_dir=state_dir,
        ),
        encoding="utf-8",
    )
    (workdir / "member-b.toml").write_text(
        member_toml(
            device_id="smoke-b",
            name="Smoke B",
            group=group,
            token=token,
            key=key,
            bind_port=member_b_port,
            relay_port=relay_port,
            peer_id="smoke-a",
            peer_name="Smoke A",
            peer_port=member_a_port,
            clipboard_file=clip_b,
            state_dir=state_dir,
        ),
        encoding="utf-8",
    )

    children: list[Child] = []
    failure: BaseException | None = None
    try:
        relay = start_child("relay", workdir / "relay.toml", workdir, env)
        children.append(relay)
        log(f"relay starting on 127.0.0.1:{relay_port} (pid {relay.proc.pid})")
        if wait_until(
            lambda: read_text(relay.log_path).find("relay serving groups") >= 0,
            timeout=10.0,
            description="relay ready",
        ) is None:
            raise RuntimeError("relay did not report readiness within 10 s")

        member_a = start_child("member-a", workdir / "member-a.toml", workdir, env)
        children.append(member_a)
        log(f"member-a starting, clipboard file {clip_a.name} (pid {member_a.proc.pid})")
        member_b = start_child("member-b", workdir / "member-b.toml", workdir, env)
        children.append(member_b)
        log(f"member-b starting, clipboard file {clip_b.name} (pid {member_b.proc.pid})")

        # Single-copy delivery verified on the meshcore state channel: write once
        # before attach and wait bounded (15 s). No retry loop.
        first_text = f"smoke text {run_id}"
        atomic_write_text(clip_a, first_text)
        log(f"TEXT-WRITTEN member-a: {first_text!r}")
        arrived = wait_until(
            lambda: first_text in read_text(clip_b), FIRST_TEXT_TIMEOUT, "first text"
        )
        for child in children:
            if not child_alive(child):
                raise RuntimeError(
                    f"{child.name} exited early with code {child.proc.returncode}"
                )
        if arrived is None:
            raise RuntimeError(
                f"TEXT-FAILED: text did not reach member-b within {FIRST_TEXT_TIMEOUT:.0f} s"
            )
        log("TEXT-RETRY 0")
        log(f"TEXT-ARRIVED member-b after {arrived - STARTED:0.2f}s (total)")

        log("stopping the relay to check member-to-member failover")
        stop_child(relay)
        if relay.proc.poll() is None:
            raise RuntimeError("relay did not exit after SIGTERM/SIGKILL")
        log("RELAY-STOPPED")

        # Copy ONCE during the handover (without waiting for election first)
        delivered_text = f"smoke failover {run_id}"
        atomic_write_text(clip_a, delivered_text)
        log(f"TEXT-WRITTEN member-a: {delivered_text!r} (during handover)")

        failed_over = wait_until(
            lambda: delivered_text in read_text(clip_b), POST_FAILOVER_TEXT_TIMEOUT, "failover text"
        )
        for child in (member_a, member_b):
            if not child_alive(child):
                raise RuntimeError(
                    f"{child.name} exited during failover with code {child.proc.returncode}"
                )
        if failed_over is None:
            raise RuntimeError(
                f"FAILOVER-FAILED: text did not reach member-b within "
                f"{POST_FAILOVER_TEXT_TIMEOUT:.0f} s after the relay stopped"
            )
        log("FAILOVER-RETRY 0")
        log(f"FAILOVER-OK member-b after {failed_over - STARTED:0.2f}s (total)")
    except BaseException as exc:  # noqa: BLE001 - reported after cleanup
        failure = exc
    finally:
        for child in reversed(children):
            stop_child(child)
        log("all child processes stopped")

    if failure is not None:
        print(f"FAIL: {failure}", file=sys.stderr, flush=True)
        for child in children:
            print(f"--- {child.name} log (tail) ---", file=sys.stderr)
            print(child.tail(), file=sys.stderr)
        raise SystemExit(1)

    print(
        f"PASS: relay + two member daemons verified single-copy delivery on "
        f"loopback and across failover ({time.monotonic() - STARTED:0.2f}s)",
        flush=True,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="clipsync-smoke-") as tmpdir:
        run_smoke(Path(tmpdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
