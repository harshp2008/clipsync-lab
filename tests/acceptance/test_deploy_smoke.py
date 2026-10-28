"""Frozen acceptance tests for real-process deployment (D1-D7).

Contract: the laptop deployment run.  These tests prove the packaged thing works
outside pytest's in-process nodes: real daemon processes started from real TOML
files, on loopback only, using the ``file`` clipboard backend.  They never touch
the real Wayland clipboard.

Owner: orchestrator (written and committed before the implementation; do not edit).

Coverage
--------
D1  examples/*.toml load through the real loader; CHANGE-ME placeholders fail loudly.
D2  scripts/gen_secrets.py prints valid secrets; --write is 0600 and refuses overwrite.
D3  scripts/smoke_two_processes.py exits 0, text arrives, wall time < 60 s.
D4  no child processes of the test remain after the smoke run.
D5  deploy/*.service parse as INI and ExecStart names a real entry point.
D6  the ``file`` backend is opt-in; old configs still select the Wayland backend.
D7  the previous suite still collects exactly 300 tests.
"""

from __future__ import annotations

import configparser
import importlib
import importlib.util
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

import pytest

from clipsync.clipboard import FileClipboardBackend, WlClipboardBackend
from clipsync.config import ConfigError, ensure_no_placeholders, load_config
from clipsync.daemon import build_clipboard, main as daemon_main

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"
SCRIPTS = REPO_ROOT / "scripts"
DEPLOY = REPO_ROOT / "deploy"

OLD_CONFIG = (
    'device_id = "laptop"\n'
    'relay_url = "ws://127.0.0.1:9/"\n'
    'token = "old-style-token"\n'
)


# -- helpers -----------------------------------------------------------------


def run_script(name: str, *args: str, timeout: float = 30.0, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def run_smoke(timeout: float = 70.0, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "smoke_two_processes.py")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def parse_assignment(text: str, key: str) -> str:
    match = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
    assert match is not None, f"missing {key!r} assignment in:\n{text}"
    return match.group(1)


def parse_unit(path: Path) -> tuple[configparser.ConfigParser, str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    text = path.read_text(encoding="utf-8")
    parser.read_string(text)
    return parser, text


def execstart_module(parser: configparser.ConfigParser) -> str:
    argv = shlex.split(parser["Service"]["ExecStart"])
    assert argv, "empty ExecStart"
    executable = argv[0].lstrip("-@+!")
    assert executable, "empty ExecStart executable"
    assert "-m" in argv, f"ExecStart does not use a module entry point: {argv!r}"
    module = argv[argv.index("-m") + 1]
    assert importlib.util.find_spec(module) is not None, f"no such module: {module}"
    return module


def procs_with_marker(needle: bytes) -> list[int]:
    found: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline:
            found.append(int(entry))
    return found


# -- D1: examples load; placeholders fail loudly -----------------------------


def test_d1_examples_load_and_placeholders_fail_loudly(capsys) -> None:
    files = sorted(EXAMPLES.glob("*.toml"))
    assert len(files) >= 3, f"expected example configs, found {files}"
    assert (EXAMPLES / "two-groups-on-one-box.md").is_file()

    placeholder_files = []
    for path in files:
        raw = path.read_text(encoding="utf-8")
        tomllib.loads(raw)  # syntactically valid TOML
        config = load_config(path)  # valid through the real loader
        assert config.device_id
        if "CHANGE-ME" in raw:
            placeholder_files.append(path)
            with pytest.raises(ConfigError, match="CHANGE-ME"):
                ensure_no_placeholders(config)

            # The real daemon entry point must refuse to start, loudly, and must
            # not start a Node / clipboard / transport.
            rc = daemon_main(["--config", str(path)])
            captured = capsys.readouterr()
            assert rc == 2, f"{path.name} did not fail with config error rc=2"
            assert "CHANGE-ME" in captured.err
            assert "gen_secrets" in captured.err

    assert len(placeholder_files) >= 2, placeholder_files

    relay = load_config(EXAMPLES / "relay.toml")
    assert relay.role == "relay"
    assert len(relay.serve_groups) == 2
    assert relay.e2e_key is None

    laptop = load_config(EXAMPLES / "member-laptop.toml")
    assert laptop.group_id
    assert laptop.e2e_key is not None
    assert laptop.relay is not None
    assert laptop.peers

    phone = load_config(EXAMPLES / "member-phone-placeholder.toml")
    assert phone.group_id == laptop.group_id
    phone_text = (EXAMPLES / "member-phone-placeholder.toml").read_text(encoding="utf-8")
    assert "not implemented" in phone_text.lower()


# -- D2: gen_secrets ----------------------------------------------------------


def test_d2_gen_secrets_prints_valid_secrets_and_writes_0600(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    first = run_script("gen_secrets.py")
    assert first.returncode == 0, first.stderr
    token = parse_assignment(first.stdout, "token")
    key = parse_assignment(first.stdout, "e2e_key")
    assert len(token) >= 32, f"token too short: {len(token)}"
    Fernet(key.encode("ascii"))  # raises if not a valid Fernet key

    second = run_script("gen_secrets.py")
    assert second.returncode == 0, second.stderr
    assert parse_assignment(second.stdout, "token") != token
    assert parse_assignment(second.stdout, "e2e_key") != key

    destination = tmp_path / "family-a.secrets"
    written = run_script("gen_secrets.py", "--write", str(destination))
    assert written.returncode == 0, written.stderr
    assert destination.is_file()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    content = destination.read_text(encoding="utf-8")
    Fernet(parse_assignment(content, "e2e_key").encode("ascii"))
    assert len(parse_assignment(content, "token")) >= 32
    # Secrets are not echoed when writing to a file.
    assert parse_assignment(content, "token") not in written.stdout

    refused = run_script("gen_secrets.py", "--write", str(destination))
    assert refused.returncode != 0
    assert "exist" in refused.stderr.lower()
    assert destination.read_text(encoding="utf-8") == content


# -- D3: the real two-process smoke script -----------------------------------


def test_d3_smoke_script_passes_within_budget() -> None:
    started = time.monotonic()
    result = run_smoke()
    elapsed = time.monotonic() - started

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "TEXT-ARRIVED" in result.stdout, result.stdout
    assert "FAILOVER-OK" in result.stdout, result.stdout
    assert "PASS" in result.stdout, result.stdout
    assert elapsed < 60.0, f"smoke script took {elapsed:.1f}s"


# -- D4: no stray children ----------------------------------------------------


def test_d4_no_child_processes_survive_the_smoke_run(tmp_path: Path) -> None:
    run_dir = tmp_path / f"smoke-{uuid.uuid4().hex}"
    run_dir.mkdir()
    env = os.environ.copy()
    env["TMPDIR"] = str(run_dir)

    result = run_smoke(env=env)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    needle = run_dir.name.encode("utf-8")
    deadline = time.monotonic() + 5.0
    leftovers = procs_with_marker(needle)
    while leftovers and time.monotonic() < deadline:
        time.sleep(0.05)
        leftovers = procs_with_marker(needle)
    assert not leftovers, f"stray processes after smoke run: {leftovers}"


# -- D5: systemd units --------------------------------------------------------


def test_d5_systemd_units_parse_and_name_real_entrypoints() -> None:
    relay_path = DEPLOY / "clipsync-relay.service"
    member_path = DEPLOY / "clipsync-member.service"
    assert relay_path.is_file()
    assert member_path.is_file()

    relay, relay_text = parse_unit(relay_path)
    member, member_text = parse_unit(member_path)

    for unit in (relay, member):
        assert unit.has_section("Unit")
        assert unit.has_section("Service")
        module = execstart_module(unit)
        assert callable(getattr(importlib.import_module(module), "main"))

    # The relay is a system unit with a dedicated user and safe hardening.
    relay_service = relay["Service"]
    assert relay_service.get("User", "").strip(), "relay unit needs a dedicated User="
    assert relay_service.get("NoNewPrivileges", "").lower() == "true"
    assert relay_service.get("ProtectSystem", "").strip()
    assert relay_service.get("PrivateTmp", "").lower() == "true"
    assert "dedicated" in relay_text.lower()

    # The member is a per-user unit: no User=, uses %h, runs as the login user.
    assert not member["Service"].get("User", "").strip()
    assert "%h" in member["Service"]["ExecStart"]
    assert member["Service"].get("NoNewPrivileges", "").lower() == "true"
    assert member["Service"].get("PrivateTmp", "").lower() == "true"

    # The unit module target really is the declared console-script entry point.
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["clipsync"] == "clipsync.daemon:main"


# -- D6: the file backend is opt-in ------------------------------------------


def test_d6_file_backend_is_opt_in_and_old_configs_stay_wayland(tmp_path: Path) -> None:
    old_path = tmp_path / "old.toml"
    old_path.write_text(OLD_CONFIG, encoding="utf-8")
    old = load_config(old_path)
    assert old.clipboard_backend == "wayland"
    assert isinstance(build_clipboard(old), WlClipboardBackend)

    file_path = tmp_path / "file.toml"
    file_path.write_text(
        OLD_CONFIG
        + 'clipboard_backend = "file"\n'
        + f'clipboard_file = "{tmp_path / "clip.txt"}"\n',
        encoding="utf-8",
    )
    file_config = load_config(file_path)
    assert file_config.clipboard_backend == "file"
    backend = build_clipboard(file_config)
    assert isinstance(backend, FileClipboardBackend)

    missing_file = tmp_path / "missing-file.toml"
    missing_file.write_text(
        OLD_CONFIG + 'clipboard_backend = "file"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="clipboard_file"):
        load_config(missing_file)

    unknown_backend = tmp_path / "unknown.toml"
    unknown_backend.write_text(
        OLD_CONFIG + 'clipboard_backend = "magic"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="clipboard_backend"):
        load_config(unknown_backend)


# -- D7: previous suite unchanged --------------------------------------------


def test_d7_previous_suite_still_collects_300_tests() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "--ignore=tests/acceptance/test_deploy_smoke.py",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r"(\d+) tests collected", result.stdout)
    assert match is not None, result.stdout[-2000:]
    assert int(match.group(1)) == 300, (
        f"previous suite changed: {match.group(1)} tests collected (expected 300)"
    )
