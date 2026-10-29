"""Transport adapter connecting clipsync to meshcore.Node via state channel."""

from __future__ import annotations

import collections
import threading
from typing import Any

from clipsync.transport import FrameTooLarge, Transport, TransportError
from meshcore.node import Node, NodeError

__all__ = ["MeshStateTransport"]


class MeshStateTransport:
    """Implements Transport protocol over Node's state channel API."""

    def __init__(self, node: Node, key: str = "clipboard") -> None:
        self._node: Node = node
        self._key: str = key
        self._inbox: collections.deque[bytes] = collections.deque()
        self._inbox_lock = threading.Lock()
        self._node.on_state(self._on_node_state)

    def _on_node_state(self, key: str, sender_id: str, payload: bytes) -> None:
        if key == self._key:
            with self._inbox_lock:
                self._inbox.append(payload)

    @property
    def node(self) -> Node:
        return self._node

    @property
    def connected(self) -> bool:
        """Mirror the underlying Node's connected state."""
        return self._node.connected

    def __enter__(self) -> MeshStateTransport:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()

    def connect(self) -> None:
        """Start the underlying Node."""
        self._node.start()

    def close(self) -> None:
        """Stop the underlying Node."""
        self._node.stop()

    def send(self, frame: bytes | bytearray | memoryview) -> None:
        """Publish a state frame via the Node, mapping NodeError to FrameTooLarge or TransportError."""
        try:
            self._node.publish_state(self._key, frame)
        except NodeError as exc:
            msg = str(exc)
            if "too large" in msg.lower():
                raise FrameTooLarge(msg) from exc
            raise TransportError(msg) from exc

    def recv(self) -> list[bytes]:
        """Drain received payload frames in FIFO order."""
        with self._inbox_lock:
            frames = list(self._inbox)
            self._inbox.clear()
            return frames
