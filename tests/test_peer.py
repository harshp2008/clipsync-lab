"""Unit tests for clipsync.peer module."""

from __future__ import annotations

import io
import json
from pathlib import Path
import threading
import pytest

from clipsync.crypto import FernetCodec
from clipsync.peer import (
    IMAGE_MIME_BY_SUFFIX,
    PeerError,
    image_payload_from_file,
    listen,
    main,
    send_payload,
)
from clipsync.protocol import (
    ClipMessage,
    decode_frame,
    encode_frame,
    image_payload,
    text_payload,
)
from clipsync.transport import make_pair

SAMPLE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_image_mime_by_suffix_completeness() -> None:
    assert ".png" in IMAGE_MIME_BY_SUFFIX
    assert ".jpg" in IMAGE_MIME_BY_SUFFIX
    assert ".jpeg" in IMAGE_MIME_BY_SUFFIX
    assert ".webp" in IMAGE_MIME_BY_SUFFIX
    assert ".gif" in IMAGE_MIME_BY_SUFFIX


def test_image_payload_from_file(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(SAMPLE_PNG)
    payload = image_payload_from_file(img)
    assert payload.kind == "image"
    assert payload.mime == "image/png"
    assert payload.data == SAMPLE_PNG

    bad = tmp_path / "test.pdf"
    bad.write_bytes(b"%PDF")
    with pytest.raises(PeerError):
        image_payload_from_file(bad)


def test_send_payload_direct() -> None:
    t1, t2 = make_pair()
    try:
        payload = text_payload("peer text message")
        msg = send_payload(t1, None, "peer-1", payload, seq=42)
        assert msg.seq == 42
        assert msg.origin == "peer-1"

        frames = t2.recv()
        assert len(frames) == 1
        decoded = decode_frame(frames[0])
        assert decoded.payload == payload
    finally:
        t1.close()
        t2.close()


def test_listen_saves_image_and_emits_json(tmp_path: Path) -> None:
    t1, t2 = make_pair()
    out = io.StringIO()
    out_dir = tmp_path / "imgs"
    stop = threading.Event()

    msg = ClipMessage(origin="laptop", seq=1, payload=image_payload(SAMPLE_PNG, "image/png"))
    t1.send(encode_frame(msg))

    def run_listen() -> None:
        listen(
            t2,
            None,
            device_id="phone",
            output_dir=out_dir,
            stop_event=stop,
            out=out,
            poll_interval=0.01,
        )

    thread = threading.Thread(target=run_listen)
    thread.start()
    try:
        import time

        deadline = time.monotonic() + 3.0
        while not out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(3.0)

    assert not thread.is_alive()
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["kind"] == "image"
    assert lines[0]["origin"] == "laptop"
    assert lines[0]["bytes"] == len(SAMPLE_PNG)
    saved = Path(lines[0]["path"])
    assert saved.exists()
    assert saved.read_bytes() == SAMPLE_PNG


def test_peer_main_argument_errors(tmp_path: Path, capsys) -> None:
    # No subcommand
    cfg = tmp_path / "config.toml"
    cfg.write_text('device_id = "p"\nrelay_url = "ws://127.0.0.1:8765/"\ntoken = "t"\n')
    assert main(["--config", str(cfg)]) == 2

    # Invalid device id override
    assert main(["--config", str(cfg), "--device-id", "", "send-text", "hi"]) == 2
