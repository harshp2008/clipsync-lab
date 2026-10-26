"""WebSocket relay server for clipsync.

Runs an asyncio WebSocket server in a dedicated background thread behind a
synchronous lifecycle API (start, stop, context manager). Loopback only.
Acts as an opaque byte pipe forwarding frames verbatim to all other
authenticated clients without inspecting or parsing payloads.
"""

from __future__ import annotations

import asyncio
import contextlib
from http import HTTPStatus
import threading
from typing import Any

import websockets
from websockets.server import WebSocketServer

from clipsync.protocol import MAX_IMAGE_BYTES

# Base64 expansion of MAX_IMAGE_BYTES plus JSON envelope slack
RELAY_MAX_FRAME_BYTES: int = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 8192

__all__ = [
    "RELAY_MAX_FRAME_BYTES",
    "RelayServer",
]


class RelayServer:
    """Synchronous wrapper around a background asyncio WebSocket relay server."""

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
        if not isinstance(token, str) or len(token) == 0:
            raise ValueError("token must be a non-empty string")
        if not isinstance(port, int) or port < 0 or port > 65535:
            raise ValueError(f"port must be an int between 0 and 65535, got {port!r}")
        if not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
            raise ValueError(
                f"max_frame_bytes must be a positive int, got {max_frame_bytes!r}"
            )

        self._token: str = token
        self._host: str = host
        self._port: int = port
        self._max_frame_bytes: int = max_frame_bytes

        self._bound_port: int = 0
        self._running: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: WebSocketServer | None = None
        self._stop_event: asyncio.Event | None = None
        self._clients: dict[Any, asyncio.Queue[bytes | str]] = {}
        self._lock = threading.Lock()
        self._started_event = threading.Event()
        self._start_error: BaseException | None = None

    @property
    def host(self) -> str:
        """Loopback host address."""
        return self._host

    @property
    def port(self) -> int:
        """Actual bound port, or initial port if not yet listening."""
        return self._bound_port if self._bound_port != 0 else self._port

    @property
    def running(self) -> bool:
        """Whether the server is running and listening."""
        return self._running

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

    def start(self) -> None:
        """Start listening in a dedicated background thread.

        Blocks until the socket is listening or raises OSError on bind failure.
        """
        with self._lock:
            if self._running:
                return
            self._started_event = threading.Event()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._run_loop, name="clipsync-relay-server", daemon=True
            )
            self._thread.start()

        self._started_event.wait()
        if self._start_error is not None:
            if self._thread is not None and self._thread.is_alive():
                self._thread.join()
            err = self._start_error
            self._thread = None
            self._loop = None
            self._server = None
            self._running = False
            if isinstance(err, OSError):
                raise err
            raise OSError(str(err)) from err

    def stop(self) -> None:
        """Stop the server, disconnect clients, release port, and join thread.

        Idempotent.
        """
        with self._lock:
            if not self._running and self._thread is None:
                return
            self._running = False

            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._signal_stop)

            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join()

            self._thread = None
            self._loop = None
            self._server = None
            self._stop_event = None
            self._clients.clear()

    def _signal_stop(self) -> None:
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    async def _serve(self) -> None:
        async def process_request(
            path: str, request_headers: Any
        ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes] | None:
            auth = request_headers.get("Authorization")
            if auth != f"Bearer {self._token}":
                return (HTTPStatus.FORBIDDEN, [], b"")
            return None

        try:
            self._server = await websockets.serve(
                self._handler,
                host=self._host,
                port=self._port,
                process_request=process_request,
                max_size=self._max_frame_bytes,
                ping_interval=None,
            )
            if self._server.sockets:
                for sock in self._server.sockets:
                    self._bound_port = sock.getsockname()[1]
                    break
            self._running = True
            self._stop_event = asyncio.Event()
            self._started_event.set()
        except BaseException as exc:
            self._start_error = exc
            self._started_event.set()
            return

        await self._stop_event.wait()

        # Close all accepted client connections promptly
        close_tasks = []
        for ws in list(self._clients.keys()):
            close_tasks.append(asyncio.create_task(ws.close(1001, "Server shutdown")))
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

        self._server.close()
        await self._server.wait_closed()
        self._running = False

    async def _client_sender(
        self, ws: Any, queue: asyncio.Queue[bytes | str]
    ) -> None:
        try:
            while True:
                msg = await queue.get()
                try:
                    await ws.send(msg)
                except Exception:
                    break
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _handler(self, ws: Any, *args: Any) -> None:
        queue: asyncio.Queue[bytes | str] = asyncio.Queue()
        self._clients[ws] = queue
        sender_task = asyncio.create_task(self._client_sender(ws, queue))
        try:
            async for message in ws:
                msg_len = (
                    len(message)
                    if isinstance(message, (bytes, bytearray, memoryview))
                    else len(message.encode("utf-8"))
                )
                if msg_len > self._max_frame_bytes:
                    await ws.close(1009, "Message too big")
                    break

                for other_ws, other_queue in list(self._clients.items()):
                    if other_ws != ws and not getattr(other_ws, "closed", False):
                        other_queue.put_nowait(message)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            pass
        finally:
            self._clients.pop(ws, None)
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
