"""Tests for clipsync transport module (InMemoryTransport and FileTransport)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from clipsync.transport import (
    FileTransport,
    InMemoryTransport,
    Transport,
    TransportError,
    make_pair,
)


# -- Protocol and basic compliance tests -------------------------------------


def test_protocol_conformance(tmp_path: Path) -> None:
    """Verify that both transports satisfy the runtime_checkable Transport protocol."""
    in_mem = InMemoryTransport()
    file_tr = FileTransport(tmp_path, "devA", "devB")
    assert isinstance(in_mem, Transport)
    assert isinstance(file_tr, Transport)
    assert issubclass(TransportError, Exception)


# -- InMemoryTransport tests -------------------------------------------------


def test_make_pair_basic_and_fifo() -> None:
    """Test make_pair creates connected endpoints with bidirectional FIFO delivery."""
    a, b = make_pair()
    assert a.connected is True
    assert b.connected is True

    # Send A -> B
    a.send(b"frame1")
    a.send(b"frame2")
    a.send(b"frame3")

    # Send B -> A
    b.send(b"reply1")
    b.send(b"reply2")

    # Draining recv on B
    b_frames = b.recv()
    assert b_frames == [b"frame1", b"frame2", b"frame3"]
    # Second recv drains nothing
    assert b.recv() == []

    # Draining recv on A
    a_frames = a.recv()
    assert a_frames == [b"reply1", b"reply2"]
    assert a.recv() == []


def test_in_memory_disconnected_operations() -> None:
    """Test send and recv behavior when endpoints are disconnected."""
    # Fresh standalone transport starts disconnected
    isolated = InMemoryTransport()
    assert isolated.connected is False
    with pytest.raises(TransportError):
        isolated.send(b"cannot send")
    with pytest.raises(TransportError):
        isolated.recv()

    # Even if connected, sending with no peer raises TransportError
    isolated.connect()
    assert isolated.connected is True
    with pytest.raises(TransportError):
        isolated.send(b"no peer")

    # Connected pair: test disconnecting self
    a, b = make_pair()
    a.disconnect()
    assert a.connected is False
    with pytest.raises(TransportError):
        a.send(b"disconnected self")
    with pytest.raises(TransportError):
        a.recv()

    # Reconnect a, then disconnect peer b
    a.reconnect()
    assert a.connected is True
    b.close()  # close() aliases disconnect()
    assert b.connected is False

    with pytest.raises(TransportError):
        a.send(b"disconnected peer")

    # Verify no message was queued into b during the failed send
    b.reconnect()
    assert b.recv() == []


def test_in_memory_reconnect_no_magic_duplicates() -> None:
    """Verify reconnect restores communication without duplicating queued messages."""
    a, b = make_pair()
    a.send(b"first")

    # Disconnect sender, reconnect sender, send second message
    a.disconnect()
    a.reconnect()
    a.send(b"second")

    # Receiver receives both in order
    assert b.recv() == [b"first", b"second"]
    assert b.recv() == []

    # Receiver disconnects and reconnects; empty inbox remains empty
    b.disconnect()
    b.reconnect()
    assert b.recv() == []


def test_in_memory_frame_types() -> None:
    """Test bytearray, memoryview, and rejection of non-bytes frames."""
    a, b = make_pair()

    # bytearray accepted and coerced to bytes
    a.send(bytearray(b"bytearray-data"))
    # memoryview accepted and coerced to bytes
    a.send(memoryview(b"memoryview-data"))
    # empty bytes accepted
    a.send(b"")

    frames = b.recv()
    assert frames == [b"bytearray-data", b"memoryview-data", b""]
    assert all(isinstance(f, bytes) for f in frames)

    # Non-bytes frames rejected with TransportError
    with pytest.raises(TransportError):
        a.send("string")  # type: ignore[arg-type]
    with pytest.raises(TransportError):
        a.send(12345)  # type: ignore[arg-type]
    with pytest.raises(TransportError):
        a.send(None)  # type: ignore[arg-type]
    with pytest.raises(TransportError):
        a.send(["list"])  # type: ignore[arg-type]


# -- FileTransport identifier validation tests --------------------------------


@pytest.mark.parametrize(
    "invalid_id",
    [
        "",  # empty
        "x" * 129,  # 129 characters
        "a/b",  # forward slash
        "a\\b",  # backslash
        ".",  # current dir
        "..",  # parent dir
        "foo..bar",  # contains ".."
    ],
)
def test_file_transport_invalid_device_ids(tmp_path: Path, invalid_id: str) -> None:
    """Verify invalid device_id and peer_id are rejected with ValueError."""
    with pytest.raises(ValueError):
        FileTransport(tmp_path, invalid_id, "peer1")
    with pytest.raises(ValueError):
        FileTransport(tmp_path, "dev1", invalid_id)


def test_file_transport_non_string_ids(tmp_path: Path) -> None:
    """Verify non-string device_id and peer_id raise ValueError."""
    with pytest.raises(ValueError):
        FileTransport(tmp_path, 123, "peer1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FileTransport(tmp_path, "dev1", None)  # type: ignore[arg-type]


# -- FileTransport functional tests -------------------------------------------


def test_file_transport_directory_creation(tmp_path: Path) -> None:
    """Test that connect() creates the directory including parent folders."""
    nested_dir = tmp_path / "sub" / "deep" / "dir"
    assert not nested_dir.exists()

    tr = FileTransport(nested_dir, "dev1", "dev2")
    assert tr.connected is False
    tr.connect()
    assert tr.connected is True
    assert nested_dir.is_dir()


def test_file_transport_disconnected_operations(tmp_path: Path) -> None:
    """Verify send/recv on disconnected FileTransport raise TransportError."""
    tr = FileTransport(tmp_path, "dev1", "dev2")
    assert tr.connected is False

    with pytest.raises(TransportError):
        tr.send(b"frame")
    with pytest.raises(TransportError):
        tr.recv()

    tr.connect()
    assert tr.connected is True
    tr.close()
    assert tr.connected is False

    with pytest.raises(TransportError):
        tr.send(b"frame")
    with pytest.raises(TransportError):
        tr.recv()


def test_file_transport_bidirectional_exchange(tmp_path: Path) -> None:
    """Two FileTransport endpoints in the same directory exchange frames both ways."""
    t1 = FileTransport(tmp_path, "dev1", "dev2")
    t2 = FileTransport(tmp_path, "dev2", "dev1")
    t1.connect()
    t2.connect()

    # Recv when files do not exist returns []
    assert t1.recv() == []
    assert t2.recv() == []

    # Send from dev1 to dev2
    t1.send(b"msg1")
    t1.send(bytearray(b"msg2"))
    t1.send(memoryview(b"msg3"))

    # Send from dev2 to dev1
    t2.send(b"ack1")

    # dev2 receives all 3 frames in order
    assert t2.recv() == [b"msg1", b"msg2", b"msg3"]
    # Subsequent recv is empty
    assert t2.recv() == []

    # dev1 receives ack
    assert t1.recv() == [b"ack1"]
    assert t1.recv() == []


def test_file_transport_persistence_new_instance(tmp_path: Path) -> None:
    """A new FileTransport instance starts at offset 0 and re-reads existing frames."""
    t1 = FileTransport(tmp_path, "sender", "receiver")
    t2 = FileTransport(tmp_path, "receiver", "sender")
    t1.connect()
    t2.connect()

    t1.send(b"payload-alpha")
    t1.send(b"payload-beta")

    # t2 consumes the frames
    assert t2.recv() == [b"payload-alpha", b"payload-beta"]
    assert t2.recv() == []

    # A fresh instance for receiver re-reads from offset 0
    t3 = FileTransport(tmp_path, "receiver", "sender")
    assert t3.read_offset == 0
    t3.connect()
    assert t3.recv() == [b"payload-alpha", b"payload-beta"]
    assert t3.recv() == []


def test_file_transport_reconnect_no_magic_duplicates(tmp_path: Path) -> None:
    """Reconnecting an existing FileTransport does not reset offset or duplicate frames."""
    t1 = FileTransport(tmp_path, "devA", "devB")
    t2 = FileTransport(tmp_path, "devB", "devA")
    t1.connect()
    t2.connect()

    t1.send(b"msg-1")
    assert t2.recv() == [b"msg-1"]

    # Disconnect t2 and reconnect it
    t2.disconnect()
    t2.reconnect()
    # Should not re-read msg-1
    assert t2.recv() == []

    # Subsequent send is delivered once
    t1.send(b"msg-2")
    assert t2.recv() == [b"msg-2"]
    assert t2.recv() == []


def test_file_transport_partial_line_handling(tmp_path: Path) -> None:
    """Manually append partial JSON line, recv returns none; finish line, recv returns frame."""
    receiver = FileTransport(tmp_path, "dev_recv", "dev_send")
    receiver.connect()

    inbox_file = tmp_path / "to-dev_recv.jsonl"

    # Full frame ready to write
    full_line = json.dumps({"frame_b64": base64.b64encode(b"split-frame").decode("ascii")}) + "\n"
    part1 = full_line[: len(full_line) // 2].encode("utf-8")
    part2 = full_line[len(full_line) // 2 :].encode("utf-8")

    # Write partial line without trailing newline
    with open(inbox_file, "wb") as f:
        f.write(part1)

    # receiver.recv() leaves partial line unconsumed and offset at 0
    assert receiver.recv() == []
    assert receiver.read_offset == 0

    # Complete the line with trailing newline
    with open(inbox_file, "ab") as f:
        f.write(part2)

    # receiver now consumes the completed line
    frames = receiver.recv()
    assert frames == [b"split-frame"]
    assert receiver.read_offset == len(full_line.encode("utf-8"))


def test_file_transport_complete_lines_followed_by_partial(tmp_path: Path) -> None:
    """Verify complete lines before a partial trailing line are consumed."""
    receiver = FileTransport(tmp_path, "target", "source")
    receiver.connect()
    inbox = tmp_path / "to-target.jsonl"

    line1 = (json.dumps({"frame_b64": base64.b64encode(b"frame1").decode("ascii")}) + "\n").encode("utf-8")
    line2 = (json.dumps({"frame_b64": base64.b64encode(b"frame2").decode("ascii")}) + "\n").encode("utf-8")
    full_line3 = (json.dumps({"frame_b64": base64.b64encode(b"frame3").decode("ascii")}) + "\n").encode("utf-8")
    part1 = full_line3[: len(full_line3) // 2]
    part2 = full_line3[len(full_line3) // 2 :]

    with open(inbox, "wb") as f:
        f.write(line1 + line2 + part1)

    # First recv consumes line1 and line2, leaving partial line
    assert receiver.recv() == [b"frame1", b"frame2"]
    assert receiver.read_offset == len(line1) + len(line2)

    # Still waiting for partial line
    assert receiver.recv() == []

    # Finish the partial line
    with open(inbox, "ab") as f:
        f.write(part2)

    assert receiver.recv() == [b"frame3"]
    assert receiver.read_offset == len(line1) + len(line2) + len(full_line3)


@pytest.mark.parametrize(
    "bad_content",
    [
        b"not-json\n",
        b"[1, 2, 3]\n",
        b'"just a string"\n',
        b"12345\n",
        b"{}\n",  # missing frame_b64
        b'{"frame_b64": 123}\n',  # frame_b64 not a string
        b'{"frame_b64": null}\n',  # frame_b64 is null
        b'{"frame_b64": "not_valid_base64!!!"}\n',  # invalid base64 chars
        b'{"frame_b64": "abc"}\n',  # invalid base64 length / padding
        b"\n",  # empty line
    ],
)
def test_file_transport_malformed_lines(tmp_path: Path, bad_content: bytes) -> None:
    """Verify malformed complete lines raise TransportError."""
    receiver = FileTransport(tmp_path, "devR", "devS")
    receiver.connect()

    inbox = tmp_path / "to-devR.jsonl"
    with open(inbox, "wb") as f:
        f.write(bad_content)

    with pytest.raises(TransportError):
        receiver.recv()


def test_file_transport_file_shrink_resets_offset(tmp_path: Path) -> None:
    """Verify that truncating the file below the current offset resets offset to 0."""
    t1 = FileTransport(tmp_path, "s", "r")
    t2 = FileTransport(tmp_path, "r", "s")
    t1.connect()
    t2.connect()

    t1.send(b"first")
    t1.send(b"second")
    assert t2.recv() == [b"first", b"second"]
    assert t2.read_offset > 0

    inbox = tmp_path / "to-r.jsonl"
    # Overwrite file with smaller new content
    new_line = (json.dumps({"frame_b64": base64.b64encode(b"brand-new").decode("ascii")}) + "\n").encode("utf-8")
    with open(inbox, "wb") as f:
        f.write(new_line)

    assert inbox.stat().st_size < t2.read_offset

    # recv detects file shrank, resets offset to 0, and reads the new line
    assert t2.recv() == [b"brand-new"]
    assert t2.read_offset == len(new_line)


def test_file_transport_rejects_non_bytes_frame(tmp_path: Path) -> None:
    """FileTransport.send() rejects non-bytes frames with TransportError."""
    tr = FileTransport(tmp_path, "d1", "d2")
    tr.connect()

    with pytest.raises(TransportError):
        tr.send("not bytes")  # type: ignore[arg-type]
    with pytest.raises(TransportError):
        tr.send(999)  # type: ignore[arg-type]
    with pytest.raises(TransportError):
        tr.send({"dict": "data"})  # type: ignore[arg-type]
