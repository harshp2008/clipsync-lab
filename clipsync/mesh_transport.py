"""Transport adapter connecting clipsync to a meshcore.node.Node."""

from __future__ import annotations

import collections
import threading
from typing import Any

from clipsync.transport import Transport, TransportError
from meshcore.node import Node, NodeError

__all__ = ["NodeTransport"]


class NodeTransport:
    """Implements clipsync.transport.Transport over a meshcore Node."""

    def __init__(self, node: Node) -> None:
        self._node: Node = node
        self._connected: bool = False
        self._inbox: collections.deque[bytes] = collections.deque()
        self._inbox_lock = threading.Lock()
        self._node.on_receive(self._on_node_receive)

    def _on_node_receive(self, sender_id: str | None, payload: bytes) -> None:
        with self._inbox_lock:
            self._inbox.append(payload)

    @property
    def connected(self) -> bool:
        """Whether the transport is connected (node has been started and not closed)."""
        return self._connected

    def __enter__(self) -> NodeTransport:
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
        """Start the underlying Node once."""
        if not self._connected:
            self._node.start()
            self._connected = True

    def close(self) -> None:
        """Stop the underlying Node."""
        if self._connected:
            self._connected = False
            self._node.stop()

    def send(self, frame: bytes | bytearray | memoryview) -> None:
        """Publish a frame via the Node, mapping NodeError to TransportError."""
        if not self._connected:
            raise TransportError("Transport is disconnected")
        try:
            self._node.publish(frame)
        except NodeError as exc:
            raise TransportError(str(exc)) from exc

    def recv(self) -> list[bytes]:
        """Drain received payload frames in FIFO order."""
        if not self._connected:
            raise TransportError("Transport is disconnected")
        with self._inbox_lock:
            frames = list(self._inbox)
            self._inbox.clear()
            return frames
