"""Node facade tying hub server, election, and link transport together."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Callable
import urllib.parse

from meshcore.config import MeshConfig, PeerConfig, format_ws_url
from meshcore.coordination import order
from meshcore.election import Election
from meshcore.hub import HubServer
from meshcore.link import Link, LinkError
from meshcore.messages import Hello

__all__ = ["Node", "NodeError"]

logger = logging.getLogger("meshcore.node")


class NodeError(Exception):
    """Exception raised for node operations when no route exists or link fails."""


def _parse_host_port(address: str) -> tuple[str, int]:
    addr = address.strip()
    if addr.startswith("ws://") or addr.startswith("wss://"):
        parsed = urllib.parse.urlsplit(addr)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        return host, port
    else:
        host, sep, port_str = addr.partition(":")
        port = int(port_str.split("/")[0])
        return host, port


class Node:
    """Facade managing elected hub role, client link, and payload routing."""

    def __init__(
        self,
        config: MeshConfig,
        *,
        can_reach: Callable[[str, str], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config: MeshConfig = config
        self._can_reach: Callable[[str, str], bool] = (
            can_reach if can_reach is not None else (lambda a, b: True)
        )
        self._clock: Callable[[], float] | None = clock
        self._peer_by_id: dict[str, PeerConfig] = {
            p.device_id: p for p in config.peers
        }

        self._hub: HubServer | None = None
        self._election: Election | None = None
        self._link: Link | None = None
        self._current_hub: str | None = None
        self._client_roster: list[Hello] | None = None

        self._receive_callbacks: list[Callable[[str | None, bytes], None]] = []
        self._hub_changed_callbacks: list[Callable[[str], None]] = []

        self._started: bool = False
        self._stopped: bool = False
        self._lifecycle_lock = threading.RLock()
        self._link_lock = threading.Lock()
        self._roster_lock = threading.Lock()

    @property
    def device_id(self) -> str:
        """Local device ID."""
        return self._config.device_id

    @property
    def hub_id(self) -> str | None:
        """Currently elected hub device ID, or None if not known."""
        with self._lifecycle_lock:
            if self._election is not None:
                return self._election.current_hub
            return self._current_hub

    @property
    def is_hub(self) -> bool:
        """Whether this node is currently acting as the elected hub."""
        return self.hub_id == self.device_id

    @property
    def connected(self) -> bool:
        """Whether the node is connected to the elected hub (or serving as hub)."""
        if self.is_hub:
            return self._hub is not None and self._hub.running
        with self._link_lock:
            return self._link is not None and self._link.connected

    def on_receive(self, callback: Callable[[str | None, bytes], None]) -> None:
        """Register a callback for received application payloads."""
        self._receive_callbacks.append(callback)

    def on_hub_changed(self, callback: Callable[[str], None]) -> None:
        """Register a callback for elected hub changes."""
        self._hub_changed_callbacks.append(callback)

    def start(self) -> None:
        """Start hub server listener, election, and hub connection tracking. Idempotent."""
        with self._lifecycle_lock:
            if self._started:
                return
            self._started = True
            self._stopped = False

            # 1. Start hub server listener on bind:port
            self._hub = HubServer(
                token=self._config.token,
                host=self._config.bind,
                port=self._config.port,
                max_frame_bytes=self._config.max_frame_bytes,
                device_id=self._config.device_id,
                name=self._config.name,
                perm=self._config.perm,
                temp_rank=self._config.temp_rank,
                will_hub=self._config.will_hub,
                on_data=self._on_hub_data,
            )
            self._hub.start()

            # 2. Start election driver
            self._election = Election(
                self_hello=self._config.hello(),
                peers=self._config.peers,
                ping=self._ping,
                clock=self._clock if self._clock is not None else time.monotonic,
                heartbeat=self._config.heartbeat,
                missed_pongs=self._config.missed_pongs,
                probe_timeout=self._config.probe_timeout,
                debounce=self._config.debounce,
                on_hub_changed=self._handle_hub_changed,
                logger=logging.getLogger("meshcore.election"),
            )
            self._election.start()

    def stop(self) -> None:
        """Stop link, election, and hub server, and release listening port. Idempotent."""
        with self._lifecycle_lock:
            if self._stopped or not self._started:
                self._stopped = True
                return
            self._stopped = True
            self._started = False

            if self._election is not None:
                self._election.stop()
                self._election = None

            with self._link_lock:
                if self._link is not None:
                    self._link.close()
                    self._link = None

            if self._hub is not None:
                self._hub.stop()
                self._hub = None

    def publish(self, payload: bytes | bytearray | memoryview) -> None:
        """Publish payload to the cluster. Raises NodeError if no live route."""
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise NodeError(f"payload must be bytes-like, got {type(payload).__name__}")
        frame = bytes(payload)

        if self.is_hub:
            if self._hub is None or not self._hub.running:
                raise NodeError("Hub server is not running")
            self._hub.broadcast(frame)
        else:
            self._ensure_link()
            with self._link_lock:
                link = self._link
                if link is None or not link.connected:
                    raise NodeError("No live link to hub")
                try:
                    link.send(frame)
                except LinkError as exc:
                    raise NodeError(str(exc)) from exc

    def roster(self) -> list[Hello]:
        """Return the current cluster roster sorted by coordination.order."""
        if self.is_hub:
            if self._hub is not None and self._hub.running:
                return self._hub.roster()
        with self._roster_lock:
            if self._client_roster is not None:
                return order(self._client_roster)
        return [self._config.hello()]

    def _ping(self, target_id: str, timeout: float) -> bool:
        if target_id == self.device_id:
            return True

        if not self._can_reach(self.device_id, target_id):
            return False

        peer = self._peer_by_id.get(target_id)
        if peer is None:
            return False

        try:
            host, port = _parse_host_port(peer.address)
        except Exception:
            return False

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except Exception:
            return False
        finally:
            sock.close()

    def _handle_hub_changed(self, new_hub: str) -> None:
        old_hub = self._current_hub
        self._current_hub = new_hub
        logger.info("Hub changed from %s to %s (election)", old_hub, new_hub)

        if new_hub == self.device_id:
            with self._link_lock:
                if self._link is not None:
                    self._link.close()
                    self._link = None
        else:
            self._ensure_link(new_hub)

        # Notify callbacks outside internal locks
        for cb in list(self._hub_changed_callbacks):
            try:
                cb(new_hub)
            except Exception:
                pass

    def _ensure_link(self, target_hub: str | None = None) -> None:
        hub = target_hub if target_hub is not None else self.hub_id
        if hub is None or hub == self.device_id:
            return

        with self._link_lock:
            if self._link is not None and self._link.connected:
                return

            if self._link is not None and not self._link.connected:
                # If existing link URL does not match current hub, close it
                peer = self._peer_by_id.get(hub)
                if peer is not None and format_ws_url(peer.address) != self._link.url:
                    self._link.close()
                    self._link = None
                else:
                    return

            if not self._can_reach(self.device_id, hub):
                return

            peer = self._peer_by_id.get(hub)
            if peer is None:
                return

            url = format_ws_url(peer.address)
            link = Link(
                url=url,
                token=self._config.token,
                hello=self._config.hello(),
                max_frame_bytes=self._config.max_frame_bytes,
                reconnect=True,
                reconnect_delay=self._config.reconnect_delay,
                connect_timeout=self._config.connect_timeout,
                thread_name=f"meshcore-link-{self.device_id}",
                on_roster=self._handle_client_roster,
                on_payload=self._handle_client_payload,
            )
            try:
                link.connect()
                self._link = link
            except LinkError:
                # Initial connect failed; link will be retried
                link.close()
                self._link = None

    def _handle_client_roster(self, new_roster: list[Hello]) -> None:
        with self._roster_lock:
            self._client_roster = list(new_roster)
        if self._election is not None:
            self._election.set_devices(new_roster)

    def _handle_client_payload(self, sender_id: str | None, payload: bytes) -> None:
        for cb in list(self._receive_callbacks):
            try:
                cb(sender_id, payload)
            except Exception:
                pass

    def _on_hub_data(self, sender_id: str | None, payload: bytes) -> None:
        if sender_id == self.device_id:
            return
        for cb in list(self._receive_callbacks):
            try:
                cb(sender_id, payload)
            except Exception:
                pass
