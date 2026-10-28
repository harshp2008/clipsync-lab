"""WebSocket relay transport client for clipsync.

Thin adapter over meshcore.link.Link satisfying clipsync.transport.Transport.
"""

from __future__ import annotations

from typing import Any

from clipsync.transport import Transport, TransportError
from meshcore.hub import RELAY_MAX_FRAME_BYTES
from meshcore.link import Link, LinkError, relay_url

__all__ = [
    "RELAY_MAX_FRAME_BYTES",
    "RelayTransport",
    "relay_url",
]


class RelayTransport(Link):
    """Synchronous transport client connected to a clipsync RelayServer."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        max_frame_bytes: int = RELAY_MAX_FRAME_BYTES,
        reconnect: bool = True,
        reconnect_delay: float = 0.2,
        connect_timeout: float = 5.0,
    ) -> None:
        super().__init__(
            url,
            token,
            hello=None,
            max_frame_bytes=max_frame_bytes,
            reconnect=reconnect,
            reconnect_delay=reconnect_delay,
            connect_timeout=connect_timeout,
            thread_name="clipsync-relay-transport",
        )

    def connect(self) -> None:
        """Connect to the relay server."""
        try:
            super().connect()
        except LinkError as exc:
            raise TransportError(str(exc)) from exc

    def send(self, frame: bytes | bytearray | memoryview) -> None:
        """Send a frame, translating LinkError to TransportError."""
        try:
            super().send(frame)
        except LinkError as exc:
            raise TransportError(str(exc)) from exc

    def recv(self) -> list[bytes]:
        """Drain queued frames, translating LinkError to TransportError."""
        try:
            return super().recv()
        except LinkError as exc:
            raise TransportError(str(exc)) from exc
