"""WebSocket relay transport client for clipsync.

Runs an asyncio WebSocket client in a dedicated background thread behind the
synchronous clipsync.transport.Transport protocol. Handles connection timeouts,
reconnection, client-side size checks, thread-safe message queues, and URL
formatting.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import threading
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from clipsync.relay_server import RELAY_MAX_FRAME_BYTES
from clipsync.transport import Transport, TransportError

__all__ = [
    "RELAY_MAX_FRAME_BYTES",
    "RelayTransport",
    "relay_url",
]


def relay_url(port: int, host: str = "127.0.0.1") -> str:
    """Return the WebSocket URL for the relay server."""
    return f"ws://{host}:{port}/"


def _coerce_frame(frame: Any) -> bytes:
    """Coerce bytes-like objects (bytes, bytearray, memoryview) to bytes."""
    if isinstance(frame, (bytes, bytearray, memoryview)):
        return bytes(frame)
    raise TransportError(f"Expected bytes-like frame, got {type(frame).__name__}")


class RelayTransport:
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
        self._url: str = url
        self._token: str = token
        self._max_frame_bytes: int = max_frame_bytes
        self._reconnect: bool = reconnect
        self._reconnect_delay: float = reconnect_delay
        self._connect_timeout: float = connect_timeout

        self._connected: bool = False
        self._closed: bool = False
        self._inbox: collections.deque[bytes] = collections.deque()
        self._inbox_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws: WebSocketClientProtocol | None = None
        self._send_lock: asyncio.Lock | None = None
        self._connected_event = threading.Event()
        self._connect_error: BaseException | None = None

    @property
    def url(self) -> str:
        """The WebSocket server URL."""
        return self._url

    @property
    def connected(self) -> bool:
        """Whether the transport is currently connected to the relay."""
        ws = self._ws
        return (
            self._connected
            and ws is not None
            and getattr(ws, "open", False)
            and not self._closed
        )

    def __enter__(self) -> RelayTransport:
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
        """Connect to the relay server.

        Starts the background event loop, performs the authenticated handshake,
        and waits until connected. Raises TransportError on initial failure or timeout.
        """
        if self.connected:
            return

        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                self.close()

            self._closed = False
            self._connected_event = threading.Event()
            self._connect_error = None
            self._thread = threading.Thread(
                target=self._run_loop,
                name="clipsync-relay-transport",
                daemon=True,
            )
            self._thread.start()

        ok = self._connected_event.wait(timeout=self._connect_timeout)
        if not ok:
            self.close()
            raise TransportError(
                f"Connection timed out after {self._connect_timeout}s"
            )

        if self._connect_error is not None:
            err = self._connect_error
            self.close()
            raise TransportError(f"Connection failed: {err}") from err

    def close(self) -> None:
        """Close connection and stop reconnecting.

        Idempotent.
        """
        with self._lifecycle_lock:
            if self._closed and self._thread is None:
                return
            self._closed = True
            self._connected = False

            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._cancel_tasks)

            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join()

            self._thread = None
            self._loop = None
            self._ws = None

    def _cancel_tasks(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                if hasattr(self._ws, "transport") and self._ws.transport is not None:
                    self._ws.transport.close()
        if self._loop is not None:
            for task in asyncio.all_tasks(self._loop):
                task.cancel()

    def send(self, frame: bytes | bytearray | memoryview) -> None:
        """Send an opaque frame to the relay.

        Preserves call order. Raises TransportError if disconnected or oversized.
        """
        frame_bytes = _coerce_frame(frame)
        if len(frame_bytes) > self._max_frame_bytes:
            raise TransportError(
                f"Frame size ({len(frame_bytes)} bytes) exceeds max_frame_bytes ({self._max_frame_bytes})"
            )
        if not self.connected or self._ws is None or self._loop is None:
            raise TransportError("Transport is disconnected")

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_send(frame_bytes), self._loop
            )
            future.result(timeout=self._connect_timeout)
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(f"Send failed: {exc}") from exc

    async def _async_send(self, frame: bytes) -> None:
        assert self._send_lock is not None
        async with self._send_lock:
            ws = self._ws
            if ws is None or not getattr(ws, "open", False):
                raise TransportError("Transport is disconnected")
            await ws.send(frame)

    def recv(self) -> list[bytes]:
        """Drain and return all queued frames in FIFO order.

        Raises TransportError if disconnected.
        """
        if not self.connected:
            raise TransportError("Transport is disconnected")
        with self._inbox_lock:
            frames = list(self._inbox)
            self._inbox.clear()
            return frames

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._send_lock = asyncio.Lock()
        try:
            loop.run_until_complete(self._connection_loop())
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    async def _connection_loop(self) -> None:
        first_attempt = True
        extra_headers = {"Authorization": f"Bearer {self._token}"}

        while not self._closed:
            ws = None
            try:
                ws = await websockets.connect(
                    self._url,
                    extra_headers=extra_headers,
                    max_size=self._max_frame_bytes,
                    ping_interval=None,
                    open_timeout=self._connect_timeout,
                )
                self._ws = ws
                self._connected = True
                if first_attempt:
                    first_attempt = False
                    self._connected_event.set()

                async for message in ws:
                    if self._closed:
                        break
                    frame = (
                        message
                        if isinstance(message, (bytes, bytearray, memoryview))
                        else message.encode("utf-8")
                    )
                    with self._inbox_lock:
                        self._inbox.append(bytes(frame))

            except Exception as exc:
                if first_attempt:
                    first_attempt = False
                    self._connect_error = exc
                    self._connected_event.set()
                    break
            finally:
                self._connected = False
                self._ws = None
                if ws is not None:
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(
                            asyncio.shield(ws.close()), timeout=1.0
                        )

            if self._closed or not self._reconnect:
                break

            try:
                await asyncio.sleep(self._reconnect_delay)
            except asyncio.CancelledError:
                break
