#!/usr/bin/env python3
"""CLI utility to verify LAN peer discovery between machines."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meshcore.discovery import (
    DEFAULT_DISCOVERY_PORT,
    MULTICAST_GROUP,
    create_udp_socket,
    decode_beacon,
    encode_beacon,
    local_broadcast_addresses,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify LAN UDP discovery between devices without internet."
    )
    parser.add_argument(
        "mode",
        choices=["listen", "probe"],
        help="Run as listener or probe (both send and receive beacons)",
    )
    parser.add_argument(
        "--group",
        "-g",
        required=True,
        help="Group identifier",
    )
    parser.add_argument(
        "--token",
        "-t",
        required=True,
        help="Group shared secret token",
    )
    parser.add_argument(
        "--duration",
        "-d",
        type=float,
        default=20.0,
        help="Run duration in seconds before reporting PASS/FAIL (default 20.0)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=DEFAULT_DISCOVERY_PORT,
        help=f"Discovery UDP port to bind (default {DEFAULT_DISCOVERY_PORT})",
    )
    parser.add_argument(
        "--hub-port",
        type=int,
        default=8765,
        help="Hub port to advertise in outgoing beacons (default 8765)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Beacon announcement interval in seconds (default 2.0)",
    )
    parser.add_argument(
        "--device-id",
        type=str,
        default=None,
        help="Local device ID (default random generated)",
    )

    args = parser.parse_args()

    device_id = args.device_id or f"{args.mode}-{uuid.uuid4().hex[:8]}"
    print(
        f"Starting {args.mode} as device '{device_id}' for group '{args.group}' "
        f"on discovery port {args.port} (duration: {args.duration}s)..."
    )

    destinations: list[str] = []
    for addr in local_broadcast_addresses() + ["255.255.255.255", MULTICAST_GROUP]:
        if addr and addr not in destinations:
            destinations.append(addr)

    sock = create_udp_socket(args.port)
    start_time = time.time()
    deadline = start_time + max(0.1, args.duration)
    last_send = 0.0

    other_beacons: int = 0
    first_sighting_s: float | None = None

    def send_beacon() -> None:
        nonlocal last_send
        datagram = encode_beacon(
            group=args.group,
            device_id=device_id,
            port=args.hub_port,
            ts=time.time(),
            token=args.token,
        )
        for dest in destinations:
            try:
                sock.send(datagram, (dest, args.port))
            except OSError:
                pass
        last_send = time.time()

    send_beacon()

    try:
        while time.time() < deadline:
            now = time.time()
            if now - last_send >= args.interval:
                send_beacon()

            remaining = deadline - now
            if remaining <= 0:
                break

            poll_timeout = min(0.1, remaining)
            res = sock.recv(poll_timeout)
            if res is not None:
                data, source_ip = res
                beacon = decode_beacon(data, args.token, now=time.time())
                if beacon is not None:
                    if beacon.device_id != device_id and beacon.group == args.group:
                        other_beacons += 1
                        elapsed = time.time() - start_time
                        if first_sighting_s is None:
                            first_sighting_s = elapsed
                            print(
                                f"Beacon heard from {source_ip}: device_id={beacon.device_id} "
                                f"group={beacon.group} port={beacon.port} "
                                f"[first sighting at {elapsed:.2f}s]"
                            )
                        else:
                            print(
                                f"Beacon heard from {source_ip}: device_id={beacon.device_id} "
                                f"group={beacon.group} port={beacon.port} "
                                f"[{elapsed:.2f}s]"
                            )
    finally:
        sock.close()

    if other_beacons > 0 and first_sighting_s is not None:
        print(
            f"PASS: received {other_beacons} beacon(s) from other devices "
            f"(first sighting at {first_sighting_s:.2f}s)"
        )
        return 0
    else:
        print(f"FAIL: no valid beacons from other devices seen within {args.duration}s")
        return 1


if __name__ == "__main__":
    sys.exit(main())
