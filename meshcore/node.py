"""Node facade tying hub server, election, and link transport together."""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from typing import Any, Callable
import urllib.parse

from meshcore.config import MeshConfig, PeerConfig, format_ws_url
from meshcore.coordination import order
from meshcore.crypto import GroupCipher, MeshCryptoError
from meshcore.election import Election
from meshcore.hub import HubServer
from meshcore.link import Link, LinkError, probe_hub
from meshcore.messages import Hello

__all__ = ["Node", "NodeError"]

logger = logging.getLogger("meshcore.node")


class NodeError(Exception):
    """Exception raised for node operations when no route exists or link fails."""


def _extract_host(address: str) -> str:
    addr = address.strip()
    if not (addr.startswith("ws://") or addr.startswith("wss://")):
        addr = f"ws://{addr}"
    parsed = urllib.parse.urlsplit(addr)
    return parsed.hostname or ""


def _is_loopback(host: str) -> bool:
    h = host.strip().lower()
    if h == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_loopback
    except ValueError:
        return False


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

        self._cipher: GroupCipher | None = None
        if config.role != "relay" and config.e2e_key is not None:
            self._cipher = GroupCipher(config.e2e_key)

        self._dropped_undecryptable: int = 0

        self._hub: HubServer | None = None
        self._election: Election | None = None
        self._link: Link | None = None
        # _desired_hub is the election result; _current_hub is the hub this node is
        # actually attached to (its own server, or a connected client link). Keeping
        # them apart means hub_id only moves once the new link is usable, so callers
        # that wait for hub_id do not publish into a half-migrated cluster.
        self._desired_hub: str | None = None
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
    def group_id(self) -> str:
        """Group ID for this node."""
        return self._config.group_id

    @property
    def is_relay(self) -> bool:
        """Whether this node runs as a relay."""
        return self._config.role == "relay"

    @property
    def relay_id(self) -> str | None:
        """Device ID of the configured preferred relay hub, or None."""
        return self._config.relay.device_id if self._config.relay is not None else None

    @property
    def dropped_undecryptable(self) -> int:
        """Count of received frames dropped due to decryption failure."""
        with self._lifecycle_lock:
            return self._dropped_undecryptable

    @property
    def hub_id(self) -> str | None:
        """The hub this node is attached to, or None while (re)connecting."""
        if self.is_relay:
            return None
        with self._lifecycle_lock:
            return self._current_hub

    @property
    def is_hub(self) -> bool:
        """Whether this node is the elected hub (not just a listener)."""
        if self.is_relay:
            return False
        with self._lifecycle_lock:
            return (
                self._desired_hub == self.device_id
                and self._hub is not None
                and self._hub.running
            )

    @property
    def connected(self) -> bool:
        """Whether the node is connected to the elected hub (or serving as hub)."""
        if self.is_relay:
            return False
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

            # Security gate before starting any network thread
            if self._config.role != "relay" and self._config.e2e_key is None:
                non_loopback = False
                if not _is_loopback(self._config.bind):
                    non_loopback = True
                else:
                    for peer in self._config.peers:
                        if not _is_loopback(_extract_host(peer.address)):
                            non_loopback = True
                            break
                    if not non_loopback and self._config.relay is not None:
                        if not _is_loopback(_extract_host(self._config.relay.address)):
                            non_loopback = True

                if non_loopback:
                    if not self._config.allow_insecure:
                        raise NodeError(
                            "e2e_key is required when binding to or connecting to non-loopback addresses, "
                            "unless allow_insecure=True is explicitly set"
                        )
                    logger.warning(
                        "allow_insecure=True is set: node running without e2e_key on non-loopback address"
                    )

            self._started = True
            self._stopped = False

            if self._config.role == "relay":
                self._hub = HubServer(
                    groups=self._config.serve_groups,
                    host=self._config.bind,
                    port=self._config.port,
                    max_frame_bytes=self._config.max_frame_bytes,
                    device_id=None,
                    on_data=None,
                )
                self._hub.start()
                return

            # Member role
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
                group_id=self._config.group_id,
                on_data=self._on_hub_data,
            )
            self._hub.start()

            # 2. Start election driver
            preferred_id = self._config.relay.device_id if self._config.relay is not None else None
            self._election = Election(
                self_hello=self._config.hello(),
                peers=self._config.peers,
                ping=self._ping,
                clock=self._clock if self._clock is not None else time.monotonic,
                heartbeat=self._config.heartbeat,
                missed_pongs=self._config.missed_pongs,
                probe_timeout=self._config.probe_timeout,
                debounce=self._config.debounce,
                preferred_id=preferred_id,
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

            election = self._election
            self._election = None

            with self._link_lock:
                link = self._link
                self._link = None

            hub = self._hub
            self._hub = None
            self._desired_hub = None
            self._current_hub = None

        if election is not None:
            election.stop()

        if link is not None:
            link.close()

        if hub is not None:
            hub.stop()

    def publish(self, payload: bytes | bytearray | memoryview) -> None:
        """Publish payload to the cluster. Raises NodeError if no live route or relay."""
        if self.is_relay:
            raise NodeError("Relay nodes cannot publish")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise NodeError(f"payload must be bytes-like, got {type(payload).__name__}")
        raw_bytes = bytes(payload)
        if self._cipher is not None:
            try:
                frame = self._cipher.encrypt(raw_bytes)
            except Exception as exc:
                raise NodeError(f"Encryption failed: {exc}") from exc
        else:
            frame = raw_bytes

        if self.is_hub:
            if self._hub is None or not self._hub.running:
                raise NodeError("Hub server is not running")
            self._hub.broadcast(frame, group_id=self._config.group_id)
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
        if self.is_relay:
            return []
        if self.is_hub:
            if self._hub is not None and self._hub.running:
                return self._hub.roster(self._config.group_id)
        with self._roster_lock:
            if self._client_roster is not None:
                filtered = [h for h in self._client_roster if h.group_id == self._config.group_id]
                return order(filtered)
        return [self._config.hello()]

    def _ping(self, target_id: str, timeout: float) -> bool:
        if target_id == self.device_id:
            return True

        if not self._can_reach(self.device_id, target_id):
            return False

        if self._config.relay is not None and target_id == self._config.relay.device_id:
            try:
                url = format_ws_url(self._config.relay.address)
            except ValueError:
                return False
            return probe_hub(url, self._config.token, timeout)

        peer = self._peer_by_id.get(target_id)
        if peer is None:
            return False

        try:
            url = format_ws_url(peer.address)
        except ValueError:
            return False

        return probe_hub(url, self._config.token, timeout)

    def _handle_hub_changed(self, new_hub: str) -> None:
        old_hub = self._current_hub
        self._desired_hub = new_hub
        forced = self._election is not None and self._election.last_forced
        reason = "forced: no reachable device will_hub" if forced else "election probe"
        logger.info("Hub changed from %s to %s (%s)", old_hub, new_hub, reason)

        if new_hub == self.device_id:
            with self._link_lock:
                if self._link is not None:
                    self._link.close()
                    self._link = None
            self._current_hub = new_hub
        else:
            self._ensure_link(new_hub)

        # Notify callbacks outside internal locks
        for cb in list(self._hub_changed_callbacks):
            try:
                cb(new_hub)
            except Exception:
                pass

    def _ensure_link(self, target_hub: str | None = None) -> None:
        hub = target_hub if target_hub is not None else self._desired_hub
        if hub is None or hub == self.device_id:
            return

        if self._config.relay is not None and hub == self._config.relay.device_id:
            target_url = format_ws_url(self._config.relay.address)
        else:
            peer = self._peer_by_id.get(hub)
            if peer is None:
                return
            target_url = format_ws_url(peer.address)

        with self._link_lock:
            if self._link is not None:
                if self._link.url != target_url:
                    self._link.close()
                    self._link = None
                    self._current_hub = None
                elif self._link.connected:
                    self._current_hub = hub
                    return
                else:
                    # Same address: the Link reconnects on its own.
                    return

            if not self._can_reach(self.device_id, hub):
                return

            link = Link(
                url=target_url,
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
                self._current_hub = hub
            except LinkError:
                # Initial connect failed; publish() retries on the next call and a
                # later election change re-triggers the link.
                link.close()
                self._link = None
                self._current_hub = None

    def _handle_client_roster(self, new_roster: list[Hello]) -> None:
        with self._roster_lock:
            self._client_roster = list(new_roster)
        if self._election is not None:
            merged: dict[str, Any] = dict(self._peer_by_id)
            for hello in new_roster:
                if hello.device_id != self.device_id:
                    merged[hello.device_id] = hello
            merged.pop(self.device_id, None)
            if self._config.relay is not None:
                merged.pop(self._config.relay.device_id, None)
            self._election.set_devices(merged.values())

    def _handle_client_payload(self, sender_id: str | None, payload: bytes) -> None:
        if self._cipher is not None:
            try:
                decrypted = self._cipher.decrypt(payload)
            except MeshCryptoError:
                with self._lifecycle_lock:
                    self._dropped_undecryptable += 1
                return
            except Exception:
                with self._lifecycle_lock:
                    self._dropped_undecryptable += 1
                return
        else:
            decrypted = payload

        for cb in list(self._receive_callbacks):
            try:
                cb(sender_id, decrypted)
            except Exception:
                pass

    def _on_hub_data(self, sender_id: str | None, payload: bytes) -> None:
        if sender_id == self.device_id:
            return

        if self._cipher is not None:
            try:
                decrypted = self._cipher.decrypt(payload)
            except MeshCryptoError:
                with self._lifecycle_lock:
                    self._dropped_undecryptable += 1
                return
            except Exception:
                with self._lifecycle_lock:
                    self._dropped_undecryptable += 1
                return
        else:
            decrypted = payload

        for cb in list(self._receive_callbacks):
            try:
                cb(sender_id, decrypted)
            except Exception:
                pass
