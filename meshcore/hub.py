"""Generic WebSocket hub server for meshcore."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from http import HTTPStatus
import json
import threading
from typing import Any, Callable, Iterable

import websockets
from websockets.server import WebSocketServer

from meshcore.config import DEFAULT_MAX_FRAME_BYTES, GroupConfig
from meshcore.coordination import order
from meshcore.messages import (
    CONTROL_HELLO,
    CONTROL_LEAVING,
    Hello,
    decode_hello,
    encode_roster,
    wrap_data_envelope,
)

RELAY_MAX_FRAME_BYTES: int = DEFAULT_MAX_FRAME_BYTES

__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "HubServer",
    "RELAY_MAX_FRAME_BYTES",
]


class HubServer:
    """Synchronous lifecycle wrapper around an asyncio WebSocket hub server."""

    def __init__(
        self,
        *,
        token: str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        device_id: str | None = None,
        name: str | None = None,
        perm: int | None = None,
        temp_rank: int | None = None,
        will_hub: bool = True,
        on_data: Callable[[str | None, bytes], None] | None = None,
        group_id: str = "default",
        groups: Iterable[GroupConfig] | None = None,
    ) -> None:
        if not isinstance(host, str) or len(host) == 0:
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
            raise ValueError(f"port must be an int between 0 and 65535, got {port!r}")
        if isinstance(max_frame_bytes, bool) or not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
            raise ValueError(
                f"max_frame_bytes must be a positive int, got {max_frame_bytes!r}"
            )

        if groups is None:
            if not isinstance(token, str) or len(token) == 0:
                raise ValueError("token must be a non-empty string when groups is None")
            if not isinstance(group_id, str) or not group_id.strip():
                raise ValueError("group_id must be a non-empty string")
            self._groups: tuple[GroupConfig, ...] = (GroupConfig(group_id.strip(), token),)
        else:
            groups_tuple = tuple(groups)
            if not groups_tuple:
                raise ValueError("groups must not be empty when provided")
            for g in groups_tuple:
                if not isinstance(g, GroupConfig):
                    raise ValueError(f"All groups must be GroupConfig instances, got {type(g).__name__}")
            g_ids = [g.id for g in groups_tuple]
            g_tokens = [g.token for g in groups_tuple]
            if len(set(g_ids)) != len(g_ids):
                raise ValueError("serve_groups ids must be unique")
            if len(set(g_tokens)) != len(g_tokens):
                raise ValueError("serve_groups tokens must be unique")
            self._groups = groups_tuple

        self._token: str | None = token
        self._group_id: str = group_id
        self._host: str = host
        self._port: int = port
        self._max_frame_bytes: int = max_frame_bytes
        self._device_id: str | None = device_id
        self._name: str | None = name
        self._perm: int | None = perm
        self._temp_rank: int | None = temp_rank
        self._will_hub: bool = will_hub
        self._on_data: Callable[[str | None, bytes], None] | None = on_data

        self._bound_port: int = 0
        self._running: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: WebSocketServer | None = None
        self._stop_event: asyncio.Event | None = None
        self._clients: dict[Any, asyncio.Queue[bytes | str]] = {}
        self._hello_by_ws: dict[Any, Hello] = {}
        self._group_by_ws: dict[Any, str] = {}
        self._lock = threading.Lock()
        self._clients_lock = threading.Lock()
        self._started_event = threading.Event()
        self._start_error: BaseException | None = None

    @property
    def host(self) -> str:
        """Host address."""
        return self._host

    @property
    def port(self) -> int:
        """Actual bound port, or initial port if not yet listening."""
        return self._bound_port if self._bound_port != 0 else self._port

    @property
    def running(self) -> bool:
        """Whether the server is currently running and listening."""
        return self._running

    @property
    def groups(self) -> tuple[GroupConfig, ...]:
        """Tuple of groups served by this HubServer."""
        return self._groups

    def set_on_data(self, callback: Callable[[str | None, bytes], None] | None) -> None:
        """Set or update the callback for receiving client app payloads."""
        self._on_data = callback

    def __enter__(self) -> HubServer:
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
        Idempotent.
        """
        with self._lock:
            if self._running:
                return
            self._started_event = threading.Event()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._run_loop, name="meshcore-hub-server", daemon=True
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

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the server, disconnect clients, release port, and join thread.

        Idempotent. Waits at most ``timeout`` seconds for the loop thread to
        exit; a timed-out thread keeps its reference so a later ``stop()`` can
        retry instead of blocking forever.
        """
        with self._lock:
            if not self._running and self._thread is None:
                return
            self._running = False
            loop = self._loop
            thread = self._thread

        if loop is not None and loop.is_running():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._signal_stop)

        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

        with self._lock:
            if thread is not None and thread.is_alive():
                return
            self._thread = None
            self._loop = None
            self._server = None
            self._stop_event = None
            with self._clients_lock:
                self._clients.clear()
                self._hello_by_ws.clear()
                self._group_by_ws.clear()

    def _signal_stop(self) -> None:
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()

    def roster(self, group_id: str | None = None) -> list[Hello]:
        """Return the current roster for a group sorted by coordination.order."""
        if group_id is None:
            if len(self._groups) == 1:
                return self.roster(self._groups[0].id)
            devices: list[Hello] = []
            if self._device_id is not None:
                devices.append(
                    Hello(
                        device_id=self._device_id,
                        name=self._name if self._name is not None else self._device_id,
                        perm=self._perm,
                        temp_rank=self._temp_rank,
                        will_hub=self._will_hub,
                        group_id=self._group_id,
                    )
                )
            with self._clients_lock:
                devices.extend(self._hello_by_ws.values())
            return order(devices)

        devices = []
        if self._device_id is not None and self._group_id == group_id:
            devices.append(
                Hello(
                    device_id=self._device_id,
                    name=self._name if self._name is not None else self._device_id,
                    perm=self._perm,
                    temp_rank=self._temp_rank,
                    will_hub=self._will_hub,
                    group_id=self._group_id,
                )
            )
        with self._clients_lock:
            for ws, hello in self._hello_by_ws.items():
                if self._group_by_ws.get(ws) == group_id:
                    devices.append(hello)
        return order(devices)

    def broadcast(
        self, payload: bytes | bytearray | memoryview, group_id: str | None = None
    ) -> None:
        """Broadcast hub-origin data to every connected client of the target group."""
        frame = bytes(payload)
        if group_id is None:
            if len(self._groups) == 1:
                target_group = self._groups[0].id
            else:
                raise ValueError(
                    "group_id is required for broadcast when multiple groups are served"
                )
        else:
            target_group = group_id

        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._async_broadcast, frame, target_group)

    def _async_broadcast(self, payload: bytes, target_group: str) -> None:
        wrapped: bytes | None = None
        with self._clients_lock:
            for ws, queue in list(self._clients.items()):
                if self._group_by_ws.get(ws) == target_group and not getattr(ws, "closed", False):
                    if ws in self._hello_by_ws:
                        if wrapped is None:
                            wrapped = wrap_data_envelope(self._device_id, payload)
                        queue.put_nowait(wrapped)
                    else:
                        queue.put_nowait(payload)

    def _broadcast_roster(self, group_id: str) -> None:
        """Broadcast updated roster to helloed clients of a given group."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is not loop:
            loop.call_soon_threadsafe(self._broadcast_roster, group_id)
            return

        roster_msg = encode_roster(self.roster(group_id))
        with self._clients_lock:
            for ws, hello in list(self._hello_by_ws.items()):
                if self._group_by_ws.get(ws) == group_id:
                    queue = self._clients.get(ws)
                    if queue is not None and not getattr(ws, "closed", False):
                        queue.put_nowait(roster_msg)

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
            if not isinstance(auth, str) or not auth.startswith("Bearer "):
                return (HTTPStatus.FORBIDDEN, [], b"")
            token_candidate = auth[7:]
            matched = False
            for g in self._groups:
                if hmac.compare_digest(token_candidate, g.token):
                    matched = True
                    break
            if not matched:
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
        with self._clients_lock:
            clients_to_close = list(self._clients.keys())
        for ws in clients_to_close:
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
        auth = getattr(ws, "request_headers", {}).get("Authorization", "")
        token_candidate = auth[7:] if isinstance(auth, str) and auth.startswith("Bearer ") else ""
        bound_group_id: str | None = None
        for g in self._groups:
            if hmac.compare_digest(token_candidate, g.token):
                bound_group_id = g.id
                break

        if bound_group_id is None:
            await ws.close(1008, "Unauthorized")
            return

        queue: asyncio.Queue[bytes | str] = asyncio.Queue()
        with self._clients_lock:
            self._clients[ws] = queue
            self._group_by_ws[ws] = bound_group_id

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

                if isinstance(message, (bytes, bytearray, memoryview)):
                    payload = bytes(message)
                    with self._clients_lock:
                        sender_hello = self._hello_by_ws.get(ws)
                    sender_id = sender_hello.device_id if sender_hello is not None else None

                    # Notify hub's own application
                    if self._on_data is not None:
                        try:
                            self._on_data(sender_id, payload)
                        except Exception:
                            pass

                    # Forward to other clients in same group (no echo to sender)
                    wrapped: bytes | None = None
                    with self._clients_lock:
                        for other_ws, other_queue in list(self._clients.items()):
                            if (
                                other_ws != ws
                                and self._group_by_ws.get(other_ws) == bound_group_id
                                and not getattr(other_ws, "closed", False)
                            ):
                                if other_ws in self._hello_by_ws:
                                    if wrapped is None:
                                        wrapped = wrap_data_envelope(sender_id, payload)
                                    other_queue.put_nowait(wrapped)
                                else:
                                    other_queue.put_nowait(payload)
                elif isinstance(message, str):
                    try:
                        data = json.loads(message)
                        is_json = True
                    except Exception:
                        is_json = False

                    if is_json and isinstance(data, dict) and "type" in data:
                        msg_type = data.get("type")
                        if msg_type == CONTROL_HELLO:
                            try:
                                hello = decode_hello(data)
                            except Exception:
                                continue
                            if hello.group_id != bound_group_id:
                                with self._clients_lock:
                                    self._hello_by_ws.pop(ws, None)
                                await ws.close(1008, "Group mismatch")
                                break
                            with self._clients_lock:
                                self._hello_by_ws[ws] = hello
                            self._broadcast_roster(bound_group_id)
                        elif msg_type == CONTROL_LEAVING:
                            with self._clients_lock:
                                removed = self._hello_by_ws.pop(ws, None)
                            if removed is not None:
                                self._broadcast_roster(bound_group_id)
                        else:
                            # Malformed / unknown control text is ignored
                            pass
                    else:
                        # Non-control text frame: forward to un-helloed clients only in same group
                        with self._clients_lock:
                            is_helloed = ws in self._hello_by_ws
                            if not is_helloed:
                                for other_ws, other_queue in list(self._clients.items()):
                                    if (
                                        other_ws != ws
                                        and self._group_by_ws.get(other_ws) == bound_group_id
                                        and other_ws not in self._hello_by_ws
                                        and not getattr(other_ws, "closed", False)
                                    ):
                                        other_queue.put_nowait(message)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            pass
        finally:
            with self._clients_lock:
                self._clients.pop(ws, None)
                had_hello = self._hello_by_ws.pop(ws, None)
                self._group_by_ws.pop(ws, None)
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
            if had_hello is not None and self._running:
                self._broadcast_roster(bound_group_id)
