"""Tests for the opt-in xclip (X11/XWayland) clipboard backend.

Every test drives :class:`XclipClipboardBackend` with :class:`FakeXclipRunner`; no
real ``xclip``, ``wl-paste`` or system clipboard is touched.
"""

from __future__ import annotations

import subprocess

import pytest

from clipsync.clipboard import (
    ClipboardError,
    XclipClipboardBackend,
    backend_unavailable_reason,
    choose_xclip_target,
    xclip_unavailable_reason,
)
from clipsync.config import ConfigError, DaemonConfig, load_config
from clipsync.daemon import build_clipboard, main
from clipsync.engine import SyncEngine
from clipsync.protocol import (
    MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
    PayloadTooLarge,
    ClipMessage,
    encode_frame,
    image_payload,
    text_payload,
)
from clipsync.transport import make_pair

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MINIMAL_CONFIG = (
    'device_id = "lap"\n'
    'relay_url = "ws://127.0.0.1:8765/"\n'
    'token = "tok"\n'
)


class FakeXclipRunner:
    """Emulates the subset of xclip behaviour clipsync uses, with fault injection."""

    def __init__(self) -> None:
        self.selection: dict[str, bytes] = {}
        self.calls: list[tuple[list[str], bytes | None, float | None]] = []
        self.read_error: BaseException | None = None
        self.write_error: BaseException | None = None
        self.fail_reads = 0
        self.fail_writes = 0
        self.targets_rc = 0
        self.targets_stderr = b""

    # -- fake clipboard state -----------------------------------------------
    def set_text(self, text: str, target: str = "UTF8_STRING") -> None:
        self.selection = {target: text.encode("utf-8")}

    def set_image(self, data: bytes, target: str = "image/png") -> None:
        self.selection = {target: data}

    def clear(self) -> None:
        self.selection = {}

    # -- CommandRunner ------------------------------------------------------
    def run(
        self,
        argv: list[str],
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        self.calls.append((list(argv), stdin, timeout))
        assert argv[0] == "xclip", argv
        assert argv[1:4] == ["-selection", "clipboard", argv[3]], argv
        if "-i" in argv:
            return self._write(argv, stdin)
        return self._read(argv)

    def _write(self, argv: list[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
        if self.fail_writes > 0:
            self.fail_writes -= 1
            assert self.write_error is not None
            raise self.write_error
        target = argv[argv.index("-t") + 1]
        assert stdin is not None
        self.selection = {target: stdin}
        return 0, b"", b""

    def _read(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        if self.fail_reads > 0:
            self.fail_reads -= 1
            assert self.read_error is not None
            raise self.read_error
        target = argv[argv.index("-t") + 1]
        if target == "TARGETS":
            if self.targets_rc != 0:
                return self.targets_rc, b"", self.targets_stderr
            if not self.selection:
                return 1, b"", b"Error: no selection owner"
            listing = ["TARGETS", "TIMESTAMP", *self.selection]
            return 0, ("\n".join(listing) + "\n").encode(), b""
        if target not in self.selection:
            return 1, b"", b"Error: target " + target.encode() + b" not available"
        return 0, self.selection[target], b""


def runner_with_text(text: str, target: str = "UTF8_STRING") -> FakeXclipRunner:
    runner = FakeXclipRunner()
    runner.set_text(text, target)
    return runner


# -- command builders --------------------------------------------------------
def test_command_builders() -> None:
    assert XclipClipboardBackend.build_list_targets_command() == [
        "xclip", "-selection", "clipboard", "-o", "-t", "TARGETS",
    ]
    assert XclipClipboardBackend.build_read_command("UTF8_STRING") == [
        "xclip", "-selection", "clipboard", "-o", "-t", "UTF8_STRING",
    ]
    assert XclipClipboardBackend.build_write_command("image/png") == [
        "xclip", "-selection", "clipboard", "-i", "-t", "image/png",
    ]


def test_constructor_never_runs_a_command() -> None:
    runner = FakeXclipRunner()
    XclipClipboardBackend(runner=runner)
    assert runner.calls == []


def test_write_target_mapping() -> None:
    assert XclipClipboardBackend.write_target_for("text/plain") == "UTF8_STRING"
    assert XclipClipboardBackend.write_target_for("text/html") == "text/html"
    assert XclipClipboardBackend.write_target_for("text/uri-list") == "text/uri-list"
    assert XclipClipboardBackend.write_target_for("image/png") == "image/png"
    with pytest.raises(ClipboardError):
        XclipClipboardBackend.write_target_for("application/octet-stream")


# -- target parsing ----------------------------------------------------------
@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        (["TARGETS", "TIMESTAMP", "UTF8_STRING", "STRING"], "UTF8_STRING"),
        (["TARGETS", "STRING", "text/plain;charset=utf-8"], "STRING"),
        (["text/plain;charset=utf-8", "text/html"], "text/plain;charset=utf-8"),
        (["TARGETS", "text/html"], "text/html"),
        (["text/plain"], "text/plain"),
        (["image/png", "text/html", "UTF8_STRING"], "image/png"),
        (["image/jpeg", "image/png"], "image/png"),
        (["image/gif"], "image/gif"),
        (["TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS"], None),
        ([], None),
        (["", "  "], None),
    ],
)
def test_choose_xclip_target(targets: list[str], expected: str | None) -> None:
    assert choose_xclip_target(targets) == expected


def test_choose_xclip_target_keeps_atom_spelling() -> None:
    # X target atoms are case-sensitive; UTF8_STRING must not be lower-cased.
    assert choose_xclip_target(["utf8_string"]) == "utf8_string"


# -- reads -------------------------------------------------------------------
def test_read_empty_clipboard_returns_none() -> None:
    runner = FakeXclipRunner()
    assert XclipClipboardBackend(runner=runner).read() is None
    assert runner.calls[0][0][-1] == "TARGETS"


def test_read_targets_rc_nonzero_is_empty_not_error() -> None:
    runner = FakeXclipRunner()
    runner.targets_rc = 1
    runner.targets_stderr = b"Error: no selection owner"
    assert XclipClipboardBackend(runner=runner).read() is None


def test_read_target_not_available_is_empty_not_error() -> None:
    runner = FakeXclipRunner()
    runner.targets_rc = 1
    runner.targets_stderr = b"Error: target TARGETS not available"
    assert XclipClipboardBackend(runner=runner).read() is None


def test_read_only_unsupported_targets_is_empty() -> None:
    runner = FakeXclipRunner()
    runner.selection = {"TARGETS": b"", "TIMESTAMP": b""}
    assert XclipClipboardBackend(runner=runner).read() is None


def test_read_text() -> None:
    runner = runner_with_text("hello")
    payload = XclipClipboardBackend(runner=runner).read()
    assert payload == text_payload("hello")
    assert [call[0][-1] for call in runner.calls] == ["TARGETS", "UTF8_STRING"]
    assert [call[2] for call in runner.calls] == [2.0, 2.0]


def test_read_text_non_ascii_and_multiline() -> None:
    text = "héllo wörld\nsecond line\n日本語\n🙂"
    payload = XclipClipboardBackend(runner=runner_with_text(text)).read()
    assert payload == text_payload(text)


def test_read_string_target_maps_to_text_plain() -> None:
    payload = XclipClipboardBackend(runner=runner_with_text("plain", "STRING")).read()
    assert payload == text_payload("plain")


def test_read_text_near_cap_and_above_cap() -> None:
    near = "a" * (MAX_TEXT_BYTES - 1)
    assert XclipClipboardBackend(runner=runner_with_text(near)).read() == text_payload(near)

    above = "a" * (MAX_TEXT_BYTES + 1)
    with pytest.raises(PayloadTooLarge):
        XclipClipboardBackend(runner=runner_with_text(above)).read()


def test_read_image_and_size_cap() -> None:
    runner = FakeXclipRunner()
    runner.set_image(PNG)
    payload = XclipClipboardBackend(runner=runner).read()
    assert payload == image_payload(PNG, "image/png")

    big = FakeXclipRunner()
    big.set_image(b"x" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(PayloadTooLarge):
        XclipClipboardBackend(runner=big).read()


def test_read_invalid_utf8_is_clipboard_error() -> None:
    runner = FakeXclipRunner()
    runner.selection = {"UTF8_STRING": b"\xff\xfe\x00"}
    with pytest.raises(ClipboardError, match="UTF-8"):
        XclipClipboardBackend(runner=runner).read()


def test_read_timeout_becomes_clipboard_error() -> None:
    runner = FakeXclipRunner()
    runner.read_error = subprocess.TimeoutExpired("xclip", 2.0)
    runner.fail_reads = 1
    with pytest.raises(ClipboardError, match="timed out"):
        XclipClipboardBackend(runner=runner).read()


def test_read_missing_binary_becomes_clipboard_error() -> None:
    runner = FakeXclipRunner()
    runner.read_error = FileNotFoundError(2, "No such file or directory")
    runner.fail_reads = 1
    with pytest.raises(ClipboardError, match="not installed"):
        XclipClipboardBackend(runner=runner).read()


def test_read_oserror_becomes_clipboard_error() -> None:
    runner = FakeXclipRunner()
    runner.read_error = OSError(13, "X server is gone")
    runner.fail_reads = 1
    with pytest.raises(ClipboardError, match="cannot run xclip"):
        XclipClipboardBackend(runner=runner).read()


def test_read_nonzero_target_exit_is_clipboard_error() -> None:
    class Refusing(FakeXclipRunner):
        def _read(self, argv):
            if argv[-1] == "TARGETS":
                return 0, b"TARGETS\nUTF8_STRING\n", b""
            return 3, b"", b"Error: selection owner crashed"

    with pytest.raises(ClipboardError, match="exited 3"):
        XclipClipboardBackend(runner=Refusing()).read()


# -- writes ------------------------------------------------------------------
def test_write_text_uses_utf8_string_target_and_pipes_stdin() -> None:
    runner = FakeXclipRunner()
    backend = XclipClipboardBackend(runner=runner)
    backend.write(text_payload("héllo"))
    argv, stdin, timeout = runner.calls[0]
    assert argv == ["xclip", "-selection", "clipboard", "-i", "-t", "UTF8_STRING"]
    assert stdin == "héllo".encode("utf-8")
    assert timeout == 3.0


def test_write_image_pipes_raw_bytes() -> None:
    runner = FakeXclipRunner()
    XclipClipboardBackend(runner=runner).write(image_payload(PNG, "image/png"))
    argv, stdin, _timeout = runner.calls[0]
    assert argv[-1] == "image/png"
    assert stdin == PNG


def test_write_timeout_becomes_clipboard_error() -> None:
    runner = FakeXclipRunner()
    runner.write_error = subprocess.TimeoutExpired("xclip", 3.0)
    runner.fail_writes = 1
    with pytest.raises(ClipboardError, match="timed out"):
        XclipClipboardBackend(runner=runner).write(text_payload("x"))


def test_write_nonzero_exit_is_clipboard_error() -> None:
    class Refusing(FakeXclipRunner):
        def _write(self, argv, stdin):
            return 4, b"", b"Error: unbind failed"

    with pytest.raises(ClipboardError, match="failed"):
        XclipClipboardBackend(runner=Refusing()).write(text_payload("x"))


def test_write_then_read_back_echo() -> None:
    runner = FakeXclipRunner()
    backend = XclipClipboardBackend(runner=runner)
    payload = text_payload("same text")
    backend.write(payload)
    assert backend.read() == payload


# -- engine echo guard with the real backend ---------------------------------
def test_engine_does_not_republish_our_own_xclip_write() -> None:
    runner = FakeXclipRunner()
    clip = XclipClipboardBackend(runner=runner)
    ta, tb = make_pair()
    engine = SyncEngine("lap", clip, ta)
    tb.send(encode_frame(ClipMessage(origin="phone", seq=1, payload=text_payload("from phone"))))

    stats = engine.poll_once()
    assert stats.applied == 1
    assert stats.clipboard_errors == 0
    # The xclip selection now offers exactly what we wrote.
    assert runner.selection == {"UTF8_STRING": b"from phone"}

    follow_up = engine.poll_once()
    assert follow_up.sent == 0
    assert follow_up.clipboard_errors == 0
    assert engine.last_clipboard_error is None


# -- config + startup validation --------------------------------------------
def test_load_config_accepts_xclip(tmp_path) -> None:
    path = tmp_path / "xclip.toml"
    path.write_text(MINIMAL_CONFIG + 'clipboard_backend = "xclip"\n', encoding="utf-8")
    config = load_config(path)
    assert config.clipboard_backend == "xclip"
    assert isinstance(build_clipboard(config), XclipClipboardBackend)


def test_load_config_still_rejects_unknown_backend(tmp_path) -> None:
    path = tmp_path / "bogus.toml"
    path.write_text(MINIMAL_CONFIG + 'clipboard_backend = "magic"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="clipboard_backend"):
        load_config(path)


def test_xclip_rejects_clipboard_file(tmp_path) -> None:
    path = tmp_path / "xclip-file.toml"
    path.write_text(
        MINIMAL_CONFIG
        + 'clipboard_backend = "xclip"\n'
        + 'clipboard_file = "/tmp/clip.txt"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="clipboard_file"):
        load_config(path)


def test_build_clipboard_default_stays_wayland() -> None:
    config = DaemonConfig(device_id="lap", relay_url="ws://127.0.0.1:8765/", token="tok")
    from clipsync.clipboard import WlClipboardBackend

    assert isinstance(build_clipboard(config), WlClipboardBackend)


def test_xclip_unavailable_reasons() -> None:
    assert xclip_unavailable_reason(which=lambda _n: None, env={}) is not None
    assert "xclip" in xclip_unavailable_reason(which=lambda _n: None, env={})
    missing_display = xclip_unavailable_reason(
        which=lambda n: "/usr/bin/xclip" if n == "xclip" else None, env={}
    )
    assert missing_display is not None and "DISPLAY" in missing_display
    ok = xclip_unavailable_reason(
        which=lambda n: "/usr/bin/xclip" if n == "xclip" else None, env={"DISPLAY": ":0"}
    )
    assert ok is None


def test_backend_unavailable_reason_wayland_names_missing_binary() -> None:
    reason = backend_unavailable_reason("wayland", which=lambda _n: None, env={})
    assert reason is not None
    assert "wl-paste" in reason
    assert backend_unavailable_reason("file", which=lambda _n: None, env={}) is None


def test_main_exits_2_when_xclip_is_not_installed(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "xclip.toml"
    path.write_text(MINIMAL_CONFIG + 'clipboard_backend = "xclip"\n', encoding="utf-8")
    monkeypatch.setattr("clipsync.clipboard.shutil.which", lambda _name: None)
    rc = main(["--config", str(path), "--once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert "xclip" in err


def test_main_exits_2_when_display_is_unset(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "xclip.toml"
    path.write_text(MINIMAL_CONFIG + 'clipboard_backend = "xclip"\n', encoding="utf-8")
    monkeypatch.setattr(
        "clipsync.clipboard.shutil.which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )
    monkeypatch.delenv("DISPLAY", raising=False)
    rc = main(["--config", str(path), "--once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "DISPLAY" in err


def test_main_exits_2_when_wl_clipboard_is_not_installed(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "wayland.toml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    monkeypatch.setattr("clipsync.clipboard.shutil.which", lambda _name: None)
    rc = main(["--config", str(path), "--once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "wl-paste" in err
