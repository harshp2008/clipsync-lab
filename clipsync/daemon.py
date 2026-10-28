"""Clipboard daemon loop and CLI entry point.

Connects a clipboard backend to a transport relay and runs the sync loop.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import dataclasses
import json
import logging
import math
from pathlib import Path
import random
import signal
import sys
import threading
import time
from typing import Any, Callable, Sequence

from .clipboard import ClipboardBackend, WlClipboardBackend
from .config import ConfigError, DaemonConfig, load_config
from .crypto import Codec, FernetCodec
from .engine import SyncEngine, SyncStats
from .relay_transport import RelayTransport
from .transport import Transport, TransportError

__all__ = [
    "BackoffPolicy",
    "ClipboardDaemon",
    "build_clipboard",
    "build_codec",
    "build_transport",
    "main",
    "wait_or_stop",
]

logger = logging.getLogger("clipsync.daemon")


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Capped exponential backoff with configurable jitter."""

    base: float = 0.2
    max_delay: float = 30.0
    jitter: float = 0.5

    def __post_init__(self) -> None:
        if isinstance(self.base, bool) or not isinstance(self.base, (int, float)):
            raise ValueError(f"base must be a number, got {self.base!r}")
        if math.isnan(self.base) or math.isinf(self.base) or self.base <= 0:
            raise ValueError(f"base must be a positive finite number, got {self.base!r}")

        if isinstance(self.max_delay, bool) or not isinstance(self.max_delay, (int, float)):
            raise ValueError(f"max_delay must be a number, got {self.max_delay!r}")
        if math.isnan(self.max_delay) or math.isinf(self.max_delay) or self.max_delay < self.base:
            raise ValueError(
                f"max_delay must be >= base and finite, got max_delay={self.max_delay!r}, base={self.base!r}"
            )

        if isinstance(self.jitter, bool) or not isinstance(self.jitter, (int, float)):
            raise ValueError(f"jitter must be a number, got {self.jitter!r}")
        if math.isnan(self.jitter) or math.isinf(self.jitter) or not (0.0 <= self.jitter <= 1.0):
            raise ValueError(f"jitter must be between 0.0 and 1.0, got {self.jitter!r}")

    def delay(self, attempt: int, *, rand: Callable[[], float] = random.random) -> float:
        """Compute delay for a given attempt index (0-based)."""
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise ValueError(f"attempt must be a non-negative int, got {attempt!r}")

        if attempt >= 64:
            raw = float(self.max_delay)
        else:
            raw = min(float(self.max_delay), float(self.base) * (2.0 ** attempt))

        r = min(1.0, max(0.0, float(rand())))
        factor = 1.0 - self.jitter + 2.0 * self.jitter * r
        return min(float(self.max_delay), raw * factor)


def wait_or_stop(
    stop_event: threading.Event | None,
    delay: float,
    sleep: Callable[[float], None] = time.sleep,
    *,
    slice_seconds: float = 0.05,
) -> bool:
    """Sleep for delay seconds or until stop_event is set.

    Returns True if stopped, False if full delay elapsed.
    """
    if delay < 0:
        delay = 0.0

    if stop_event is None:
        sleep(delay)
        return False

    if stop_event.is_set():
        return True

    if slice_seconds <= 0:
        slice_seconds = 0.05

    remaining = delay
    while remaining > 1e-9:
        if stop_event.is_set():
            return True
        step = min(remaining, slice_seconds)
        sleep(step)
        remaining -= step
        if stop_event.is_set():
            return True

    return False


class ClipboardDaemon:
    """Daemon running a :class:`SyncEngine` over a clipboard backend and transport."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        clipboard: ClipboardBackend,
        transport: Transport,
        codec: Codec | None = None,
        backoff: BackoffPolicy | None = None,
    ) -> None:
        self._config = config
        self._clipboard = clipboard
        self._transport = transport
        self._codec = codec
        if backoff is None:
            self.backoff = BackoffPolicy(
                base=config.reconnect_delay,
                max_delay=30.0,
                jitter=0.5,
            )
        else:
            self.backoff = backoff
        self._engine = SyncEngine(
            device_id=config.device_id,
            clipboard=clipboard,
            transport=transport,
            codec=codec,
        )

    def connect(self) -> None:
        """Connect the underlying transport to the relay."""
        self._transport.connect()

    def connect_resilient(
        self,
        *,
        stop_event: threading.Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int | None = None,
    ) -> bool:
        """Connect transport with capped exponential backoff and jitter.

        Returns True on success, False if stopped or max_attempts reached.
        Never raises TransportError.
        """
        if isinstance(max_attempts, bool):
            raise ValueError("max_attempts must be an integer, not a bool")
        if max_attempts is not None:
            if not isinstance(max_attempts, int) or max_attempts < 0:
                raise ValueError(
                    f"max_attempts must be a non-negative integer or None, got {max_attempts!r}"
                )
            if max_attempts == 0:
                return False

        attempts = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                return False

            try:
                self._transport.connect()
                return True
            except TransportError as exc:
                logger.warning("Relay connection failed: %s", exc)
                attempts += 1
                if max_attempts is not None and attempts >= max_attempts:
                    return False
                delay = self.backoff.delay(attempts - 1)
                if wait_or_stop(stop_event, delay, sleep=sleep):
                    return False

    def poll_once(self) -> SyncStats:
        """Execute a single sync iteration and return stats."""
        return self._engine.poll_once()

    def run(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        """Run the daemon loop until stopped or maximum iterations reached.

        Parameters
        ----------
        stop_event:
            Optional threading.Event to request clean shutdown.
        max_iterations:
            Optional non-negative int maximum iterations to execute.
        sleep:
            Callable invoked between iterations with sleep duration.

        Returns
        -------
        int:
            Number of iterations executed.
        """
        if isinstance(max_iterations, bool):
            raise ValueError("max_iterations must be an integer, not a bool")
        if max_iterations is not None:
            if not isinstance(max_iterations, int) or max_iterations < 0:
                raise ValueError(
                    f"max_iterations must be a non-negative integer or None, got {max_iterations!r}"
                )
            if max_iterations == 0:
                return 0

        count = 0
        consecutive_failures = 0

        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if max_iterations is not None and count >= max_iterations:
                break

            if not self._transport.connected:
                try:
                    self._transport.connect()
                except TransportError as exc:
                    logger.warning("Transport connect failed: %s", exc)
                    delay = self.backoff.delay(consecutive_failures)
                    consecutive_failures += 1
                    count += 1
                    if wait_or_stop(stop_event, delay, sleep=sleep):
                        break
                    continue
                else:
                    consecutive_failures = 0

            try:
                self.poll_once()
            except TransportError as exc:
                logger.warning("Transport error during poll: %s", exc)
                delay = self.backoff.delay(consecutive_failures)
                consecutive_failures += 1
                count += 1
                if wait_or_stop(stop_event, delay, sleep=sleep):
                    break
                continue

            if not self._transport.connected:
                logger.warning("Transport disconnected after poll")
                delay = self.backoff.delay(consecutive_failures)
                consecutive_failures += 1
                count += 1
                if wait_or_stop(stop_event, delay, sleep=sleep):
                    break
                continue

            consecutive_failures = 0
            count += 1

            if stop_event is not None and stop_event.is_set():
                break
            if max_iterations is not None and count >= max_iterations:
                break

            if wait_or_stop(stop_event, self._config.poll_interval, sleep=sleep):
                break

        return count

    def close(self) -> None:
        """Idempotently close the underlying transport."""
        self._transport.close()


def build_codec(config: DaemonConfig) -> Codec | None:
    """Build a FernetCodec from config if an e2e_key is specified, else None."""
    if config.e2e_key is None:
        return None
    return FernetCodec(config.e2e_key)


def build_clipboard(config: DaemonConfig) -> ClipboardBackend:
    """Build the default Wayland clipboard backend."""
    del config
    return WlClipboardBackend()


def build_transport(config: DaemonConfig) -> Transport:
    """Build the default RelayTransport client."""
    return RelayTransport(
        config.relay_url,
        config.token,
        reconnect_delay=config.reconnect_delay,
        connect_timeout=config.connect_timeout,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    import clipsync

    parser = argparse.ArgumentParser(
        prog="clipsync",
        description="Two-way clipboard synchronization daemon.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="~/.config/clipsync/config.toml",
        help="Path to TOML configuration file (default: ~/.config/clipsync/config.toml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once, print stats JSON to stdout, and exit.",
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
        version=f"%(prog)s {clipsync.__version__}",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    clipboard_factory: Callable[[DaemonConfig], ClipboardBackend] | None = None,
    transport_factory: Callable[[DaemonConfig], Transport] | None = None,
    codec_factory: Callable[[DaemonConfig], Codec | None] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """CLI entry point for the clipsync daemon.

    Returns exit code (0 on success, 1 on transport error, 2 on config/CLI error).
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

    try:
        config_path = Path(args.config).expanduser()
        config = load_config(config_path)
    except ConfigError as exc:
        sys.stderr.write(f"Configuration error: {exc}\n")
        return 2

    make_clipboard = clipboard_factory or build_clipboard
    if transport_factory is not None:
        make_transport = transport_factory
    elif config.peers:
        from meshcore.node import Node
        from .mesh_transport import NodeTransport

        def _make_mesh_transport(cfg: DaemonConfig) -> Transport:
            node = Node(cfg.to_mesh_config())
            return NodeTransport(node)

        make_transport = _make_mesh_transport
    else:
        make_transport = build_transport
    make_codec = codec_factory or build_codec

    clipboard = make_clipboard(config)
    transport = make_transport(config)
    codec = make_codec(config)

    daemon = ClipboardDaemon(
        config,
        clipboard=clipboard,
        transport=transport,
        codec=codec,
    )

    if args.once:
        try:
            daemon.connect()
        except TransportError as exc:
            sys.stderr.write(f"Transport connection error: {exc}\n")
            daemon.close()
            return 1

        try:
            stats = daemon.poll_once()
            stats_json = json.dumps(dataclasses.asdict(stats), separators=(",", ":"))
            sys.stdout.write(stats_json + "\n")
            sys.stdout.flush()
        finally:
            daemon.close()
        return 0

    if stop_event is None:
        local_stop = threading.Event()

        def _sig_handler(_signum: int, _frame: Any) -> None:
            local_stop.set()

        try:
            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)
        except (ValueError, AttributeError):
            pass
        run_stop = local_stop
    else:
        run_stop = stop_event

    try:
        daemon.run(stop_event=run_stop)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
