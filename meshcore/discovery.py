"""LAN UDP broadcast and multicast peer discovery for meshcore."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import math
import os
import select
import socket
import struct
import threading
import time
from typing import Any, Callable, Iterable, Protocol

from meshcore.addressing import Address

__all__ = [
    "BEACON_VERSION",
    "DEFAULT_BEACON_INTERVAL",
    "DEFAULT_DISCOVERY_PORT",
    "DEFAULT_MAX_AGE",
    "MULTICAST_GROUP",
    "Beacon",
    "Discovery",
    "DiscoverySocket",
    "beacon_mac",
    "create_udp_socket",
    "decode_beacon",
    "encode_beacon",
    "local_broadcast_addresses",
]

logger = logging.getLogger("meshcore.discovery")

MULTICAST_GROUP: str = "239.255.76.67"
DEFAULT_DISCOVERY_PORT: int = 8766
BEACON_VERSION: int = 1
DEFAULT_BEACON_INTERVAL: float = 5.0
DEFAULT_MAX_AGE: float = 30.0


@dataclass(frozen=True)
class Beacon:
    """Decoded, authenticated discovery beacon."""

    group: str
    device_id: str
    port: int
    ts: float


def beacon_mac(group: str, device_id: str, port: int, ts: float, token: str) -> str:
    """Compute HMAC-SHA256 hex digest over canonical JSON fields."""
    canonical = json.dumps(
        {
            "device_id": str(device_id),
            "group": str(group),
            "port": int(port),
            "ts": float(ts),
            "v": BEACON_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(token.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def encode_beacon(
    *,
    group: str,
    device_id: str,
    port: int,
    ts: float,
    token: str,
) -> bytes:
    """Encode an authenticated beacon to compact UTF-8 JSON bytes."""
    mac = beacon_mac(group, device_id, port, ts, token)
    payload = {
        "v": BEACON_VERSION,
        "group": str(group),
        "device_id": str(device_id),
        "port": int(port),
        "ts": float(ts),
        "mac": mac,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def decode_beacon(
    data: Any,
    token: str,
    *,
    now: float,
    max_age: float = DEFAULT_MAX_AGE,
) -> Beacon | None:
    """Decode and authenticate a beacon datagram. Never raises; returns None on failure."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    try:
        raw_text = bytes(data).decode("utf-8")
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            return None

        v = payload.get("v")
        if isinstance(v, bool) or v != BEACON_VERSION:
            return None

        group = payload.get("group")
        if not isinstance(group, str) or not group:
            return None

        device_id = payload.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            return None

        port = payload.get("port")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not (1 <= port <= 65535)
        ):
            return None

        ts = payload.get("ts")
        if (
            isinstance(ts, bool)
            or not isinstance(ts, (int, float))
            or not math.isfinite(ts)
        ):
            return None
        ts = float(ts)

        mac = payload.get("mac")
        if not isinstance(mac, str) or not mac:
            return None

        if abs(now - ts) > max_age:
            return None

        expected_mac = beacon_mac(group, device_id, port, ts, token)
        if not hmac.compare_digest(mac, expected_mac):
            return None

        return Beacon(group=group, device_id=device_id, port=port, ts=ts)
    except Exception:
        return None


class DiscoverySocket(Protocol):
    """Protocol for discovery transport sockets."""

    def send(self, data: bytes, dest: tuple[str, int]) -> None:
        """Send datagram to destination (host, port)."""
        ...

    def recv(self, timeout: float) -> tuple[bytes, str] | None:
        """Receive next datagram (bytes, source_ip) within timeout, or None."""
        ...

    def close(self) -> None:
        """Close the socket."""
        ...


def local_broadcast_addresses() -> list[str]:
    """Return local IPv4 broadcast addresses across network interfaces (stdlib only)."""
    broadcasts: set[str] = set()
    try:
        import fcntl

        SIOCGIFBRDADDR = 0x8919
        SIOCGIFFLAGS = 0x8913
        IFF_UP = 0x1
        IFF_BROADCAST = 0x2
        IFF_LOOPBACK = 0x8

        interfaces: list[str] = []
        if hasattr(socket, "if_nameindex"):
            interfaces = [name for _, name in socket.if_nameindex()]
        elif os.path.exists("/proc/net/dev"):
            with open("/proc/net/dev", "r", encoding="utf-8") as f:
                for line in f.readlines()[2:]:
                    parts = line.split(":")
                    if parts:
                        interfaces.append(parts[0].strip())

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for ifname in interfaces:
                try:
                    ifreq = struct.pack("256s", ifname.encode("utf-8")[:15])
                    flags_res = fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, ifreq)
                    flags = struct.unpack("H", flags_res[16:18])[0]
                    if (
                        not (flags & IFF_UP)
                        or not (flags & IFF_BROADCAST)
                        or (flags & IFF_LOOPBACK)
                    ):
                        continue

                    brd_res = fcntl.ioctl(s.fileno(), SIOCGIFBRDADDR, ifreq)
                    ip = socket.inet_ntoa(brd_res[20:24])
                    if ip and ip != "0.0.0.0":
                        broadcasts.add(ip)
                except OSError:
                    continue
    except Exception:
        pass
    return sorted(broadcasts)


class _UdpDiscoverySocket:
    """Real UDP broadcast and multicast discovery socket."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._sock = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            with contextlib.suppress(OSError):
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        try:
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(MULTICAST_GROUP),
                socket.inet_aton("0.0.0.0"),
            )
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self._sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 1)
            )
        except OSError:
            pass

        self._sock.bind(("", port))
        self._closed = False
        self._lock = threading.Lock()

    def send(self, data: bytes, dest: tuple[str, int]) -> None:
        if self._closed:
            return
        host, port = dest
        if host:
            self._sock.sendto(data, (host, port))
        else:
            dests = list(local_broadcast_addresses())
            if "255.255.255.255" not in dests:
                dests.append("255.255.255.255")
            if MULTICAST_GROUP not in dests:
                dests.append(MULTICAST_GROUP)
            for h in dests:
                try:
                    self._sock.sendto(data, (h, port))
                except OSError:
                    pass

    def recv(self, timeout: float) -> tuple[bytes, str] | None:
        if self._closed:
            return None
        r, _, _ = select.select([self._sock], [], [], max(0.0, timeout))
        if not r or self._closed:
            return None
        try:
            data, (host, _port) = self._sock.recvfrom(65535)
            return (data, host)
        except OSError:
            return None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._sock.close()
                except OSError:
                    pass


def create_udp_socket(port: int) -> DiscoverySocket:
    """Create a real UDP broadcast and multicast DiscoverySocket."""
    return _UdpDiscoverySocket(port)


class Discovery:
    """LAN discovery driver for announcing and finding peers."""

    def __init__(
        self,
        *,
        group_id: str,
        device_id: str,
        token: str,
        port: int,
        socket: DiscoverySocket,
        peers: Iterable[str],
        clock: Callable[[], float] = time.monotonic,
        interval: float = DEFAULT_BEACON_INTERVAL,
        max_age: float = DEFAULT_MAX_AGE,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        broadcast_addresses: list[str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._group_id: str = group_id
        self._device_id: str = device_id
        self._token: str = token
        self._port: int = port
        self._discovery_port: int = int(discovery_port)
        self._socket: DiscoverySocket = socket
        self._peers: set[str] = set(peers)
        self._clock: Callable[[], float] = clock
        self._interval: float = interval
        self._max_age: float = max_age
        self._logger: logging.Logger = (
            logger if logger is not None else logging.getLogger("meshcore.discovery")
        )

        if broadcast_addresses is not None:
            raw_broadcasts = list(broadcast_addresses)
        else:
            raw_broadcasts = local_broadcast_addresses()

        destinations: list[str] = []
        for addr in raw_broadcasts + ["255.255.255.255", MULTICAST_GROUP]:
            if addr and addr not in destinations:
                destinations.append(addr)
        self._destinations: list[str] = destinations
        self._logger.info("Discovery destinations: %s", self._destinations)

        self._entries: dict[str, tuple[Address, float]] = {}
        self._accepted_beacons: int = 0
        self._ignored_beacons: int = 0
        self._send_errors: int = 0
        self._warned_send_error: bool = False

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started: bool = False
        self._stopped: bool = False

    @property
    def accepted_beacons(self) -> int:
        with self._lock:
            return self._accepted_beacons

    @property
    def ignored_beacons(self) -> int:
        with self._lock:
            return self._ignored_beacons

    @property
    def send_errors(self) -> int:
        with self._lock:
            return self._send_errors

    def addresses_for(self, device_id: str) -> list[Address]:
        """Return currently valid discovered addresses (kind 'lan') for device_id."""
        with self._lock:
            entry = self._entries.get(device_id)
            if entry is None:
                return []
            addr, last_seen = entry
            if self._clock() - last_seen > (3 * self._interval):
                del self._entries[device_id]
                return []
            return [addr]

    def announce(self) -> None:
        """Send an authenticated beacon to all discovery destinations."""
        datagram = encode_beacon(
            group=self._group_id,
            device_id=self._device_id,
            port=self._port,
            ts=self._clock(),
            token=self._token,
        )
        for dest in self._destinations:
            try:
                self._socket.send(datagram, (dest, self._discovery_port))
            except OSError as exc:
                with self._lock:
                    self._send_errors += 1
                    if not self._warned_send_error:
                        self._warned_send_error = True
                        self._logger.warning("Discovery send error: %s", exc)

    def poll_once(self, timeout: float = 0.0) -> int:
        """Poll the socket for incoming datagrams. Returns count of datagrams processed."""
        res = self._socket.recv(timeout)
        if res is None:
            return 0
        data, source_ip = res
        self._process_datagram(data, source_ip)
        return 1

    def _process_datagram(self, data: bytes, source_ip: str) -> None:
        now = self._clock()
        beacon = decode_beacon(data, self._token, now=now, max_age=self._max_age)
        with self._lock:
            if beacon is None:
                self._ignored_beacons += 1
                return

            if beacon.device_id == self._device_id:
                # Self beacons are ignored without being counted as bad
                return

            if beacon.group != self._group_id:
                self._ignored_beacons += 1
                return

            if beacon.device_id not in self._peers:
                self._ignored_beacons += 1
                return

            self._accepted_beacons += 1
            addr = Address("lan", source_ip, beacon.port)
            self._entries[beacon.device_id] = (addr, now)

    def start(self) -> None:
        """Start announcement and listening thread. Idempotent."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopped = False
            self._stop_event.clear()
            self.announce()
            self._thread = threading.Thread(
                target=self._run,
                name="meshcore-discovery",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop discovery thread and close socket. Idempotent, bounded join."""
        with self._lock:
            if self._stopped or not self._started:
                self._stopped = True
                return
            self._stopped = True
            self._started = False
            self._stop_event.set()
            try:
                self._socket.close()
            except Exception:
                pass
            thread = self._thread
            self._thread = None

        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        last_announce = self._clock()
        while not self._stop_event.is_set():
            now = self._clock()
            if now - last_announce >= self._interval:
                self.announce()
                last_announce = now

            time_to_announce = max(0.001, self._interval - (self._clock() - last_announce))
            poll_timeout = min(0.05, time_to_announce)
            try:
                self.poll_once(timeout=poll_timeout)
            except Exception:
                pass
