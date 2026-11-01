"""Tests for clipboard backend auto-resolution and configuration.

Every test uses fake PATH/env or mock runners; no real xclip, wl-paste, or system
clipboard is touched.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
from typing import Mapping

import pytest

from clipsync.clipboard import (
    FileClipboardBackend,
    WlClipboardBackend,
    XclipClipboardBackend,
    backend_unavailable_reason,
    resolve_clipboard_backend,
)
from clipsync.config import ConfigError, DaemonConfig, load_config
from clipsync.daemon import build_clipboard, check_clipboard_backend, main
from clipsync.protocol import text_payload
from clipsync.transport import make_pair

MINIMAL_CONFIG = (
    'device_id = "lap"\n'
    'relay_url = "ws://127.0.0.1:8765/"\n'
    'token = "tok"\n'
)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_daemon_logs(level: int = logging.INFO):
    """Capture records directly from the clipsync.daemon logger.

    Direct handler avoids caplog issues under the ROS PYTHONPATH where
    propagation may be disabled by third-party loggers.
    """
    logger = logging.getLogger("clipsync.daemon")
    handler = _ListHandler()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _make_which(available: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in available else None


# -- Resolution table branches ------------------------------------------------


def test_auto_resolution_branch1_wayland_session_with_xclip_and_display() -> None:
    which = _make_which({"xclip", "wl-paste", "wl-copy"})
    env = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
    backend, reason = resolve_clipboard_backend(env=env, which=which)
    assert backend == "xclip"
    assert reason == "wayland session"


def test_auto_resolution_branch2_wayland_without_xclip_uses_wl_clipboard() -> None:
    which = _make_which({"wl-paste", "wl-copy"})
    env = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
    backend, reason = resolve_clipboard_backend(env=env, which=which)
    assert backend == "wayland"
    assert reason == "wl-clipboard on PATH"


def test_auto_resolution_branch2_wayland_without_display_uses_wl_clipboard() -> None:
    which = _make_which({"xclip", "wl-paste", "wl-copy"})
    env = {"XDG_SESSION_TYPE": "wayland"}  # no DISPLAY
    backend, reason = resolve_clipboard_backend(env=env, which=which)
    assert backend == "wayland"
    assert reason == "wl-clipboard on PATH"


def test_auto_resolution_branch2_x11_with_wl_clipboard_uses_wl_clipboard() -> None:
    which = _make_which({"wl-paste", "wl-copy"})
    env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
    backend, reason = resolve_clipboard_backend(env=env, which=which)
    assert backend == "wayland"
    assert reason == "wl-clipboard on PATH"


def test_auto_resolution_branch3_x11_session_with_xclip_and_display() -> None:
    which = _make_which({"xclip"})
    env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
    backend, reason = resolve_clipboard_backend(env=env, which=which)
    assert backend == "xclip"
    assert reason == "x11 session"


def test_auto_resolution_branch3_unset_session_type_with_xclip_and_display() -> None:
    which = _make_which({"xclip"})
    env = {"DISPLAY": ":1"}
    backend, reason = resolve_clipboard_backend(env=env, which=which)
    assert backend == "xclip"
    assert reason == "x11 session"


def test_auto_resolution_branch4_both_missing_raises_config_error() -> None:
    which = _make_which(set())
    env: dict[str, str] = {}
    with pytest.raises(ConfigError) as exc_info:
        resolve_clipboard_backend(env=env, which=which)
    msg = str(exc_info.value)
    assert "no usable clipboard backend found" in msg
    assert "missing tools on PATH" in msg
    assert "xclip" in msg
    assert "wl-paste" in msg
    assert "wl-copy" in msg
    assert "DISPLAY is not set" in msg
    assert "install xclip or wl-clipboard" in msg


def test_auto_resolution_branch4_xclip_available_but_no_display_and_no_wl() -> None:
    which = _make_which({"xclip"})
    env = {"XDG_SESSION_TYPE": "x11"}  # DISPLAY unset
    with pytest.raises(ConfigError) as exc_info:
        resolve_clipboard_backend(env=env, which=which)
    msg = str(exc_info.value)
    assert "no usable clipboard backend found" in msg
    assert "DISPLAY is not set" in msg
    assert "wl-paste" in msg
    assert "wl-copy" in msg
    assert "install xclip or wl-clipboard" in msg


def test_auto_never_selects_file() -> None:
    # Under all combinations of tools and environments, auto resolution never produces "file"
    for which_set in [
        {"xclip", "wl-paste", "wl-copy"},
        {"wl-paste", "wl-copy"},
        {"xclip"},
    ]:
        for env in [
            {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"},
            {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
            {"DISPLAY": ":0"},
        ]:
            backend, _ = resolve_clipboard_backend(
                env=env, which=_make_which(which_set)
            )
            assert backend in ("xclip", "wayland")
            assert backend != "file"


# -- Config parsing and explicit backend flags --------------------------------


def test_config_absent_key_defaults_to_wayland_and_explicit_false(tmp_path: Path) -> None:
    path = tmp_path / "absent.toml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.clipboard_backend == "wayland"
    assert cfg.clipboard_backend_explicit is False


def test_config_explicit_auto(tmp_path: Path) -> None:
    path = tmp_path / "auto.toml"
    path.write_text(
        MINIMAL_CONFIG + 'clipboard_backend = "auto"\n', encoding="utf-8"
    )
    cfg = load_config(path)
    assert cfg.clipboard_backend == "auto"
    assert cfg.clipboard_backend_explicit is True


def test_config_explicit_wayland(tmp_path: Path) -> None:
    path = tmp_path / "wayland.toml"
    path.write_text(
        MINIMAL_CONFIG + 'clipboard_backend = "wayland"\n', encoding="utf-8"
    )
    cfg = load_config(path)
    assert cfg.clipboard_backend == "wayland"
    assert cfg.clipboard_backend_explicit is True


def test_config_explicit_xclip(tmp_path: Path) -> None:
    path = tmp_path / "xclip.toml"
    path.write_text(
        MINIMAL_CONFIG + 'clipboard_backend = "xclip"\n', encoding="utf-8"
    )
    cfg = load_config(path)
    assert cfg.clipboard_backend == "xclip"
    assert cfg.clipboard_backend_explicit is True


def test_config_explicit_file(tmp_path: Path) -> None:
    path = tmp_path / "file.toml"
    path.write_text(
        MINIMAL_CONFIG
        + 'clipboard_backend = "file"\n'
        + 'clipboard_file = "/tmp/clip.txt"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.clipboard_backend == "file"
    assert cfg.clipboard_backend_explicit is True


def test_config_auto_rejects_clipboard_file(tmp_path: Path) -> None:
    path = tmp_path / "auto_file.toml"
    path.write_text(
        MINIMAL_CONFIG
        + 'clipboard_backend = "auto"\n'
        + 'clipboard_file = "/tmp/clip.txt"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="clipboard_file"):
        load_config(path)


# -- Explicit override beats auto ---------------------------------------------


def test_explicit_wayland_beats_auto_even_on_wayland_with_xclip() -> None:
    # On a Wayland session with xclip, auto would pick xclip.
    # But explicit clipboard_backend = "wayland" must stay wayland.
    config = DaemonConfig(
        device_id="lap",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        clipboard_backend="wayland",
        clipboard_backend_explicit=True,
    )
    which = _make_which({"xclip", "wl-paste", "wl-copy"})
    env = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
    backend, reason = check_clipboard_backend(config, env=env, which=which)
    assert backend == "wayland"
    assert reason is None  # not auto-resolved


def test_explicit_wayland_fails_if_wl_clipboard_missing_even_if_xclip_present() -> None:
    config = DaemonConfig(
        device_id="lap",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        clipboard_backend="wayland",
        clipboard_backend_explicit=True,
    )
    which = _make_which({"xclip"})  # no wl-paste
    env = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
    with pytest.raises(ConfigError, match="the wl \\(Wayland\\) backend needs"):
        check_clipboard_backend(config, env=env, which=which)


def test_explicit_xclip_beats_auto() -> None:
    config = DaemonConfig(
        device_id="lap",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        clipboard_backend="xclip",
        clipboard_backend_explicit=True,
    )
    which = _make_which({"xclip", "wl-paste", "wl-copy"})
    env = {"DISPLAY": ":0"}
    backend, reason = check_clipboard_backend(config, env=env, which=which)
    assert backend == "xclip"
    assert reason is None


def test_explicit_file_beats_auto(tmp_path: Path) -> None:
    clip_file = str(tmp_path / "clip.txt")
    config = DaemonConfig(
        device_id="lap",
        relay_url="ws://127.0.0.1:8765/",
        token="tok",
        clipboard_backend="file",
        clipboard_file=clip_file,
        clipboard_backend_explicit=True,
    )
    backend, reason = check_clipboard_backend(config)
    assert backend == "file"
    assert reason is None
    built = build_clipboard(config)
    assert isinstance(built, FileClipboardBackend)


# -- backend_unavailable_reason with 'auto' -----------------------------------


def test_backend_unavailable_reason_auto() -> None:
    which_ok = _make_which({"xclip"})
    env_ok = {"DISPLAY": ":0"}
    assert backend_unavailable_reason("auto", env=env_ok, which=which_ok) is None

    which_fail = _make_which(set())
    reason = backend_unavailable_reason("auto", env={}, which=which_fail)
    assert reason is not None
    assert "no usable clipboard backend found" in reason


# -- Daemon startup, resolution and INFO log line -----------------------------


def test_daemon_startup_auto_resolves_and_logs_once_absent_key(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    monkeypatch.setattr(
        "clipsync.clipboard.shutil.which",
        _make_which({"xclip", "wl-paste", "wl-copy"}),
    )
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("DISPLAY", ":0")

    t1, _ = make_pair()
    with capture_daemon_logs() as records:
        rc = main(
            ["--config", str(path), "--once"],
            transport_factory=lambda _c: t1,
        )
    assert rc == 0
    backend_logs = [r for r in records if "clipboard backend:" in r.getMessage()]
    assert len(backend_logs) == 1
    assert (
        backend_logs[0].getMessage()
        == "clipboard backend: xclip (auto: wayland session)"
    )


def test_daemon_startup_auto_resolves_and_logs_once_explicit_auto(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        MINIMAL_CONFIG + 'clipboard_backend = "auto"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        "clipsync.clipboard.shutil.which",
        _make_which({"wl-paste", "wl-copy"}),
    )
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.delenv("DISPLAY", raising=False)

    t1, _ = make_pair()
    with capture_daemon_logs() as records:
        rc = main(
            ["--config", str(path), "--once"],
            transport_factory=lambda _c: t1,
        )
    assert rc == 0
    backend_logs = [r for r in records if "clipboard backend:" in r.getMessage()]
    assert len(backend_logs) == 1
    assert (
        backend_logs[0].getMessage()
        == "clipboard backend: wayland (auto: wl-clipboard on PATH)"
    )


def test_daemon_startup_explicit_backend_logs_once(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        MINIMAL_CONFIG + 'clipboard_backend = "xclip"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        "clipsync.clipboard.shutil.which",
        _make_which({"xclip"}),
    )
    monkeypatch.setenv("DISPLAY", ":0")

    t1, _ = make_pair()
    with capture_daemon_logs() as records:
        rc = main(
            ["--config", str(path), "--once"],
            transport_factory=lambda _c: t1,
        )
    assert rc == 0
    backend_logs = [r for r in records if "clipboard backend:" in r.getMessage()]
    assert len(backend_logs) == 1
    assert backend_logs[0].getMessage() == "clipboard backend: xclip"


def test_daemon_main_missing_all_backends_exits_2_with_clear_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    monkeypatch.setattr("clipsync.clipboard.shutil.which", lambda _name: None)
    monkeypatch.delenv("DISPLAY", raising=False)
    rc = main(["--config", str(path), "--once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert "no usable clipboard backend found" in err
    assert "xclip" in err
    assert "wl-paste" in err
    assert "install xclip or wl-clipboard" in err
