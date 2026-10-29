"""Two-way sync engine.

Ties a :class:`~clipsync.clipboard.ClipboardBackend` to a
:class:`~clipsync.transport.Transport` and moves payloads in both directions without
echo loops, duplicate writes, or regression from out-of-order/duplicated frames.

Invariants
----------
* After applying a remote payload the local clipboard holds exactly that payload, so
  the next local read sees ``_last_local_hash`` and cannot re-send it (echo break).
* Frames from our own ``device_id`` are dropped (loopback defence).
* Per origin, an applied ``seq`` never goes backwards; stale frames are dropped.
* A frame is applied at most once (``msg_id`` dedup) and identical content already on
  the clipboard is not written again.
* Frames that fail to send stay at the front of the outbox and are retried on the next
  poll, so a reconnect (or transient transport error) loses nothing.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Deque

from .clipboard import CLIPBOARD_FAILURES, ClipboardBackend
from .crypto import Codec, CryptoError
from .protocol import (
    ClipMessage,
    PayloadTooLarge,
    ValidationError,
    decode_frame,
    encode_frame,
)
from .transport import FrameTooLarge, Transport, TransportError

__all__ = ["SyncEngine", "SyncStats"]

_MAX_DEVICE_ID = 128


@dataclass
class SyncStats:
    """Counters for one :meth:`SyncEngine.poll_once` call (cumulative per call).

    In relay mode, ``sent`` counts frames handed to the transport relay.
    In state channel mode, ``sent`` counts frames published to the channel.
    """

    sent: int = 0
    received: int = 0
    applied: int = 0
    dropped_loop: int = 0
    dropped_dup: int = 0
    dropped_stale: int = 0
    dropped_invalid: int = 0
    rejected_too_large: int = 0
    send_errors: int = 0
    recv_errors: int = 0
    clipboard_errors: int = 0
    queued: int = 0


class SyncEngine:
    """Push local clipboard changes out, apply remote ones in, safely."""

    def __init__(
        self,
        device_id: str,
        clipboard: ClipboardBackend,
        transport: Transport,
        codec: Codec | None = None,
        *,
        seen_capacity: int = 512,
        max_outbox: int = 64,
        clock: Callable[[], float] = time.time,
        seq_base: int | None = None,
    ) -> None:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must be a non-empty string")
        if len(device_id) > _MAX_DEVICE_ID:
            raise ValueError(f"device_id must be at most {_MAX_DEVICE_ID} chars")
        if seen_capacity < 1:
            raise ValueError("seen_capacity must be >= 1")
        if max_outbox < 1:
            raise ValueError("max_outbox must be >= 1")

        self.device_id = device_id
        self._clipboard = clipboard
        self._transport = transport
        self._codec = codec
        self._clock = clock
        self._seen_capacity = seen_capacity
        self._max_outbox = max_outbox

        # msg_id -> time first seen; OrderedDict as a bounded LRU-ish set.
        self._seen_ids: OrderedDict[str, float] = OrderedDict()
        self._last_seq: dict[str, int] = {}
        self._last_local_hash: str | None = None
        self.last_clipboard_error: str | None = None
        self._outbox: Deque[bytes] = deque()
        self._seq_gaps = 0
        self._overflow_dropped = 0
        self._next_seq = int(seq_base) if seq_base is not None else int(clock() * 1000)

    # -- public API -----------------------------------------------------------
    def poll_once(self) -> SyncStats:
        """Flush pending sends, receive/apply everything available, then send local."""
        stats = SyncStats()
        self.last_clipboard_error = None
        self.flush_outbox(stats)
        frames: list[bytes]
        try:
            frames = self._transport.recv()
        except TransportError:
            stats.recv_errors += 1
            frames = []
        for frame in frames:
            stats.received += 1
            self._handle_frame(frame, stats)
        self._read_local(stats)
        self.flush_outbox(stats)
        stats.queued = len(self._outbox)
        return stats

    def flush_outbox(self, stats: SyncStats | None = None) -> int:
        """Try to send queued frames in order; stop at the first transport error."""
        stats = stats if stats is not None else SyncStats()
        while self._outbox:
            frame = self._outbox[0]
            try:
                self._transport.send(frame)
            except FrameTooLarge:
                self._outbox.popleft()
                stats.rejected_too_large += 1
                continue
            except TransportError:
                stats.send_errors += 1
                break
            self._outbox.popleft()
            stats.sent += 1
        stats.queued = len(self._outbox)
        return stats.sent

    def state_summary(self) -> dict:
        return {
            "device_id": self.device_id,
            "last_seq": dict(self._last_seq),
            "seen_ids": len(self._seen_ids),
            "outbox": len(self._outbox),
            "last_local_hash": self._last_local_hash,
            "seq_gaps": self._seq_gaps,
            "overflow_dropped": self._overflow_dropped,
        }

    # -- receive path ---------------------------------------------------------
    def _handle_frame(self, frame: bytes, stats: SyncStats) -> None:
        if self._codec is not None:
            try:
                frame = self._codec.decrypt(frame)
            except CryptoError:
                stats.dropped_invalid += 1
                return
        try:
            msg = decode_frame(frame)
        except PayloadTooLarge:
            stats.rejected_too_large += 1
            return
        except ValidationError:
            stats.dropped_invalid += 1
            return

        if msg.origin == self.device_id:
            stats.dropped_loop += 1
            return

        last = self._last_seq.get(msg.origin)
        if last is not None and msg.seq <= last:
            stats.dropped_stale += 1
            return

        if msg.msg_id in self._seen_ids:
            self._advance_seq(msg.origin, msg.seq)
            stats.dropped_dup += 1
            return

        if msg.content_hash == self._last_local_hash:
            # Already on the clipboard: no write, no flicker, but the seq is consumed.
            self._advance_seq(msg.origin, msg.seq)
            self._mark_seen(msg.msg_id)
            stats.dropped_dup += 1
            return

        try:
            self._clipboard.write(msg.payload)
        except CLIPBOARD_FAILURES as exc:
            # A backend failure is expected at run time (locked session, clipboard
            # manager restart, missing display): count it and keep the poll loop
            # alive instead of letting it kill the daemon.
            self.last_clipboard_error = f"write: {exc}"
            stats.dropped_invalid += 1
            stats.clipboard_errors += 1
            return

        if last is not None and msg.seq > last + 1:
            self._seq_gaps += 1
        self._advance_seq(msg.origin, msg.seq)
        self._mark_seen(msg.msg_id)
        self._last_local_hash = msg.content_hash
        stats.applied += 1

    # -- send path ------------------------------------------------------------
    def _read_local(self, stats: SyncStats) -> None:
        try:
            payload = self._clipboard.read()
        except CLIPBOARD_FAILURES + (PayloadTooLarge,) as exc:
            # PayloadTooLarge is the backend rejecting an oversized clipboard; it is
            # a read failure like any other, not a daemon exit.
            self.last_clipboard_error = f"read: {exc}"
            stats.dropped_invalid += 1
            stats.clipboard_errors += 1
            return
        if payload is None:
            return
        content_hash = payload.content_hash()
        if content_hash == self._last_local_hash:
            return  # unchanged, or the echo of a payload we just applied

        msg = ClipMessage(origin=self.device_id, seq=self._next_seq, payload=payload)
        self._next_seq += 1
        frame = encode_frame(msg)
        if self._codec is not None:
            frame = self._codec.encrypt(frame)
        if len(self._outbox) >= self._max_outbox:
            self._outbox.popleft()
            self._overflow_dropped += 1
        self._outbox.append(frame)
        self._last_local_hash = content_hash
        self._mark_seen(msg.msg_id)

    # -- helpers --------------------------------------------------------------
    def _advance_seq(self, origin: str, seq: int) -> None:
        current = self._last_seq.get(origin)
        if current is None or seq > current:
            self._last_seq[origin] = seq

    def _mark_seen(self, msg_id: str) -> None:
        self._seen_ids[msg_id] = self._clock()
        self._seen_ids.move_to_end(msg_id)
        while len(self._seen_ids) > self._seen_capacity:
            self._seen_ids.popitem(last=False)
