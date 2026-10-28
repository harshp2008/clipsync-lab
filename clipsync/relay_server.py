"""WebSocket relay server for clipsync (compatibility wrapper around meshcore.hub).

Subclasses HubServer with strict loopback-only host validation.
"""

from __future__ import annotations

from meshcore.hub import DEFAULT_MAX_FRAME_BYTES, HubServer

# Re-exported relay maximum frame size constant
RELAY_MAX_FRAME_BYTES: int = DEFAULT_MAX_FRAME_BYTES

__all__ = [
    "RELAY_MAX_FRAME_BYTES",
    "RelayServer",
]


class RelayServer(HubServer):
    """Loopback-only WebSocket relay server subclassing HubServer."""

    def __init__(
        self,
        *,
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
        max_frame_bytes: int = RELAY_MAX_FRAME_BYTES,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError(f"host must be '127.0.0.1', got {host!r}")
        super().__init__(
            token=token,
            host=host,
            port=port,
            max_frame_bytes=max_frame_bytes,
        )
