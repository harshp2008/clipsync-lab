"""WebSocket relay server for clipsync prototype.

A byte-transparent WebSocket relay running on 127.0.0.1. Clients authenticate via
HTTP upgrade Authorization: Bearer <token>. Frames are forwarded verbatim to all
other connected clients without parsing, persistence, or replay.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import websockets
from websockets.datastructures import Headers
from websockets.server import WebSocketServerProtocol

from clipsync.protocol import MAX_IMAGE_BYTES

RELAY_MAX_FRAME_BYTES: int = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 8192

__all__ = ["RELAY_MAX_FRAME_BYTES", "RelayServer"]


class RelayServer:
    """Loopback WebSocket relay server for clipsync."""

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
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        if not isinstance(port, int) or port < 0 or port > 65535:
            raise ValueError(f"port must be an int in 0..65535, got {port!r}")
        if not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
            raise ValueError(
                f"max_frame_bytes must be a positive int, got {max_frame_bytes!r}"
            )

        self._token: str = token
        self._host: str = host
        self._port: int = port
        self._bound_port: int | None = None
        self._max_frame_bytes: int = max_frame_bytes
        self._running: bool = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None
        self._clients: set[WebSocketServerProtocol] = set()
        self._lock = threading.Lock()

    @property
    def host(self) -> str:
        """Loopback IP address."""
        return self._host

    @property
    def port(self) -> int:
        """Actual bound port number (ephemeral when port=0 was requested)."""
        if self._bound_port is not None:
            return self._bound_port
        return self._port

    @property
    def running(self) -> bool:
        """Whether the relay server is currently running and listening."""
        return self._running

    def start(self) -> None:
        """Start the relay server in a background daemon thread.

        Blocks until the server is bound and listening.
        Raises OSError if the port cannot be bound.
        """
        with self._lock:
            if self._running:
                return

            self._loop = asyncio.new_event_loop()
            started_event = threading.Event()
            start_error: list[BaseException] = []

            async def _async_start() -> None:
                try:
                    self._server = await websockets.serve(
                        self._handler,
                        self._host,
                        self._port,
                        process_request=self._process_request,
                        max_size=self._max_frame_bytes,
                    )
                    sockets = self._server.sockets
                    if sockets:
                        self._bound_port = sockets[0].getsockname()[1]
                    self._running = True
                except BaseException as exc:
                    start_error.append(exc)
                finally:
                    started_event.set()

            def _run_loop() -> None:
                assert self._loop is not None
                asyncio.set_event_loop(self._loop)
                self._loop.run_until_complete(_async_start())
                if self._running:
                    self._loop.run_forever()

            self._thread = threading.Thread(target=_run_loop, daemon=True)
            self._thread.start()

        started_event.wait()

        if start_error:
            self.stop()
            err = start_error[0]
            if isinstance(err, OSError):
                raise err
            raise OSError(f"Failed to start relay server: {err}") from err

    def stop(self) -> None:
        """Stop the relay server, closing all client connections and releasing the port.

        Idempotent.
        """
        with self._lock:
            if not self._running and self._server is None:
                return
            self._running = False

            async def _async_stop() -> None:
                clients = list(self._clients)
                self._clients.clear()
                if clients:
                    await asyncio.gather(
                        *(c.close(1001, "Server shutdown") for c in clients),
                        return_exceptions=True,
                    )
                if self._server is not None:
                    self._server.close()
                    await self._server.wait_closed()
                    self._server = None

            if self._loop is not None and self._loop.is_running():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        _async_stop(), self._loop
                    )
                    future.result(timeout=5.0)
                except Exception:
                    pass
                self._loop.call_soon_threadsafe(self._loop.stop)

            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=5.0)

            if self._loop is not None and not self._loop.is_running():
                try:
                    self._loop.close()
                except Exception:
                    pass
                self._loop = None
                self._thread = None

    def _process_request(
        self, path: str, request_headers: Headers
    ) -> tuple[int, list[tuple[str, str]], bytes] | None:
        """Authenticate client handshake using Bearer token in Authorization header."""
        auth_header = request_headers.get("Authorization")
        if auth_header != f"Bearer {self._token}":
            return (403, [("Content-Type", "text/plain")], b"Forbidden\n")
        return None

    async def _handler(self, websocket: WebSocketServerProtocol) -> None:
        """Handle an authenticated WebSocket client connection."""
        self._clients.add(websocket)
        try:
            async for message in websocket:
                # Enforce max_frame_bytes limit
                if len(message) > self._max_frame_bytes:
                    await websocket.close(1009, "Message too big")
                    break

                # Broadcast to every other currently connected client
                targets = [
                    c for c in list(self._clients)
                    if c is not websocket and not c.closed
                ]
                if targets:
                    await asyncio.gather(
                        *(c.send(message) for c in targets),
                        return_exceptions=True,
                    )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)

    def __enter__(self) -> RelayServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.stop()
