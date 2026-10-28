"""Generic WebSocket client link for meshcore."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import socket
import threading
import urllib.parse
from typing import Any, Callable

import websockets
from websockets.client import WebSocketClientProtocol

from meshcore.config import DEFAULT_MAX_FRAME_BYTES
from meshcore.messages import (
    CONTROL_LEAVING,
    CONTROL_ROSTER,
    Hello,
    decode_control,
    encode_hello_message,
    encode_leaving,
    unwrap_data_envelope,
)

__all__ = [
    "Link",
    "LinkError",
    "probe_hub",
    "relay_url",
]


class LinkError(Exception):
    """Transport or protocol error in a meshcore Link."""


def relay_url(port: int, host: str = "127.0.0.1") -> str:
    """Return the WebSocket URL for the given host and port."""
    return f"ws://{host}:{port}/"


def probe_hub(url: str, token: str, timeout: float) -> bool:
    """Return True when the hub's port accepts a connection within ``timeout``.

    The probe is deliberately a bare TCP connect: a WebSocket (or even HTTP)
    request makes the hub's event loop do work on every election round, which
    shifts the existing election/link timings. The failed-handshake log it
    causes is suppressed at the source by ``meshcore.hub`` (the
    ``websockets.server`` logger is raised to WARNING). Kept here so the probe
    has one named seam and its timeout is easy to test.
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        sock.close()


def _coerce_frame(frame: Any) -> bytes:
    """Coerce bytes-like objects to bytes."""
    if isinstance(frame, (bytes, bytearray, memoryview)):
        return bytes(frame)
    raise LinkError(f"Expected bytes-like frame, got {type(frame).__name__}")


class Link:
    """Synchronous transport client connected to a HubServer."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        hello: Hello | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        reconnect: bool = True,
        reconnect_delay: float = 0.2,
        connect_timeout: float = 5.0,
        thread_name: str = "meshcore-link",
        on_roster: Callable[[list[Hello]], None] | None = None,
        on_payload: Callable[[str | None, bytes], None] | None = None,
    ) -> None:
        self._url: str = url
        self._token: str = token
        self._hello: Hello | None = hello
        self._max_frame_bytes: int = max_frame_bytes
        self._reconnect: bool = reconnect
        self._reconnect_delay: float = reconnect_delay
        self._connect_timeout: float = connect_timeout
        self._thread_name: str = thread_name
        self._on_roster: Callable[[list[Hello]], None] | None = on_roster
        self._on_payload: Callable[[str | None, bytes], None] | None = on_payload

        self._roster: list[Hello] = []
        self._roster_lock = threading.Lock()
        self._connected: bool = False
        self._closed: bool = False
        self._inbox: collections.deque[bytes] = collections.deque()
        self._inbox_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()

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
        """Whether the link is currently connected to the hub."""
        ws = self._ws
        return (
            self._connected
            and ws is not None
            and getattr(ws, "open", False)
            and not self._closed
        )

    @property
    def roster(self) -> list[Hello]:
        """The last received roster."""
        with self._roster_lock:
            return list(self._roster)

    def set_on_roster(self, callback: Callable[[list[Hello]], None] | None) -> None:
        """Set or update the callback for roster updates."""
        self._on_roster = callback

    def set_on_payload(self, callback: Callable[[str | None, bytes], None] | None) -> None:
        """Set or update the callback for incoming app payloads."""
        self._on_payload = callback

    def __enter__(self) -> Link:
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
        """Connect to the hub server.

        Starts the background event loop, performs the authenticated handshake,
        and waits until connected. Raises LinkError on initial failure or timeout.
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
                name=self._thread_name,
                daemon=True,
            )
            self._thread.start()

        ok = self._connected_event.wait(timeout=self._connect_timeout)
        if not ok:
            self.close()
            raise LinkError(
                f"Connection timed out after {self._connect_timeout}s"
            )

        if self._connect_error is not None:
            err = self._connect_error
            self.close()
            raise LinkError(f"Connection failed: {err}") from err

    def close(self) -> None:
        """Close connection and stop reconnecting.

        Idempotent.
        """
        with self._lifecycle_lock:
            if self._closed and self._thread is None:
                return
            self._closed = True
            self._connected = False

            # Best-effort leaving notification
            if (
                self._hello is not None
                and self._ws is not None
                and self._loop is not None
                and self._loop.is_running()
            ):
                try:
                    leaving_msg = encode_leaving()
                    future = asyncio.run_coroutine_threadsafe(
                        self._ws.send(leaving_msg), self._loop
                    )
                    future.result(timeout=0.1)
                except Exception:
                    pass

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
        """Send an opaque frame to the hub.

        Preserves call order. Raises LinkError if disconnected or oversized.
        """
        frame_bytes = _coerce_frame(frame)
        if len(frame_bytes) > self._max_frame_bytes:
            raise LinkError(
                f"Frame size ({len(frame_bytes)} bytes) exceeds max_frame_bytes ({self._max_frame_bytes})"
            )
        if not self.connected or self._ws is None or self._loop is None:
            raise LinkError("Link is disconnected")

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_send(frame_bytes), self._loop
            )
            future.result(timeout=self._connect_timeout)
        except LinkError:
            raise
        except Exception as exc:
            raise LinkError(f"Send failed: {exc}") from exc

    async def _async_send(self, frame: bytes) -> None:
        assert self._send_lock is not None
        async with self._send_lock:
            ws = self._ws
            if ws is None or not getattr(ws, "open", False):
                raise LinkError("Link is disconnected")
            await ws.send(frame)

    def recv(self) -> list[bytes]:
        """Drain and return all queued payload frames in FIFO order.

        Raises LinkError if disconnected.
        """
        if not self.connected:
            raise LinkError("Link is disconnected")
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
        except asyncio.CancelledError:
            pass
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

                if self._hello is not None:
                    hello_msg = encode_hello_message(self._hello)
                    await ws.send(hello_msg)

                if first_attempt:
                    first_attempt = False
                    self._connected_event.set()

                async for message in ws:
                    if self._closed:
                        break
                    if isinstance(message, str):
                        try:
                            msg_type, payload = decode_control(message)
                        except ValueError:
                            continue

                        if msg_type == CONTROL_ROSTER:
                            with self._roster_lock:
                                self._roster = list(payload)
                            if self._on_roster is not None:
                                try:
                                    self._on_roster(list(payload))
                                except Exception:
                                    pass
                        elif msg_type == CONTROL_LEAVING:
                            break
                    else:
                        raw_bytes = bytes(message)
                        sender_id, payload_bytes = unwrap_data_envelope(raw_bytes)
                        with self._inbox_lock:
                            self._inbox.append(payload_bytes)
                        if self._on_payload is not None:
                            try:
                                self._on_payload(sender_id, payload_bytes)
                            except Exception:
                                pass
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
