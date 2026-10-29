"""Node facade tying hub server, election, and link transport together."""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable
import urllib.parse

from meshcore.addressing import (
    LastGoodStore,
    default_state_dir,
    ordered_addresses,
    parse_address,
)
from meshcore.config import MeshConfig, PeerConfig, format_ws_url
from meshcore.coordination import (
    HLC,
    MAX_CLOCK_AHEAD_S,
    MAX_STATE_KEYS_PER_GROUP,
    MAX_STATE_PAYLOAD_BYTES,
    StateEntry,
    StateRegister,
    StateStats,
    StateVersion,
    is_valid_state_key,
    order,
)
from meshcore.crypto import GroupCipher, MeshCryptoError
from meshcore.discovery import (
    Discovery,
    DiscoverySocket,
    create_udp_socket,
)
from meshcore.election import Election
from meshcore.hub import HubServer
from meshcore.link import Link, LinkError, probe_hub
from meshcore.messages import (
    Hello,
    STATE_MAGIC,
    decode_state_frame,
    encode_state_frame,
    encode_state_sync,
)

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
        wall_clock: Callable[[], float] | None = None,
        probe_url: Callable[[str, float], bool] | None = None,
        discovery_socket_factory: Callable[[], DiscoverySocket] | None = None,
    ) -> None:
        self._config: MeshConfig = config
        self._can_reach: Callable[[str, str], bool] = (
            can_reach if can_reach is not None else (lambda a, b: True)
        )
        self._clock: Callable[[], float] | None = clock
        self._wall_clock: Callable[[], float] = wall_clock if wall_clock is not None else time.time
        self._hlc = HLC(self._config.device_id, wall_clock=self._wall_clock)
        self._state_register = StateRegister(MAX_STATE_KEYS_PER_GROUP)
        self._probe_url: Callable[[str, float], bool] = (
            probe_url
            if probe_url is not None
            else (lambda url, timeout: probe_hub(url, config.token, timeout))
        )
        self._discovery_socket_factory: Callable[[], DiscoverySocket] | None = (
            discovery_socket_factory
        )
        self._peer_by_id: dict[str, PeerConfig] = {
            p.device_id: p for p in config.peers
        }

        state_dir = (
            Path(config.state_dir).expanduser()
            if config.state_dir is not None
            else default_state_dir()
        )
        self._last_good: LastGoodStore = LastGoodStore(state_dir / "last_good.json")
        self._discovery: Discovery | None = None

        self._cipher: GroupCipher | None = None
        if config.role != "relay" and config.e2e_key is not None:
            self._cipher = GroupCipher(config.e2e_key)

        self._dropped_undecryptable: int = 0

        self._stat_published: int = 0
        self._stat_sent: int = 0
        self._stat_received: int = 0
        self._stat_applied: int = 0
        self._stat_ignored: int = 0
        self._stat_rejected_skew: int = 0
        self._stat_rejected_invalid: int = 0
        self._state_stats_lock = threading.Lock()
        self._last_skew_warn_by_author: dict[str, float] = {}
        self._state_callbacks: list[Callable[[str, str, bytes], None]] = []

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

    @property
    def connection_url(self) -> str | None:
        """The WebSocket URL of the active client link, or None."""
        with self._link_lock:
            if self._link is not None and self._link.connected:
                return self._link.url
            return None

    @property
    def discovery(self) -> Discovery | None:
        """The Discovery instance, or None if discovery is off or relay."""
        with self._lifecycle_lock:
            return self._discovery

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
                        if peer.address and not _is_loopback(_extract_host(peer.address)):
                            non_loopback = True
                            break
                        for addr in peer.addresses:
                            if not _is_loopback(addr.host):
                                non_loopback = True
                                break
                        if non_loopback:
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
                relay_registers = {
                    g.id: StateRegister(MAX_STATE_KEYS_PER_GROUP)
                    for g in self._config.serve_groups
                }
                self._hub = HubServer(
                    groups=self._config.serve_groups,
                    host=self._config.bind,
                    port=self._config.port,
                    max_frame_bytes=self._config.max_frame_bytes,
                    device_id=None,
                    on_data=None,
                    state_registers=relay_registers,
                    wall_clock=time.time,
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
                state_registers={self._config.group_id: self._state_register},
                wall_clock=self._wall_clock,
                on_state_stat=self._record_state_stat,
                on_state_won=self._on_hub_state_won,
            )
            self._hub.start()

            # 2. Start discovery (if enabled)
            if self._config.discovery:
                sock: DiscoverySocket | None = None
                try:
                    sock = (
                        self._discovery_socket_factory()
                        if self._discovery_socket_factory is not None
                        else create_udp_socket(self._config.discovery_port)
                    )
                    self._discovery = Discovery(
                        group_id=self._config.group_id,
                        device_id=self._config.device_id,
                        token=self._config.token,
                        port=self._hub.port,
                        socket=sock,
                        peers={p.device_id for p in self._config.peers},
                        # Discovery timestamps cross devices, so they must be wall
                        # clock; the election clock is monotonic and stays there.
                        clock=time.time,
                        interval=self._config.discovery_interval,
                        discovery_port=self._config.discovery_port,
                        logger=logging.getLogger("meshcore.discovery"),
                    )
                    self._discovery.start()
                except Exception as exc:
                    logger.warning("Failed to start discovery: %s", exc)
                    self._discovery = None
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass

            # 3. Start election driver
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
        """Stop link, election, discovery, and hub server, and release listening port. Idempotent."""
        with self._lifecycle_lock:
            if self._stopped or not self._started:
                self._stopped = True
                return
            self._stopped = True
            self._started = False

            election = self._election
            self._election = None

            discovery = self._discovery
            self._discovery = None

            with self._link_lock:
                link = self._link
                self._link = None

            hub = self._hub
            self._hub = None
            self._desired_hub = None
            self._current_hub = None

        if election is not None:
            election.stop()

        if discovery is not None:
            discovery.stop()

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

    @property
    def state_stats(self) -> StateStats:
        """Frozen snapshot of state channel counters."""
        with self._state_stats_lock:
            return StateStats(
                published=self._stat_published,
                sent=self._stat_sent,
                received=self._stat_received,
                applied=self._stat_applied,
                ignored=self._stat_ignored,
                rejected_skew=self._stat_rejected_skew,
                rejected_invalid=self._stat_rejected_invalid,
            )

    def _record_state_stat(self, name: str, count: int = 1) -> None:
        with self._state_stats_lock:
            if name == "published":
                self._stat_published += count
            elif name == "sent":
                self._stat_sent += count
            elif name == "received":
                self._stat_received += count
            elif name == "applied":
                self._stat_applied += count
            elif name == "ignored":
                self._stat_ignored += count
            elif name == "rejected_skew":
                self._stat_rejected_skew += count
            elif name == "rejected_invalid":
                self._stat_rejected_invalid += count

    def _log_skew_warning(self, key: str, author: str, wall_ms: int, now_ms: int) -> None:
        now_s = self._wall_clock()
        lead_s = int((wall_ms - now_ms) / 1000)
        last_warn = self._last_skew_warn_by_author.get(author, 0.0)
        if now_s - last_warn >= 60.0:
            self._last_skew_warn_by_author[author] = now_s
            logger.warning(
                "rejected state %s from %s: version %d s ahead of local clock (max %d s)",
                key,
                author,
                lead_s,
                MAX_CLOCK_AHEAD_S,
            )

    def on_state(self, callback: Callable[[str, str, bytes], None]) -> None:
        """Register a callback (key, author, payload) for newly-applied received states."""
        self._state_callbacks.append(callback)

    def get_state(self, key: str) -> bytes | None:
        """Return the latest plaintext this node knows for key, or None."""
        if self.is_relay:
            return None
        return self._state_register.get_payload(key)

    def get_state_version(self, key: str) -> StateVersion | None:
        """Return the latest StateVersion this node knows for key, or None."""
        if self.is_relay:
            return None
        return self._state_register.get_version(key)

    def publish_state(self, key: str, payload: bytes | bytearray | memoryview) -> None:
        """Local-first state publish with total ordering via HLC.

        Raises NodeError for relay nodes, bad keys, non-bytes payloads, or oversized values.
        """
        if self.is_relay:
            raise NodeError("Relay nodes cannot publish")
        if not is_valid_state_key(key):
            raise NodeError(f"Invalid state key: {key!r}")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise NodeError(f"payload must be bytes-like, got {type(payload).__name__}")

        raw_bytes = bytes(payload)
        if len(raw_bytes) > MAX_STATE_PAYLOAD_BYTES:
            raise NodeError(
                f"state value too large: {len(raw_bytes)} bytes exceeds cap {MAX_STATE_PAYLOAD_BYTES}"
            )

        version, corrected = self._hlc.publish()

        if self._cipher is not None:
            try:
                body = self._cipher.encrypt(raw_bytes)
            except Exception as exc:
                raise NodeError(f"Encryption failed: {exc}") from exc
        else:
            body = raw_bytes

        try:
            frame = encode_state_frame(key, version, body)
        except Exception as exc:
            raise NodeError(str(exc)) from exc

        if len(frame) > self._config.max_frame_bytes:
            raise NodeError(
                f"state frame too large: {len(frame)} bytes exceeds max_frame_bytes {self._config.max_frame_bytes}"
            )

        # Merge locally; on clock correction replace self-authored poisoned entry
        self._state_register.merge(key, version, raw_bytes, frame_bytes=frame, force=corrected)
        with self._state_stats_lock:
            self._stat_published += 1

        # Best-effort send:
        if self.is_hub:
            if self._hub is not None and self._hub.running:
                sent_count = self._hub.submit_local_state(
                    key, version, body, frame, group_id=self._config.group_id
                )
                with self._state_stats_lock:
                    self._stat_sent += sent_count
        else:
            with self._link_lock:
                link = self._link
                if link is not None and link.connected:
                    try:
                        link.send(frame)
                        with self._state_stats_lock:
                            self._stat_sent += 1
                    except Exception:
                        pass

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
            return self._probe_url(url, timeout)

        peer = self._peer_by_id.get(target_id)
        if peer is None:
            return False

        discovered = (
            self._discovery.addresses_for(target_id)
            if self._discovery is not None
            else ()
        )
        last_good = self._last_good.get(target_id)
        candidates = ordered_addresses(peer, discovered=discovered, last_good=last_good)
        if not candidates:
            return False

        clock_fn = self._clock if self._clock is not None else time.monotonic
        start_time = clock_fn()

        for addr in candidates:
            elapsed = clock_fn() - start_time
            remaining = timeout - elapsed
            if remaining <= 0:
                break
            per_addr_timeout = min(self._config.address_timeout, remaining)
            url = addr.websocket_url
            if self._probe_url(url, per_addr_timeout):
                self._last_good.set(target_id, addr)
                if target_id == self._desired_hub and target_id != self.device_id:
                    self._ensure_link(target_id)
                return True

        return False

    def _handle_hub_changed(self, new_hub: str) -> None:
        old_hub = self._current_hub
        forced = self._election is not None and self._election.last_forced
        reason = "forced: no reachable device will_hub" if forced else "election probe"
        logger.info("Hub changed from %s to %s (%s)", old_hub, new_hub, reason)

        if new_hub == self.device_id:
            with self._link_lock:
                if self._link is not None:
                    self._link.close()
                    self._link = None
            # Publish the new role only after _current_hub is consistent: is_hub
            # keys off _desired_hub, so a stale hub_id here would be a visible
            # window in which is_hub is True while hub_id still names the old hub.
            self._current_hub = new_hub
            self._desired_hub = new_hub
        else:
            self._desired_hub = new_hub
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
            try:
                candidates = [parse_address(self._config.relay.address)]
            except ValueError:
                return
        else:
            peer = self._peer_by_id.get(hub)
            if peer is None:
                return
            discovered = (
                self._discovery.addresses_for(hub)
                if self._discovery is not None
                else ()
            )
            last_good = self._last_good.get(hub)
            candidates = ordered_addresses(peer, discovered=discovered, last_good=last_good)

        if not candidates:
            return

        candidate_urls = [c.websocket_url for c in candidates]

        with self._link_lock:
            if self._link is not None:
                if self._link.url in candidate_urls and self._link.connected:
                    matching_addr = next(
                        c for c in candidates if c.websocket_url == self._link.url
                    )
                    self._last_good.set(hub, matching_addr)
                    self._current_hub = hub
                    return
                self._link.close()
                self._link = None
                self._current_hub = None

            if not self._can_reach(self.device_id, hub):
                return

            clock_fn = self._clock if self._clock is not None else time.monotonic
            start_time = clock_fn()
            total_budget = self._config.connect_timeout

            for addr in candidates:
                elapsed = clock_fn() - start_time
                remaining = total_budget - elapsed
                if remaining <= 0:
                    break
                per_addr_timeout = min(self._config.address_timeout, remaining)
                target_url = addr.websocket_url

                link = Link(
                    url=target_url,
                    token=self._config.token,
                    hello=self._config.hello(),
                    max_frame_bytes=self._config.max_frame_bytes,
                    reconnect=True,
                    reconnect_delay=self._config.reconnect_delay,
                    connect_timeout=per_addr_timeout,
                    thread_name=f"meshcore-link-{self.device_id}",
                    on_roster=self._handle_client_roster,
                    on_payload=self._handle_client_payload,
                )
                # The callback runs on the link worker thread *inside* connect(),
                # while Node still holds _link_lock, so it must be given the link
                # directly and must not take that lock (deadlock otherwise).
                link.set_on_connected(lambda: self._on_link_connected(link))
                try:
                    link.connect()
                    self._link = link
                    self._current_hub = hub
                    self._last_good.set(hub, addr)
                    return
                except LinkError:
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

    def _on_link_connected(self, link: Link) -> None:
        if not link.connected:
            return

        try:
            link.send_control(encode_state_sync())
        except Exception:
            return

        for key, entry in self._state_register.items():
            if entry.frame_bytes is not None:
                frame = entry.frame_bytes
            else:
                if self._cipher is not None:
                    body = self._cipher.encrypt(entry.payload)
                else:
                    body = entry.payload
                try:
                    frame = encode_state_frame(key, entry.version, body)
                except Exception:
                    continue
            try:
                link.send(frame)
                with self._state_stats_lock:
                    self._stat_sent += 1
            except Exception:
                pass

    def _handle_client_payload(self, sender_id: str | None, payload: bytes) -> None:
        if payload.startswith(STATE_MAGIC):
            with self._state_stats_lock:
                self._stat_received += 1
            try:
                key, version, body = decode_state_frame(payload)
            except ValueError:
                with self._state_stats_lock:
                    self._stat_rejected_invalid += 1
                return

            now_ms = int(self._wall_clock() * 1000)
            if version.wall_ms > now_ms + MAX_CLOCK_AHEAD_S * 1000:
                with self._state_stats_lock:
                    self._stat_rejected_skew += 1
                self._log_skew_warning(key, version.author, version.wall_ms, now_ms)
                return

            if self._cipher is not None:
                try:
                    decrypted = self._cipher.decrypt(body)
                except MeshCryptoError:
                    with self._lifecycle_lock:
                        self._dropped_undecryptable += 1
                    return
                except Exception:
                    with self._lifecycle_lock:
                        self._dropped_undecryptable += 1
                    return
            else:
                decrypted = body

            self._hlc.accept(version.wall_ms, version.counter)
            won = self._state_register.merge(key, version, decrypted, frame_bytes=payload)
            if won:
                with self._state_stats_lock:
                    self._stat_applied += 1
                for cb in list(self._state_callbacks):
                    try:
                        cb(key, version.author, decrypted)
                    except Exception:
                        pass
            else:
                with self._state_stats_lock:
                    self._stat_ignored += 1
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

    def _on_hub_state_won(
        self, key: str, version: StateVersion, body: bytes, frame: bytes
    ) -> None:
        if self._cipher is not None:
            try:
                decrypted = self._cipher.decrypt(body)
            except MeshCryptoError:
                with self._lifecycle_lock:
                    self._dropped_undecryptable += 1
                return
            except Exception:
                with self._lifecycle_lock:
                    self._dropped_undecryptable += 1
                return
        else:
            decrypted = body

        self._state_register.force_set(
            key, StateEntry(version, decrypted, frame_bytes=frame)
        )
        self._hlc.accept(version.wall_ms, version.counter)
        with self._state_stats_lock:
            self._stat_applied += 1

        for cb in list(self._state_callbacks):
            try:
                cb(key, version.author, decrypted)
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
