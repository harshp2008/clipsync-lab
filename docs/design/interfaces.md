# Prototype design — module contracts (frozen before code jobs)

This file is the contract that `clipsync/` modules and their tests implement. It is
written so that separately-owned chunks (agy or me) fit together without rework.
Owner of this file: DeepSeek (orchestrator). Additive changes are allowed; breaking
changes require a line in `docs/orchestrator/DECISIONS.md`.

## Scope

Laptop side of a two-way clipboard relay: text and image payloads, no phone and no
cloud needed for tests. Everything is in-memory or local-file in tests; the real
Wayland backend exists but is **never executed in this run**.

Python 3.12, stdlib + `cryptography` (already installed; no packages added).

```
clipsync/
  __init__.py
  protocol.py    # payload/message model, validation, wire format, hashing
  clipboard.py   # ClipboardBackend protocol, FakeClipboard, WlClipboardBackend (unexecuted)
  transport.py   # Transport protocol, InMemoryTransport (hub/pair), FileTransport
  crypto.py      # optional authenticated encryption codec
  engine.py      # two-way sync loop: echo prevention, dedup, ordering, reconnect
tests/
  test_protocol.py test_clipboard.py test_transport.py test_crypto.py test_engine.py
pyproject.toml   # project metadata + pytest config
```

## Security / safety invariants

- No module reads or writes the real clipboard implicitly. `WlClipboardBackend` does
  os-level work only when `.read()`/`.write()` are explicitly called; tests only call
  its pure `build_*`/`choose_mime` helpers.
- Never log payload bytes/text at INFO; `repr` of payloads is truncated.
- Wire data is untrusted: `ClipMessage.from_wire` validates version, types, sizes,
  MIME, base64, hash. A bad hash or size rejects the whole message.

## protocol.py

Constants:

```python
WIRE_VERSION = 1
MAX_TEXT_BYTES = 1_000_000            # 1 MB UTF-8
MAX_IMAGE_BYTES = 8 * 1024 * 1024     # 8 MiB
SUPPORTED_TEXT_MIMES = frozenset({"text/plain", "text/html", "text/uri-list"})
SUPPORTED_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
```

Exceptions: `ClipError(Exception)`, `ValidationError(ClipError)`,
`PayloadTooLarge(ValidationError)`, `UnsupportedMime(ValidationError)`.

```python
@dataclass(frozen=True, slots=True)
class ClipPayload:
    kind: Literal["text", "image"]
    mime: str
    text: str | None = None      # required iff kind == "text"
    data: bytes | None = None    # required iff kind == "image"

    @property
    def size_bytes(self) -> int          # len(text.encode("utf-8")) or len(data)
    def content_hash(self) -> str        # sha256 hex, see below
    def to_wire(self) -> dict
    @classmethod
    def from_wire(cls, obj) -> "ClipPayload"
```

Validation (`__post_init__`):
- kind must be `"text"` or `"image"`; mime must be in the matching supported set.
- exactly one of `text`/`data` is non-None and matches kind.
- size limits as above; reject empty payloads (size 0).
- text must be `str`; data must be `bytes` (accept `bytearray`/`memoryview` -> bytes).

`content_hash()` = `sha256(b"clipsync/1\0" + kind + b"\0" + mime + b"\0" + body).hexdigest()`
where body is UTF-8 text or image bytes. Prefixing kind+mime prevents cross-type
collisions; it is the dedup identity of the *content*, not of the message.

```python
@dataclass(frozen=True, slots=True)
class ClipMessage:
    origin: str                  # opaque device id, non-empty, <= 128 chars
    seq: int                     # per-origin monotonic, >= 0
    payload: ClipPayload
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    content_hash: str = ""       # filled in __post_init__ from payload
    wire_version: int = WIRE_VERSION

    def to_wire(self) -> dict    # JSON-safe: image data base64 (ASCII)
    @classmethod
    def from_wire(cls, obj: Mapping) -> "ClipMessage"   # strict validation
```

- `__post_init__` recomputes `content_hash`; if a non-empty supplied hash disagrees
  with the payload -> `ValidationError`. `msg_id` must be 32 hex chars.
- `to_wire` keys: `v`, `id`, `origin`, `seq`, `created_at`, `content_hash`,
  `payload: {kind, mime, text?, data_b64?}`. Unknown keys are ignored on read.
- `from_wire` rejects: non-mapping, wrong `v`, missing/extra-typed fields, bad seq,
  bad hash, oversized/unsupported payload, malformed base64.

Helpers:

```python
def text_payload(text: str, mime: str = "text/plain") -> ClipPayload
def image_payload(data: bytes, mime: str) -> ClipPayload
def canonical_json(obj: Mapping) -> bytes   # sorted keys, ", " / ": " separators, UTF-8
def encode_frame(msg: ClipMessage) -> bytes  # canonical_json(msg.to_wire())
def decode_frame(frame: bytes) -> ClipMessage  # JSON parse + from_wire
```

`encode_frame`/`decode_frame` are the plaintext path; the engine may wrap frames with
a codec.

## crypto.py

```python
class CryptoError(Exception)
class Codec(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes
    def decrypt(self, ciphertext: bytes) -> bytes

class FernetCodec:                 # authenticates with HMAC inside Fernet
    def __init__(self, key: bytes)
    @classmethod
    def generate(cls) -> "FernetCodec"
    @property key -> bytes
    def encrypt(self, plaintext: bytes) -> bytes
    def decrypt(self, ciphertext: bytes) -> bytes   # CryptoError on tamper/wrong key

def derive_key(passphrase: str, salt: bytes, *, n: int = 2**14, r: int = 8, p: int = 1) -> bytes
def new_salt(size: int = 16) -> bytes
```

`derive_key` uses `hashlib.scrypt` and returns a urlsafe-base64 32-byte Fernet key.
Wrong key/tamper must raise `CryptoError`, not leak plaintext. Optional: engine takes
`codec=None` (plaintext) or a codec (opaque binary frames).

## clipboard.py

```python
class ClipboardError(Exception)
class ClipboardBackend(Protocol):
    def read(self) -> ClipPayload | None
    def write(self, payload: ClipPayload) -> None

class FakeClipboard:
    payload: ClipPayload | None
    writes: list[ClipPayload]
    reads: int
    fail_read: bool; fail_write: bool
    def read(self) -> ClipPayload | None       # raises ClipboardError if fail_read
    def write(self, payload: ClipPayload)      # appends to writes; raises if fail_write
    def set_payload(self, payload)             # simulate a user copy, not an engine write
    def clear(self) -> None

class CommandRunner(Protocol):
    def run(self, argv: list[str], stdin: bytes | None = None) -> tuple[int, bytes, bytes]

class WlClipboardBackend:      # NOT EXECUTED IN THIS RUN
    def __init__(self, runner: CommandRunner | None = None)
    def read(self) -> ClipPayload | None
    def write(self, payload: ClipPayload) -> None
    @staticmethod build_list_types_command() -> list[str]   # ["wl-paste", "--list-types"]
    @staticmethod build_read_command(mime: str) -> list[str] # ["wl-paste","--no-newline","--type",mime]
    @staticmethod build_write_command(mime: str) -> list[str]#
    @staticmethod choose_mime(types: Iterable[str]) -> str | None
```

`choose_mime`: prefer the first supported `image/*` in `SUPPORTED_IMAGE_MIMES`, else
`text/plain`, else any supported text mime, else None. `read()` lists types, picks a
mime, reads bytes, returns payload. `write()` pipes payload bytes to `wl-copy --type`.
Temporary file/URI types are out of scope. Constructor must not spawn processes.

## transport.py

Frames are opaque bytes (`encode_frame` output, possibly encrypted).

```python
class TransportError(Exception)
class Transport(Protocol):
    def connect(self) -> None
    def close(self) -> None
    @property
    def connected(self) -> bool
    def send(self, frame: bytes) -> None   # raises TransportError
    def recv(self) -> list[bytes]          # drains; raises TransportError

def make_pair() -> tuple["InMemoryTransport", "InMemoryTransport"]
```

`InMemoryTransport`: `send` delivers instantly to the peer's inbox and raises
`TransportError` if self or peer is disconnected. `recv` drains inbox; raises if
disconnected. `disconnect()` / `reconnect()` toggle only that endpoint's own link.
`make_pair` returns two connected endpoints wired to each other.

`FileTransport(directory, device_id, peer_id)`: one JSONL file per direction,
`to-<peer_id>.jsonl`; each line is `{"frame_b64": "..."}`. `send` appends + flush +
fsync, raises when not connected. `recv` reads new complete lines from
`to-<device_id>.jsonl` and advances an in-memory offset; a partial trailing line is
left for later; a malformed complete line raises `TransportError`. `connect` creates
the directory; a new instance starts at offset 0 (redelivery is safe because the
engine dedups). Paths are confined to `directory` (reject `device_id`/`peer_id`
containing `/`, `..`, empty).

## engine.py

```python
@dataclass
class SyncStats:
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
    queued: int = 0

class SyncEngine:
    def __init__(self, device_id: str, clipboard: ClipboardBackend,
                 transport: Transport, codec: Codec | None = None,
                 *, seen_capacity: int = 512, max_outbox: int = 64,
                 clock: Callable[[], float] = time.time)
    def poll_once(self) -> SyncStats
    def flush_outbox(self, stats: SyncStats) -> None
    def state_summary(self) -> dict
```

Algorithm for `poll_once`:
1. `flush_outbox` (send queued frames; on `TransportError` keep frame at front and stop).
2. Receive: `transport.recv()`; on `TransportError` -> `recv_errors += 1`, return.
3. For each frame: decrypt if codec (failure -> `dropped_invalid`), `decode_frame`
   (failure -> `dropped_invalid`), handle.
4. Read local clipboard, send if it is new content.

Receive handling (in order):
- `msg.origin == self.device_id` -> `dropped_loop`.
- `msg.seq <= last_seq[origin]` -> `dropped_stale` (applied order per origin is
  non-decreasing; gaps are accepted but counted in `state_summary()["seq_gaps"]`).
- `msg.msg_id` already seen -> `dropped_dup`.
- `msg.content_hash == _last_local_hash` -> `dropped_dup` (already on the clipboard;
  still advance `last_seq`).
- else `clipboard.write(payload)`; `applied += 1`; remember hash and set
  `_last_local_hash = hash`; advance `last_seq`; mark msg_id seen.

Local read handling:
- `None` -> nothing.
- `hash == _last_local_hash` -> no send (covers the echo of a remote apply and an
  unchanged clipboard).
- otherwise: build `ClipMessage(origin=device_id, seq=next_seq, payload=payload)`,
  `seq += 1`, `encode`, enqueue outbox (respect `max_outbox`), `last_local_hash =
  hash`, mark msg_id seen, then try `flush_outbox`.
- `ClipboardError` -> `dropped_invalid += 1` and continue.

Echo guarantee: after applying a remote payload the local clipboard holds exactly that
payload, so the next local read sees `_last_local_hash` and cannot re-send it. Backends
that normalize bytes (re-encode images) can defeat hash equality; this is documented
as a known limitation and a reason to keep `image/*` bytes verbatim.

`state_summary()` returns counts: `last_seq` per origin, `seen_ids`, `outbox`,
`last_local_hash`, `seq_gaps`.

## Tests (owner split)

Travel with the module: `test_protocol.py` (agy), `test_transport.py` (agy),
`test_clipboard.py` + `test_crypto.py` + `test_engine.py` (me). `test_engine.py`
covers end-to-end: echo loop between two engines (bounded message count), duplicate
frame applied once, same content re-copied after a different payload applies again,
oversized image rejected, out-of-order/stale seq never regresses clipboard, reconnect
flushes outbox without duplicates, encrypted+plaintext engines interoperate only with
matching keys.
