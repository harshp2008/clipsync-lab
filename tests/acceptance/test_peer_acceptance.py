"""Acceptance tests for the ``clipsync-peer`` command (spec-first).

Contract: ``docs/design/interfaces.md`` § "resilience + peer run — backoff, resilient
daemon, ``clipsync-peer`` (2026-10-27)".

The peer is a second device on the relay: it uses the same config/token/E2E key and the
same protocol/codec as the daemon, and it never touches the clipboard.  All tests use a
loopback relay on an ephemeral port or an in-memory pair; no test spawns
``wl-paste``/``wl-copy`` or reads/writes the real clipboard.

Owner: orchestrator (written and committed before the implementation job; do not edit
during it).
"""

from __future__ import annotations

import io
import json
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest

from clipsync.crypto import FernetCodec
from clipsync.peer import PeerError, image_payload_from_file, listen, main as peer_main
from clipsync.protocol import (
    WIRE_VERSION,
    ClipMessage,
    ValidationError,
    decode_frame,
    encode_frame,
    image_payload,
    text_payload,
)
from clipsync.relay_server import RelayServer
from clipsync.relay_transport import RelayTransport

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "acceptance-peer-token"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x42" * 128


# -- helpers ----------------------------------------------------------------


def relay_url(port: int) -> str:
    return f"ws://127.0.0.1:{port}/"


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def write_toml(dirpath: Path, **values) -> Path:
    lines: list[str] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, (int, float)):
            encoded = repr(value)
        else:
            encoded = json.dumps(str(value))
        lines.append(f"{key} = {encoded}")
    path = dirpath / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def recv_one(transport: RelayTransport, timeout: float = 5.0) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = transport.recv()
        if frames:
            return frames[0]
        time.sleep(0.01)
    raise AssertionError("no frame received from the relay")


def peer_config(tmp_path: Path, **overrides) -> Path:
    values = {
        "device_id": "phone-cli",
        "relay_url": relay_url(free_port()),
        "token": TOKEN,
        "reconnect_delay": 0.02,
        "connect_timeout": 0.2,
    }
    values.update(overrides)
    return write_toml(tmp_path, **values)


# -- helpers ----------------------------------------------------------------


def test_image_payload_from_file_maps_mime_and_rejects_bad_input(tmp_path: Path) -> None:
    png = tmp_path / "shot.PNG"
    png.write_bytes(PNG)
    payload = image_payload_from_file(png)
    assert payload.kind == "image"
    assert payload.mime == "image/png"
    assert payload.data == PNG

    jpeg = tmp_path / "photo.jpeg"
    jpeg.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
    assert image_payload_from_file(jpeg).mime == "image/jpeg"

    unsupported = tmp_path / "note.txt"
    unsupported.write_text("not an image", encoding="utf-8")
    with pytest.raises(PeerError):
        image_payload_from_file(unsupported)

    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(PeerError):
        image_payload_from_file(empty)

    with pytest.raises(PeerError):
        image_payload_from_file(tmp_path / "missing.png")


# -- send-text / send-image over the relay ----------------------------------


def test_peer_send_text_uses_the_shared_protocol(tmp_path: Path) -> None:
    with RelayServer(token=TOKEN, port=0) as server:
        url = relay_url(server.port)
        listener = RelayTransport(url, TOKEN)
        listener.connect()
        try:
            config = peer_config(tmp_path, relay_url=url)
            assert peer_main(["--config", str(config), "send-text", "hello from peer"]) == 0
            message = decode_frame(recv_one(listener))
            assert message.origin == "phone-cli"
            assert message.wire_version == WIRE_VERSION
            assert message.payload == text_payload("hello from peer")
            assert message.seq >= 0

            assert (
                peer_main(
                    [
                        "--config",
                        str(config),
                        "--device-id",
                        "phone-second",
                        "send-text",
                        "override device",
                    ]
                )
                == 0
            )
            override = decode_frame(recv_one(listener))
            assert override.origin == "phone-second"
            assert override.payload == text_payload("override device")
        finally:
            listener.close()


def test_peer_send_image_roundtrips_bytes(tmp_path: Path) -> None:
    with RelayServer(token=TOKEN, port=0) as server:
        url = relay_url(server.port)
        listener = RelayTransport(url, TOKEN)
        listener.connect()
        try:
            image = tmp_path / "shot.png"
            image.write_bytes(PNG)
            config = peer_config(tmp_path, relay_url=url)
            assert peer_main(["--config", str(config), "send-image", str(image)]) == 0
            message = decode_frame(recv_one(listener))
            assert message.payload.kind == "image"
            assert message.payload.mime == "image/png"
            assert message.payload.data == PNG
        finally:
            listener.close()


def test_peer_send_text_encrypts_with_the_configured_e2e_key(tmp_path: Path) -> None:
    codec = FernetCodec.generate()
    with RelayServer(token=TOKEN, port=0) as server:
        url = relay_url(server.port)
        listener = RelayTransport(url, TOKEN)
        listener.connect()
        try:
            config = peer_config(
                tmp_path, relay_url=url, e2e_key=codec.key.decode("ascii")
            )
            assert peer_main(["--config", str(config), "send-text", "secret text"]) == 0
            raw = recv_one(listener)
            with pytest.raises(ValidationError):
                decode_frame(raw)
            message = decode_frame(codec.decrypt(raw))
            assert message.payload == text_payload("secret text")
        finally:
            listener.close()


def test_peer_uses_the_token_from_config(tmp_path: Path) -> None:
    with RelayServer(token=TOKEN, port=0) as server:
        config = peer_config(
            tmp_path, relay_url=relay_url(server.port), token="wrong-token"
        )
        assert peer_main(["--config", str(config), "send-text", "should not arrive"]) == 1


def test_peer_send_returns_1_when_the_relay_is_down(tmp_path: Path, capsys) -> None:
    config = peer_config(tmp_path, relay_url=relay_url(free_port()))
    assert peer_main(["--config", str(config), "send-text", "hello"]) == 1
    assert capsys.readouterr().err.strip()


# -- listen ---------------------------------------------------------------


def test_peer_listen_prints_text_json_and_saves_images(tmp_path: Path) -> None:
    from clipsync.transport import make_pair

    sender, receiver = make_pair()
    out = io.StringIO()
    out_dir = tmp_path / "received"
    stop = threading.Event()
    results: list[int] = []

    text_message = ClipMessage(
        origin="laptop", seq=7, payload=text_payload("hello from the phone")
    )
    image_message = ClipMessage(
        origin="laptop", seq=8, payload=image_payload(PNG, "image/png")
    )
    own_message = ClipMessage(
        origin="phone", seq=9, payload=text_payload("own loopback")
    )
    sender.send(b"\x00garbage-not-a-frame")
    sender.send(encode_frame(own_message))
    sender.send(encode_frame(text_message))
    sender.send(encode_frame(image_message))

    def run_listen() -> None:
        results.append(
            listen(
                receiver,
                None,
                device_id="phone",
                output_dir=out_dir,
                stop_event=stop,
                out=out,
                poll_interval=0.01,
            )
        )

    thread = threading.Thread(target=run_listen, name="peer-listen-direct")
    thread.start()
    try:
        assert wait_until(lambda: len(out.getvalue().splitlines()) >= 2)
    finally:
        stop.set()
        thread.join(5.0)

    assert not thread.is_alive()
    assert results == [2]
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines == [
        {
            "kind": "text",
            "mime": "text/plain",
            "origin": "laptop",
            "seq": 7,
            "text": "hello from the phone",
        },
        {
            "kind": "image",
            "mime": "image/png",
            "origin": "laptop",
            "seq": 8,
            "path": str(out_dir.resolve() / f"{image_message.msg_id}.png"),
            "bytes": len(PNG),
        },
    ]
    saved = Path(lines[1]["path"]).read_bytes()
    assert saved == PNG


def test_peer_listen_decrypts_with_the_configured_key(tmp_path: Path) -> None:
    from clipsync.transport import make_pair

    codec = FernetCodec.generate()
    wrong_codec = FernetCodec.generate()
    sender, receiver = make_pair()
    out = io.StringIO()
    stop = threading.Event()
    results: list[int] = []

    good = ClipMessage(origin="laptop", seq=1, payload=text_payload("encrypted hello"))
    bad = ClipMessage(origin="laptop", seq=2, payload=text_payload("wrong key"))
    sender.send(wrong_codec.encrypt(encode_frame(bad)))
    sender.send(codec.encrypt(encode_frame(good)))

    def run_listen() -> None:
        results.append(
            listen(
                receiver,
                codec,
                device_id="phone",
                output_dir=tmp_path / "received",
                stop_event=stop,
                out=out,
                poll_interval=0.01,
            )
        )

    thread = threading.Thread(target=run_listen, name="peer-listen-codec")
    thread.start()
    try:
        assert wait_until(lambda: bool(out.getvalue().strip()))
    finally:
        stop.set()
        thread.join(5.0)

    assert not thread.is_alive()
    assert results == [1]
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines == [
        {
            "kind": "text",
            "mime": "text/plain",
            "origin": "laptop",
            "seq": 1,
            "text": "encrypted hello",
        }
    ]


def test_peer_listen_cli_saves_images_over_the_relay(tmp_path: Path) -> None:
    out_dir = tmp_path / "received"
    with RelayServer(token=TOKEN, port=0) as server:
        url = relay_url(server.port)
        config = peer_config(tmp_path, relay_url=url)
        stop = threading.Event()
        results: list[int] = []
        thread = threading.Thread(
            target=lambda: results.append(
                peer_main(
                    [
                        "--config",
                        str(config),
                        "listen",
                        "--output-dir",
                        str(out_dir),
                    ],
                    stop_event=stop,
                )
            ),
            name="peer-listen-cli",
        )
        thread.start()

        sender = RelayTransport(url, TOKEN, connect_timeout=0.2)
        sender.connect()
        message = ClipMessage(
            origin="laptop", seq=4, payload=image_payload(PNG, "image/png")
        )
        frame = encode_frame(message)
        expected = out_dir / f"{message.msg_id}.png"
        try:
            deadline = time.monotonic() + 10.0
            while not expected.exists() and time.monotonic() < deadline:
                sender.send(frame)
                time.sleep(0.1)
            assert expected.exists(), "listen did not save the received image"
            assert expected.read_bytes() == PNG
        finally:
            sender.close()
            stop.set()
            thread.join(10.0)

        assert not thread.is_alive()
        assert results == [0]


def test_peer_listen_retries_when_the_relay_is_down_at_startup(tmp_path: Path) -> None:
    out_dir = tmp_path / "received"
    config = peer_config(tmp_path, relay_url=relay_url(free_port()))
    stop = threading.Event()
    results: list[int] = []
    thread = threading.Thread(
        target=lambda: results.append(
            peer_main(
                ["--config", str(config), "listen", "--output-dir", str(out_dir)],
                stop_event=stop,
            )
        ),
        name="peer-listen-retry",
    )
    thread.start()
    try:
        time.sleep(0.4)
        assert thread.is_alive(), "listen exited instead of retrying a down relay"
        assert out_dir.is_dir()
    finally:
        stop.set()
        thread.join(10.0)

    assert not thread.is_alive()
    assert results == [0]


# -- CLI errors -------------------------------------------------------------


def test_peer_cli_errors_return_2(tmp_path: Path, capsys) -> None:
    config = peer_config(tmp_path)
    base = ["--config", str(config)]

    assert peer_main(["--config", str(tmp_path / "absent.toml"), "send-text", "x"]) == 2
    assert peer_main(base + ["send-text", ""]) == 2
    assert peer_main(base + ["send-image", str(tmp_path / "missing.png")]) == 2

    not_an_image = tmp_path / "note.txt"
    not_an_image.write_text("not an image", encoding="utf-8")
    assert peer_main(base + ["send-image", str(not_an_image)]) == 2

    assert peer_main(base) == 2
    assert peer_main(base + ["frobnicate"]) == 2
    assert peer_main(base + ["--device-id", "   ", "send-text", "x"]) == 2
    assert capsys.readouterr().err.strip()


# -- packaging, version source, docs ---------------------------------------


def test_peer_console_script_and_module_entry_point() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["clipsync"] == "clipsync.daemon:main"
    assert scripts["clipsync-relay"] == "clipsync.relay_cli:main"
    assert scripts["clipsync-peer"] == "clipsync.peer:main"

    proc = subprocess.run(
        [sys.executable, "-m", "clipsync.peer", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout
    for needle in ("send-text", "send-image", "listen"):
        assert needle in proc.stdout


def test_cli_version_strings_come_from_the_package_version() -> None:
    cases = [
        ("clipsync.daemon", "clipsync"),
        ("clipsync.relay_cli", "clipsync-relay"),
        ("clipsync.peer", "clipsync-peer"),
    ]
    for module, prog in cases:
        code = (
            "import sys\n"
            "import clipsync\n"
            "clipsync.__version__ = '9.9.9'\n"
            f"import {module} as entry\n"
            "sys.exit(entry.main(['--version']))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == f"{prog} 9.9.9", (module, proc.stdout, proc.stderr)


def test_readme_documents_the_two_terminal_smoke_test() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Two-terminal smoke test" in readme
    section = readme.split("## Two-terminal smoke test", 1)[1]
    for needle in (
        "clipsync-relay",
        "clipsync --config",
        "clipsync-peer",
        "send-text",
        "send-image",
        "listen",
        "--output-dir",
        "--device-id",
    ):
        assert needle in section, needle
