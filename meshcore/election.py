"""Election driver for meshcore."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

from meshcore.coordination import Device, HubTracker, probe_hub
from meshcore.messages import Hello

__all__ = ["Election"]

logger = logging.getLogger("meshcore.election")


class Election:
    """Driver around coordination.probe_hub and HubTracker with injected seams."""

    def __init__(
        self,
        self_hello: Hello,
        peers: Iterable[Hello | Device],
        *,
        ping: Callable[[str, float], bool],
        clock: Callable[[], float] = time.monotonic,
        heartbeat: float = 10.0,
        missed_pongs: int = 3,
        probe_timeout: float = 2.0,
        debounce: float = 30.0,
        preferred_id: str | None = None,
        on_hub_changed: Callable[[str], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._self_hello: Hello = self_hello
        self._peers: list[Hello | Device] = list(peers)
        self._ping: Callable[[str, float], bool] = ping
        self._clock: Callable[[], float] = clock
        self._heartbeat: float = heartbeat
        self._missed_pongs_limit: int = missed_pongs
        self._probe_timeout: float = probe_timeout
        self._debounce: float = debounce
        self._preferred_id: str | None = preferred_id
        self._on_hub_changed: Callable[[str], None] | None = on_hub_changed
        self._logger: logging.Logger = logger if logger is not None else logging.getLogger("meshcore.election")

        self._tracker: HubTracker = HubTracker(self._clock, debounce=self._debounce)
        self._consecutive_missed_pongs: int = 0
        self._forced_logged_for: str | None = None
        self._last_forced: bool = False
        self._current_hub: str | None = None

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def current_hub(self) -> str | None:
        """Current elected hub ID, or None if no hub elected yet."""
        with self._lock:
            return self._tracker.current_hub

    @property
    def last_forced(self) -> bool:
        """Whether the most recent probe had to force a hub (all declined)."""
        with self._lock:
            return self._last_forced

    def set_devices(self, hellos: Iterable[Hello | Device]) -> None:
        """Replace the peer candidate list."""
        with self._lock:
            self._peers = list(hellos)

    def start(self) -> None:
        """Run one election immediately and start background periodic election thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self.run_once()
            self._thread = threading.Thread(
                target=self._loop,
                name="meshcore-election",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop periodic election thread and join it. Idempotent."""
        self._stop_event.set()
        thread = None
        with self._lock:
            if self._thread is not None:
                thread = self._thread
                self._thread = None
        if thread is not None and thread.is_alive():
            thread.join()

    def run_once(self) -> None:
        """Perform one election cycle."""
        with self._lock:
            current_hub = self._tracker.current_hub
            all_devices_dict: dict[str, Any] = {}
            # Self is always a candidate
            all_devices_dict[self._self_hello.device_id] = self._self_hello
            for p in self._peers:
                all_devices_dict[p.device_id] = p
            all_devices = list(all_devices_dict.values())

            # Missed pongs tracking wrapper around ping
            missed_counter = [self._consecutive_missed_pongs]

            def election_ping(device_id: str, timeout: float) -> bool:
                res = self._ping(device_id, timeout)
                if device_id == current_hub and device_id != self._self_hello.device_id:
                    if res:
                        missed_counter[0] = 0
                        return True
                    missed_counter[0] += 1
                    if missed_counter[0] < self._missed_pongs_limit:
                        # Still considered alive until missed_pongs limit reached
                        return True
                    return False
                return res

            if self._preferred_id is not None and election_ping(self._preferred_id, self._probe_timeout):
                candidate = self._preferred_id
                forced = False
            else:
                candidate, forced = probe_hub(
                    all_devices,
                    election_ping,
                    self._probe_timeout,
                    self._self_hello.device_id,
                )

            self._consecutive_missed_pongs = missed_counter[0]
            self._last_forced = forced

            if forced:
                if self._forced_logged_for != candidate:
                    self._logger.warning(
                        "All reachable devices decline will_hub; forced hub: %s",
                        candidate,
                    )
                    self._forced_logged_for = candidate
            else:
                self._forced_logged_for = None

            old_hub = self._tracker.current_hub
            self._tracker.observe(candidate)
            new_hub = self._tracker.current_hub

            changed = (new_hub is not None and new_hub != old_hub) or (old_hub is None and new_hub is not None)
            callback = self._on_hub_changed if changed else None

        if callback is not None and new_hub is not None:
            try:
                callback(new_hub)
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._heartbeat)
            if self._stop_event.is_set():
                break
            try:
                self.run_once()
            except Exception as exc:
                self._logger.debug("Election error in loop: %s", exc)
