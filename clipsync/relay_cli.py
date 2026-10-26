"""Console entry point and runner for the clipsync loopback relay server."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable, Sequence

from .relay_server import RELAY_MAX_FRAME_BYTES, RelayServer

__all__ = ["RelayConfig", "build_server", "run_relay", "main"]

logger = logging.getLogger("clipsync.relay_cli")


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Configuration for the loopback WebSocket relay server."""

    token: str
    port: int = 8765
    max_frame_bytes: int = RELAY_MAX_FRAME_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or len(self.token) == 0:
            raise ValueError("token must be a non-empty string")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or self.port < 0
            or self.port > 65535
        ):
            raise ValueError(
                f"port must be an integer between 0 and 65535, got {self.port!r}"
            )
        if (
            isinstance(self.max_frame_bytes, bool)
            or not isinstance(self.max_frame_bytes, int)
            or self.max_frame_bytes <= 0
        ):
            raise ValueError(
                f"max_frame_bytes must be a positive integer, got {self.max_frame_bytes!r}"
            )


def build_server(config: RelayConfig) -> RelayServer:
    """Build a loopback RelayServer from config."""
    return RelayServer(
        token=config.token,
        port=config.port,
        max_frame_bytes=config.max_frame_bytes,
    )


def run_relay(
    config: RelayConfig,
    *,
    stop_event: threading.Event | None = None,
    on_started: Callable[[RelayServer], None] | None = None,
) -> int:
    """Run the loopback relay server until stop_event is set or interrupted.

    Parameters
    ----------
    config:
        RelayConfig parameters.
    stop_event:
        Optional threading.Event to signal shutdown.
    on_started:
        Optional callback called with the running server instance once listening.

    Returns
    -------
    int:
        0 on clean exit, 1 on bind/startup OSError.
    """
    server = build_server(config)
    try:
        server.start()
    except OSError as exc:
        sys.stderr.write(f"Relay server bind error: {exc}\n")
        return 1

    try:
        sys.stdout.write(f"clipsync-relay listening on ws://127.0.0.1:{server.port}/\n")
        sys.stdout.flush()

        if on_started is not None:
            on_started(server)

        try:
            if stop_event is None:
                while True:
                    time.sleep(0.5)
            else:
                while not stop_event.is_set():
                    stop_event.wait(timeout=0.2)
        except KeyboardInterrupt:
            pass
    finally:
        server.stop()

    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="clipsync-relay",
        description="Loopback WebSocket relay server for clipsync.",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Shared bearer token for client authentication.",
    )
    parser.add_argument(
        "--token-file",
        type=str,
        default=None,
        help="Path to file containing the shared bearer token.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Loopback TCP port to bind (default: 8765, 0 for ephemeral).",
    )
    parser.add_argument(
        "--max-frame-bytes",
        type=int,
        default=RELAY_MAX_FRAME_BYTES,
        help=f"Maximum allowed frame size in bytes (default: {RELAY_MAX_FRAME_BYTES}).",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="clipsync-relay 0.1.0",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    stop_event: threading.Event | None = None,
    on_started: Callable[[RelayServer], None] | None = None,
) -> int:
    """CLI entry point for clipsync-relay.

    Returns exit code (0 on success, 1 on bind error, 2 on bad arguments or config).
    """
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )

    # Exactly one of --token or --token-file
    has_token = args.token is not None
    has_token_file = args.token_file is not None
    if has_token == has_token_file:
        sys.stderr.write("Exactly one of --token or --token-file must be specified.\n")
        return 2

    token: str
    if has_token:
        token = args.token.strip()
        if not token:
            sys.stderr.write("Token cannot be empty.\n")
            return 2
    else:
        token_path = Path(args.token_file).expanduser()
        try:
            content = token_path.read_text(encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"Failed to read token file {token_path}: {exc}\n")
            return 2
        token = content.strip()
        if not token:
            sys.stderr.write(f"Token file {token_path} is empty.\n")
            return 2

    try:
        config = RelayConfig(
            token=token,
            port=args.port,
            max_frame_bytes=args.max_frame_bytes,
        )
    except ValueError as exc:
        sys.stderr.write(f"Invalid relay configuration: {exc}\n")
        return 2

    if stop_event is None:
        local_stop = threading.Event()

        def _sig_handler(_signum: int, _frame: Any) -> None:
            local_stop.set()

        try:
            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)
        except (ValueError, AttributeError):
            pass

        return run_relay(config, stop_event=local_stop, on_started=on_started)

    return run_relay(config, stop_event=stop_event, on_started=on_started)


if __name__ == "__main__":
    sys.exit(main())
