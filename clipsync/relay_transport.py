"""WebSocket relay transport for clipsync prototype.

Satisfies the Transport protocol from clipsync.transport. Backed by an asyncio
event loop in a background daemon thread, connecting to a loopback RelayServer.
Frames are opaque bytes sent and received verbatim.
"""

from __future__ import annotations

import asyncio
from collections import deque
import threading
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from clipsync.relay_server import RELAY_MAX_FRAME_BYTES
from clipsync.transport import Transport, TransportError

__all__ = ["RelayTransport", "relay_url"]


def relay_url(port: int, host: str = "127.0.0.1") -> str:
    """Return a WebSocket URL for the relay server."""
    return f"ws://{host}:{port}/"


def _coerce_frame(frame: Any) -> bytes:
    """Coerce bytes-like objects to bytes, or raise TransportError."""
    if isinstance(frame, (bytes, bytearray, memoryview)):
        return bytes(frame)
    raise TransportError(f"Expected bytes-like frame, got {type(frame).__name__}")


class RelayTransport:
    """Synchronous Transport backed by an asyncio WebSocket connection."""

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
        if not isinstance(url, str) or not url:
            raise ValueError("url must be a non-empty string")
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        if not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
            raise ValueError(
                f"max_frame_bytes must be a positive int, got {max_frame_bytes!r}"
            )
        if not isinstance(reconnect, bool):
            raise ValueError("reconnect must be a bool")
        if not isinstance(reconnect_delay, (int, float)) or reconnect_delay < 0:
            raise ValueError("reconnect_delay must be a non-negative number")
        if not isinstance(connect_timeout, (int, float)) or connect_timeout <= 0:
            raise ValueError("connect_timeout must be a positive number")

        self._url: str = url
        self._token: str = token
        self._max_frame_bytes: int = max_frame_bytes
        self._reconnect: bool = reconnect
        self._reconnect_delay: float = float(reconnect_delay)
        self._connect_timeout: float = float(connect_timeout)

        self._ws: WebSocketClientProtocol | None = None
        self._connected_flag: bool = False
        self._closing: bool = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._supervisor_task: asyncio.Task[None] | None = None

        self._inbox: deque[bytes] = deque()
        self._inbox_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

    @property
    def url(self) -> str:
        """The WebSocket endpoint URL."""
        return self._url

    @property
    def connected(self) -> bool:
        """Whether the transport has an active, live WebSocket connection."""
        with self._state_lock:
            if self._closing or not self._connected_flag:
                return False
            ws = self._ws
            return ws is not None and not ws.closed

    def connect(self) -> None:
        """Connect to the relay server within connect_timeout.

        Raises TransportError if the first connection cannot be established
        (including authentication refusal or timeout).
        """
        if self.connected:
            return

        with self._lifecycle_lock:
            self._closing = False

            if self._loop is None or not self._loop.is_running():
                self._loop = asyncio.new_event_loop()
                loop_ready = threading.Event()

                def _run_loop() -> None:
                    assert self._loop is not None
                    asyncio.set_event_loop(self._loop)
                    loop_ready.set()
                    self._loop.run_forever()

                self._thread = threading.Thread(target=_run_loop, daemon=True)
                self._thread.start()
                loop_ready.wait()

            async def _initial_connect() -> WebSocketClientProtocol:
                headers = [("Authorization", f"Bearer {self._token}")]
                return await websockets.connect(
                    self._url,
                    extra_headers=headers,
                    max_size=self._max_frame_bytes,
                    open_timeout=self._connect_timeout,
                )

            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(
                _initial_connect(), self._loop
            )

            try:
                ws = future.result(timeout=self._connect_timeout + 1.0)
                with self._state_lock:
                    self._ws = ws
                    self._connected_flag = True
                self._loop.call_soon_threadsafe(self._schedule_supervisor)
            except Exception as exc:
                self.close()
                raise TransportError(
                    f"Failed to connect to {self._url}: {exc}"
                ) from exc

    def _schedule_supervisor(self) -> None:
        """Schedule the connection supervisor task in the background loop."""
        if (
            self._loop is not None
            and self._loop.is_running()
            and not self._closing
        ):
            self._supervisor_task = self._loop.create_task(self._supervisor())

    async def _supervisor(self) -> None:
        """Background task handling incoming frames and automatic reconnects."""
        while not self._closing:
            ws = self._ws
            if ws is None:
                if not self._reconnect or self._closing:
                    break
                await self._reconnect_loop()
                continue

            try:
                msg = await ws.recv()
                if isinstance(msg, str):
                    frame = msg.encode("utf-8")
                else:
                    frame = bytes(msg)
                with self._inbox_lock:
                    self._inbox.append(frame)
            except websockets.exceptions.ConnectionClosed:
                with self._state_lock:
                    self._connected_flag = False
                    self._ws = None
                if not self._reconnect or self._closing:
                    break
                await self._reconnect_loop()
            except Exception:
                with self._state_lock:
                    self._connected_flag = False
                    self._ws = None
                if not self._reconnect or self._closing:
                    break
                await self._reconnect_loop()

    async def _reconnect_loop(self) -> None:
        """Retry connection until successful or closed."""
        headers = [("Authorization", f"Bearer {self._token}")]
        while not self._closing:
            try:
                await asyncio.sleep(self._reconnect_delay)
            except asyncio.CancelledError:
                break
            if self._closing:
                break
            try:
                ws = await websockets.connect(
                    self._url,
                    extra_headers=headers,
                    max_size=self._max_frame_bytes,
                    open_timeout=self._connect_timeout,
                )
                with self._state_lock:
                    self._ws = ws
                    self._connected_flag = True
                return
            except Exception:
                continue

    def send(self, frame: bytes) -> None:
        """Send an opaque frame.

        Raises TransportError if not connected or frame size exceeds max_frame_bytes.
        Preserves call order.
        """
        if not self.connected:
            raise TransportError("Transport is not connected")

        frame_bytes = _coerce_frame(frame)
        if len(frame_bytes) > self._max_frame_bytes:
            raise TransportError(
                f"Frame size ({len(frame_bytes)} bytes) exceeds max_frame_bytes ({self._max_frame_bytes})"
            )

        async def _async_send() -> None:
            with self._state_lock:
                ws = self._ws
            if ws is None or ws.closed:
                raise TransportError("Transport is not connected")
            try:
                await ws.send(frame_bytes)
            except websockets.exceptions.ConnectionClosed as exc:
                raise TransportError(
                    f"Connection closed during send: {exc}"
                ) from exc
            except Exception as exc:
                raise TransportError(f"Send failed: {exc}") from exc

        if self._loop is None or not self._loop.is_running():
            raise TransportError("Event loop is not running")

        try:
            future = asyncio.run_coroutine_threadsafe(
                _async_send(), self._loop
            )
            future.result(timeout=self._connect_timeout)
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(f"Send error: {exc}") from exc

    def recv(self) -> list[bytes]:
        """Drain and return queued frames in FIFO order.

        Non-blocking. Raises TransportError if not connected.
        """
        if not self.connected:
            raise TransportError("Transport is not connected")

        with self._inbox_lock:
            frames = list(self._inbox)
            self._inbox.clear()
            return frames

    def close(self) -> None:
        """Stop reconnecting and close the transport.

        Idempotent.
        """
        with self._lifecycle_lock:
            if self._closing:
                return
            self._closing = True

            with self._state_lock:
                self._connected_flag = False
                ws = self._ws
                self._ws = None

            if self._loop is not None and self._loop.is_running():
                async def _async_close() -> None:
                    if (
                        self._supervisor_task is not None
                        and not self._supervisor_task.done()
                    ):
                        self._supervisor_task.cancel()
                    if ws is not None and not ws.closed:
                        try:
                            await ws.close()
                        except Exception:
                            pass

                try:
                    future = asyncio.run_coroutine_threadsafe(
                        _async_close(), self._loop
                    )
                    future.result(timeout=2.0)
                except Exception:
                    pass

                self._loop.call_soon_threadsafe(self._loop.stop)

            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=3.0)

            if self._loop is not None and not self._loop.is_running():
                try:
                    self._loop.close()
                except Exception:
                    pass
                self._loop = None
                self._thread = None
                self._supervisor_task = None

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
