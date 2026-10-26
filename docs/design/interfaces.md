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
def canonical_json(obj: Mapping) -> bytes   # sorted keys, compact ","/":" separators, UTF-8, no \u escaping
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

## relay_server.py / relay_transport.py (network relay)

Additive contract for the relay run (2026-10-26). The relay is a byte pipe: frames are
opaque bytes, possibly E2E ciphertext; the server never parses or decrypts them and
never needs a key. Transports remain synchronous, as in `transport.py`.

`websockets` (10.4) is available and is the implementation; the server and client run
their asyncio loops in their own background threads behind the synchronous API.

### relay_server.py

```python
RELAY_MAX_FRAME_BYTES: int   # 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 8192, i.e. base64
                             # expansion of the largest legal image payload + JSON slack

class RelayServer:
    def __init__(self, *, token: str, host: str = "127.0.0.1", port: int = 0,
                 max_frame_bytes: int = RELAY_MAX_FRAME_BYTES) -> None
    @property host -> str
    @property port -> int          # actual bound port (useful with port=0)
    @property running -> bool
    def start(self) -> None        # bind + serve in a background thread; returns when listening
    def stop(self) -> None         # idempotent; closes clients and releases the port
    def __enter__ / __exit__
```

- Loopback only: `host` must be exactly `"127.0.0.1"`; any other value (including
  `"0.0.0.0"`, `"::"`, `"localhost"`, hostnames) raises `ValueError`. Empty `token`
  raises `ValueError`. `port=0` binds an ephemeral port.
- Authentication: every client sends `Authorization: Bearer <token>` in the WebSocket
  handshake. Missing/wrong token is rejected during the handshake (HTTP 403) and the
  connection is not registered.
- Forwarding: every message received from a client is forwarded verbatim to every
  *other* connected client, never back to the sender. Order is preserved per
  connection. No persistence, no replay.
- Size: a frame longer than `max_frame_bytes` is rejected: the offending connection is
  closed (code 1009) and the frame is not forwarded. `start`/`stop` may raise `OSError`
  if the port is taken.

### relay_transport.py

```python
class RelayTransport:            # satisfies transport.Transport
    def __init__(self, url: str, token: str, *,
                 max_frame_bytes: int = RELAY_MAX_FRAME_BYTES,
                 reconnect: bool = True,
                 reconnect_delay: float = 0.2,
                 connect_timeout: float = 5.0) -> None
    @property url -> str
    def connect(self) -> None    # raises TransportError if the first connect fails
    def close(self) -> None      # idempotent; stops reconnecting
    @property connected -> bool
    def send(self, frame: bytes) -> None    # TransportError if not connected / oversize
    def recv(self) -> list[bytes]           # drains FIFO, TransportError if not connected

def relay_url(port: int, host: str = "127.0.0.1") -> str   # "ws://<host>:<port>/"
```

- `url` is a `ws://127.0.0.1:<port>/` URL; the token travels in the handshake header,
  never in the URL. Constructing the object performs no I/O.
- `connect()` returns once authenticated/serving, raising `TransportError` if that does
  not happen within `connect_timeout`. With `reconnect=True`, an unexpected drop is
  retried every `reconnect_delay` until success or `close()`; `connected` reflects the
  live socket. With `reconnect=False`, a drop leaves the transport disconnected.
- `send` preserves call order on a single connection; oversize frames raise
  `TransportError` client-side and are never sent. `recv` never blocks.

## daemon run — config, daemon loop, relay command (2026-10-27)

Additive contract for the laptop app. Acceptance tests live in
`tests/acceptance/test_daemon_acceptance.py` and are frozen before the implementation
job. All tests use `FakeClipboard`, `InMemoryTransport`, or `RelayTransport` against a
loopback `RelayServer`; `WlClipboardBackend` is only ever driven by a canned
`CommandRunner`. No test may spawn `wl-paste`/`wl-copy` or touch the real clipboard.

```
clipsync/config.py     # TOML config loader for the daemon + secret-file indirection
clipsync/daemon.py     # ClipboardDaemon loop + `clipsync` console entry point
clipsync/relay_cli.py  # `clipsync-relay` console entry point
```

`config.py`, `daemon.py` and `relay_cli.py` compose the existing public modules; they
do not duplicate protocol/engine/transport logic.

### config.py

```python
class ConfigError(Exception): ...

@dataclass(frozen=True, slots=True)
class DaemonConfig:
    device_id: str
    relay_url: str
    token: str
    e2e_key: str | None = None
    poll_interval: float = 0.5
    reconnect_delay: float = 0.2
    connect_timeout: float = 5.0

def load_config(path: str | Path) -> DaemonConfig
```

TOML keys (only these are accepted; any other top-level key is `ConfigError`):

| Key | Required | Rules |
|---|---|---|
| `device_id` | yes | `str`, 1–128 chars after `strip()` |
| `relay_url` | yes | `str` with scheme `ws` or `wss` and a non-empty host |
| `token` **or** `token_file` | exactly one | inline non-empty `str`, or a path to a file whose stripped contents are the token |
| `e2e_key` **or** `e2e_key_file` | optional, at most one | valid Fernet key (as accepted by `FernetCodec`), inline or file |
| `poll_interval` | no (default `0.5`) | finite number `> 0` |
| `reconnect_delay` | no (default `0.2`) | finite number `> 0` |
| `connect_timeout` | no (default `5.0`) | finite number `> 0` |

- `token` + `token_file` together, or `e2e_key` + `e2e_key_file` together, is an error.
- A relative `token_file` / `e2e_key_file` is resolved **relative to the config file's
  directory**, not the process CWD.
- Secret-file contents are stripped; an empty file is an error; a missing/unreadable
  file is a `ConfigError` naming the key.
- `load_config` raises `ConfigError` (never leaks the token/key value) for: missing or
  unreadable config, invalid TOML, unknown keys, missing required keys, wrong types,
  invalid URL, invalid key, and bad numeric values.
- Constructing the config performs **no I/O beyond reading the config/secret files**.

### daemon.py

```python
class ClipboardDaemon:
    def __init__(self, config: DaemonConfig, *,
                 clipboard: ClipboardBackend,
                 transport: Transport,
                 codec: Codec | None = None) -> None
    def connect(self) -> None            # transport.connect(); may raise TransportError
    def poll_once(self) -> SyncStats     # one engine.poll_once() via this daemon
    def run(self, *, stop_event: threading.Event | None = None,
            max_iterations: int | None = None,
            sleep: Callable[[float], None] = time.sleep) -> int
    def close(self) -> None              # idempotent; transport.close()
```

- The daemon owns a `SyncEngine(config.device_id, clipboard, transport, codec)`. It
  must not read/write the clipboard except through the injected backend.
- `run`: `max_iterations` is `None` or an `int >= 0` (bools rejected with
  `ValueError`). It polls at most that many times, checks `stop_event` before every
  poll, and calls `sleep(config.poll_interval)` **between** polls only (no sleep after
  the last poll or when stopping). Returns the number of polls performed.
  `max_iterations=0` returns `0` without polling or sleeping.
- A poll must not raise for transport/clipboard failures; the engine already counts
  them and keeps frames queued for retry.

```python
def build_codec(config: DaemonConfig) -> Codec | None   # FernetCodec(e2e_key) or None
def build_clipboard(config: DaemonConfig) -> ClipboardBackend  # WlClipboardBackend()
def build_transport(config: DaemonConfig) -> Transport
    # RelayTransport(config.relay_url, config.token,
    #                 reconnect_delay=config.reconnect_delay,
    #                 connect_timeout=config.connect_timeout)

def main(argv: Sequence[str] | None = None, *,
         clipboard_factory: Callable[[DaemonConfig], ClipboardBackend] | None = None,
         transport_factory: Callable[[DaemonConfig], Transport] | None = None,
         codec_factory: Callable[[DaemonConfig], Codec | None] | None = None) -> int
```

`main` CLI (argparse):

- `--config PATH` (default `~/.config/clipsync/config.toml`), `--once`,
  `--log-level {DEBUG,INFO,WARNING,ERROR}` (default `INFO`), `--version`.
- Config errors: message to stderr, return **2**. `TransportError` from
  `transport.connect()`: message to stderr, return **1**. Success: **0**.
- `--once`: connect, poll **exactly once**, print one compact JSON object line with
  the `SyncStats` fields to stdout, close, return 0. The JSON must be parseable and
  include at least `sent`, `received`, `applied`, `queued`.
- Without `--once`: connect, then `daemon.run()` until SIGINT/SIGTERM; stop cleanly,
  close the transport, return 0.
- Factories override the default builders (tests inject `FakeClipboard` and a
  `WlClipboardBackend` with a canned runner). Logs to stderr; never log payload text
  or bytes, the token, or the key.
- `python3 -m clipsync.daemon` works (`if __name__ == "__main__"`).

### relay_cli.py

```python
@dataclass(frozen=True, slots=True)
class RelayConfig:
    token: str
    port: int = 8765
    max_frame_bytes: int = RELAY_MAX_FRAME_BYTES
    # __post_init__: ValueError for empty token, port outside 0..65535,
    #                max_frame_bytes <= 0

def build_server(config: RelayConfig) -> RelayServer
def run_relay(config: RelayConfig, *,
              stop_event: threading.Event | None = None,
              on_started: Callable[[RelayServer], None] | None = None) -> int
def main(argv: Sequence[str] | None = None, *,
         stop_event: threading.Event | None = None,
         on_started: Callable[[RelayServer], None] | None = None) -> int
```

- `build_server` returns `RelayServer(token=config.token, port=config.port,
  max_frame_bytes=config.max_frame_bytes)` (loopback is enforced by the server).
- `run_relay`: start the server, print exactly one line
  `clipsync-relay listening on ws://127.0.0.1:<port>/` (flush), call
  `on_started(server)` (used by tests to learn the ephemeral port), then block until
  `stop_event` is set (or `KeyboardInterrupt`), stop the server in `finally`, return
  0. A bind `OSError` prints an error to stderr and returns 1.
- `main` CLI (argparse): `--token` **or** `--token-file`, `--port` (default `8765`),
  `--max-frame-bytes` (default `RELAY_MAX_FRAME_BYTES`), `--log-level`, `--version`.
  Missing/both token sources, unreadable token file, or invalid values: stderr +
  return **2**. With `stop_event` supplied it must **not** install signal handlers
  (safe off the main thread); otherwise install SIGINT/SIGTERM handlers.
- `python3 -m clipsync.relay_cli` works (`if __name__ == "__main__"`).

### Packaging and README

`pyproject.toml` gains:

```toml
[project.scripts]
clipsync = "clipsync.daemon:main"
clipsync-relay = "clipsync.relay_cli:main"
```

`README.md` gains a `## Quickstart` section showing: start `clipsync-relay`, create the
config + secret files, run `clipsync --config ...`, and a one-shot
`clipsync --config ... --once`. It must state that `clipsync` is the laptop daemon,
`clipsync-relay` is loopback-only, and both token and E2E key stay out of git.
