"""Transport layer for clipsync prototype.

Carries opaque bytes frames across in-memory pairs and file-backed JSONL streams.
Transports do not inspect or parse clip messages; they carry bytes-like frames as-is.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "TransportError",
    "FrameTooLarge",
    "Transport",
    "InMemoryTransport",
    "make_pair",
    "FileTransport",
]


class TransportError(Exception):
    """Raised when sending, receiving, or transport connection fails."""


class FrameTooLarge(TransportError):
    """Raised when a frame exceeds maximum transport size cap."""


@runtime_checkable
class Transport(Protocol):
    """Protocol for clipsync transport endpoints."""

    def connect(self) -> None:
        """Connect this endpoint."""
        ...

    def close(self) -> None:
        """Close/disconnect this endpoint."""
        ...

    @property
    def connected(self) -> bool:
        """Whether this endpoint is currently connected."""
        ...

    def send(self, frame: bytes) -> None:
        """Send an opaque frame. Raises :class:`TransportError` on failure."""
        ...

    def recv(self) -> list[bytes]:
        """Drain and return available frames. Raises :class:`TransportError` on failure."""
        ...


def _coerce_frame(frame: Any) -> bytes:
    """Coerce bytes-like objects (bytes, bytearray, memoryview) to bytes."""
    if isinstance(frame, (bytes, bytearray, memoryview)):
        return bytes(frame)
    raise TransportError(f"Expected bytes-like frame, got {type(frame).__name__}")


class InMemoryTransport:
    """In-memory point-to-point transport endpoint for testing and simulation.

    Starts disconnected. Wire endpoints together using :func:`make_pair` or
    by passing/setting a peer endpoint.
    """

    def __init__(self, peer: InMemoryTransport | None = None) -> None:
        self._peer: InMemoryTransport | None = peer
        self._connected: bool = False
        self._inbox: list[bytes] = []

    @property
    def connected(self) -> bool:
        """Return True if this endpoint is connected."""
        return self._connected

    @property
    def peer(self) -> InMemoryTransport | None:
        """Return the wired peer endpoint, if any."""
        return self._peer

    def set_peer(self, peer: InMemoryTransport | None) -> None:
        """Wire or unwire the peer endpoint."""
        self._peer = peer

    def connect(self) -> None:
        """Connect this endpoint."""
        self._connected = True

    def reconnect(self) -> None:
        """Reconnect this endpoint."""
        self.connect()

    def disconnect(self) -> None:
        """Disconnect this endpoint."""
        self._connected = False

    def close(self) -> None:
        """Close this endpoint (alias for disconnect)."""
        self.disconnect()

    def send(self, frame: bytes | bytearray | memoryview) -> None:
        """Send a frame to the wired peer's inbox.

        Raises :class:`TransportError` if not connected, if the peer is
        disconnected or absent, or if the frame cannot be coerced to bytes.
        """
        frame_bytes = _coerce_frame(frame)
        if not self._connected:
            raise TransportError("Endpoint is disconnected")
        if self._peer is None:
            raise TransportError("No peer wired to this transport")
        if not self._peer._connected:
            raise TransportError("Peer endpoint is disconnected")
        self._peer._inbox.append(frame_bytes)

    def recv(self) -> list[bytes]:
        """Drain and return all queued frames in FIFO order.

        Raises :class:`TransportError` if disconnected.
        """
        if not self._connected:
            raise TransportError("Endpoint is disconnected")
        frames = list(self._inbox)
        self._inbox.clear()
        return frames


def make_pair() -> tuple[InMemoryTransport, InMemoryTransport]:
    """Create and return two wired, connected InMemoryTransport endpoints."""
    a = InMemoryTransport()
    b = InMemoryTransport(peer=a)
    a.set_peer(b)
    a.connect()
    b.connect()
    return a, b


def _validate_id(name: str, value: Any) -> str:
    """Validate device/peer identifier for FileTransport."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a str, got {type(value).__name__}")
    if len(value) == 0:
        raise ValueError(f"{name} cannot be empty")
    if len(value) > 128:
        raise ValueError(f"{name} exceeds 128 characters ({len(value)})")
    if "/" in value or "\\" in value:
        raise ValueError(f"{name} cannot contain path separators: {value!r}")
    if value in {".", ".."}:
        raise ValueError(f"{name} cannot be '.' or '..': {value!r}")
    if ".." in value:
        raise ValueError(f"{name} cannot contain '..': {value!r}")
    return value


class FileTransport:
    """File-backed transport using JSONL files in a shared directory.

    One JSONL file per direction:
      - sends append to ``<directory>/to-<peer_id>.jsonl``
      - recvs read from ``<directory>/to-<device_id>.jsonl``

    Each line is JSON ``{"frame_b64": "<base64>"}`` followed by a newline.
    Offset tracking is kept in-memory starting at 0 for a fresh instance.
    """

    def __init__(self, directory: str | Path, device_id: str, peer_id: str) -> None:
        self._device_id: str = _validate_id("device_id", device_id)
        self._peer_id: str = _validate_id("peer_id", peer_id)
        self._directory: Path = Path(directory)
        self._connected: bool = False
        self._read_offset: int = 0

    @property
    def directory(self) -> Path:
        """Directory containing the transport JSONL files."""
        return self._directory

    @property
    def device_id(self) -> str:
        """Local device ID."""
        return self._device_id

    @property
    def peer_id(self) -> str:
        """Remote peer device ID."""
        return self._peer_id

    @property
    def connected(self) -> bool:
        """Return True if this transport is connected."""
        return self._connected

    @property
    def read_offset(self) -> int:
        """Current in-memory read byte offset."""
        return self._read_offset

    def connect(self) -> None:
        """Create directory if needed and mark transport connected."""
        self._directory.mkdir(parents=True, exist_ok=True)
        self._connected = True

    def reconnect(self) -> None:
        """Reconnect this transport."""
        self.connect()

    def disconnect(self) -> None:
        """Disconnect this transport."""
        self._connected = False

    def close(self) -> None:
        """Close this transport (alias for disconnect)."""
        self.disconnect()

    def send(self, frame: bytes | bytearray | memoryview) -> None:
        """Append one frame to ``<directory>/to-<peer_id>.jsonl`` with flush and fsync."""
        if not self._connected:
            raise TransportError("Transport is disconnected")
        frame_bytes = _coerce_frame(frame)
        b64_str = base64.b64encode(frame_bytes).decode("ascii")
        line = json.dumps({"frame_b64": b64_str}, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")

        out_path = self._directory / f"to-{self._peer_id}.jsonl"
        with open(out_path, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def recv(self) -> list[bytes]:
        """Read and return new complete frames from ``<directory>/to-<device_id>.jsonl``."""
        if not self._connected:
            raise TransportError("Transport is disconnected")

        in_path = self._directory / f"to-{self._device_id}.jsonl"
        if not in_path.exists():
            return []

        try:
            with open(in_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                file_size = f.tell()
                if file_size < self._read_offset:
                    self._read_offset = 0
                f.seek(self._read_offset)
                chunk = f.read()
        except FileNotFoundError:
            return []

        if not chunk:
            return []

        last_newline = chunk.rfind(b"\n")
        if last_newline == -1:
            # Trailing partial line: leave unread and leave offset unchanged
            return []

        to_process = chunk[: last_newline + 1]
        raw_lines = to_process.split(b"\n")[:-1]

        frames: list[bytes] = []
        for raw_line in raw_lines:
            try:
                line_str = raw_line.decode("utf-8")
                doc = json.loads(line_str)
            except Exception as exc:
                raise TransportError(f"Malformed JSON in line: {exc}") from exc

            if not isinstance(doc, dict):
                raise TransportError("Malformed line: JSON root must be an object")

            if "frame_b64" not in doc or not isinstance(doc["frame_b64"], str):
                raise TransportError("Malformed line: missing or invalid 'frame_b64'")

            try:
                frame_data = base64.b64decode(doc["frame_b64"], validate=True)
            except Exception as exc:
                raise TransportError(f"Malformed base64 in line: {exc}") from exc

            frames.append(frame_data)

        self._read_offset += len(to_process)
        return frames
