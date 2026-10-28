# meshcore phase 2a — elected hub over the existing relay (contract)

Status: frozen before the implementation job (2026-10-28). Implements phase 2 of
`docs/design/coordination-v2.md` **except** LAN discovery/fallback, the naming
service, panel, routing preferences, Android, channels/state, Bluetooth and TLS.

The design doc's sections 0, 3, 4, 6, 10 and 13 are the behavioural contract. This
document freezes the Python interfaces and wire details the tests rely on.

## Dependency rule

- `meshcore/` never imports `clipsync` or any other app package in this repo.
- The **pure core** (`__init__.py`, `config.py`, `coordination.py`, `messages.py`,
  `election.py`, `node.py`) imports only the Python standard library (plus
  `meshcore` itself).
- `hub.py` and `link.py` are the WebSocket transport adapters: they may import the
  standard library and the already-existing third-party `websockets` package. This
  is the only third-party exception, documented in `DECISIONS.md` (D-phase2a-1):
  re-implementing RFC 6455 in the standard library would be a larger risk than the
  existing dependency the relay already uses.
- `clipsync/` imports `meshcore/`; never the reverse. The AST test enforces it.

## Wire protocol (shared by `hub.py` and `link.py`)

WebSocket, same bearer-token authentication as today (`Authorization: Bearer
<token>`, HTTP 403 otherwise). Maximum message size is the existing relay formula
(`4 * ((8 MiB + 2) // 3) + 8192`), defined once as
`meshcore.config.DEFAULT_MAX_FRAME_BYTES`.

- **Text frames are meshcore control messages**, JSON objects with a `type`:
  - `{"type": "hello", "device_id": ..., "name": ..., "perm"?: int,
    "temp_rank"?: int, "will_hub": bool}` — a client announces itself. Absent
    optional fields have the `Hello` defaults.
  - `{"type": "roster", "devices": [<hello>, ...]}` — sent by the hub to every
    **helloed** client on join, on leave and on hub-side roster changes.
  - `{"type": "leaving"}` — a client says it is going away before closing.
- **Binary frames are opaque app payloads.** A client sends its payload bytes
  verbatim. The hub forwards them verbatim to clients that never sent a Hello (old
  clients), and wrapped in a data envelope to helloed mesh clients so the receiver
  learns the sender:
  `b"\x00mc2d\x00" + 4-byte big-endian header length + JSON {"from": id|None} + payload`.
  `Link` strips the envelope (or accepts a bare payload) and delivers bytes only.
- A client that never sends a Hello is an old client: it connects, publishes and
  receives raw payloads, is never sent control messages, and is absent from the
  roster. Nothing in the protocol requires a Hello.

## `meshcore/config.py`

```python
DEFAULT_MAX_FRAME_BYTES: int  # 4 * ((8 * 1024 * 1024 + 2) // 3) + 8192

def format_ws_url(address: str) -> str
@dataclass(frozen=True)
class PeerConfig:
    device_id: str
    name: str
    address: str                 # "host:port" or "ws://host:port/"
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True
@dataclass(frozen=True)
class MeshConfig:
    device_id: str
    name: str
    token: str
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True
    bind: str = "127.0.0.1"
    port: int = 8765
    peers: tuple[PeerConfig, ...] = ()
    heartbeat: float = 10.0
    missed_pongs: int = 3
    probe_timeout: float = 2.0
    debounce: float = 30.0
    reconnect_delay: float = 0.2
    connect_timeout: float = 5.0
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
```

Validation is strict (non-empty ids/token/name, port 0..65535, positive finite
timings, `missed_pongs >= 1`). `MeshConfig.hello()` returns the local `Hello`.
`format_ws_url("127.0.0.1:8701")` -> `"ws://127.0.0.1:8701/"`; an input that already
starts with `ws://`/`wss://` keeps its scheme and gains a trailing `/` only if it has
no path.

## `meshcore/messages.py`

Existing `Hello`, `encode_hello`, `decode_hello` are unchanged.
Add constants `CONTROL_HELLO = "hello"`, `CONTROL_ROSTER = "roster"`,
`CONTROL_LEAVING = "leaving"`, and:

```python
def encode_hello_message(hello: Hello) -> str
def decode_control(text: str | Mapping[str, Any]) -> tuple[str, Any]
def encode_roster(hellos: Iterable[Hello]) -> str
def decode_roster(text: str | Mapping[str, Any]) -> list[Hello]  # input order kept
def encode_leaving() -> str
def decode_leaving(text: str | Mapping[str, Any]) -> bool        # True; ValueError otherwise
```

`encode_*` returns compact JSON text (`sort_keys=True`, `separators=(",", ":")`),
ready to send as a WebSocket text frame. `decode_control` accepts text or an
already-decoded mapping and returns `(type, payload)`; unknown types raise
`ValueError`. `decode_roster` accepts either the encoded text or the already-decoded
mapping, validates `type == "roster"` and a `devices` list, and returns the `Hello`
objects in the order sent (the hub sends them sorted with `coordination.order`).

## `meshcore/hub.py`

The generic relay, moved out of `clipsync/relay_server.py`.

```python
RELAY_MAX_FRAME_BYTES = DEFAULT_MAX_FRAME_BYTES

class HubServer:
    def __init__(self, *, token: str, host: str = "127.0.0.1", port: int = 0,
                 max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
                 device_id: str | None = None, name: str | None = None,
                 on_data: Callable[[str | None, bytes], None] | None = None) -> None
    # context manager, .host, .port, .running, .start(), .stop()
    # .roster() -> list[Hello]   (hub + helloed clients, sort_key order)
    # .broadcast(payload: bytes) -> None   (hub-origin payload to every client)
    # .set_on_data(callback) -> None
```

Behaviour:

- Same lifecycle, loopback-friendly host default, ephemeral port, background event
  loop/thread, idempotent start/stop, 1001 close on shutdown, 1009 on oversize, 403
  on bad/missing token, no echo to the sender, FIFO per sender.
- Keeps a `_clients` mapping keyed by the WebSocket object (the existing relay tests
  use its length). Values must be per-client send queues; forwarding must stay
  payload-opaque.
- On `hello`: register the device, note its Hello, broadcast the roster (text) to
  every helloed client including the newcomer. On `leaving`/disconnect: unregister
  and broadcast the roster. A hello replaces any previous Hello for the same socket.
- On a binary payload from a client: call `on_data(sender_id_or_None, payload)` (the
  hub app receives what clients publish) and forward it to every other client;
  helloed recipients get the data envelope, old clients get the bytes verbatim.
- `broadcast(payload)` wraps for helloed recipients and sends raw to old clients.
- The roster includes the hub's own Hello only when `device_id` is not None; it is
  sorted with `coordination.sort_key`.
- Malformed control text is ignored (never forwarded, never crashes the hub).
- Hub-side device info comes only from hellos; ranks in a Hello replace the
  configured values for that connection.

## `meshcore/link.py`

The generic client side, moved out of `clipsync/relay_transport.py`.

```python
class LinkError(Exception): ...

def relay_url(port: int, host: str = "127.0.0.1") -> str

class Link:
    def __init__(self, url: str, token: str, *,
                 hello: Hello | None = None,
                 max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
                 reconnect: bool = True,
                 reconnect_delay: float = 0.2,
                 connect_timeout: float = 5.0,
                 thread_name: str = "meshcore-link",
                 on_roster: Callable[[list[Hello]], None] | None = None,
                 on_payload: Callable[[str | None, bytes], None] | None = None) -> None
    # .url, .connected, .roster -> list[Hello], .connect(), .close(),
    # .send(payload: bytes), .recv() -> list[bytes],
    # .set_on_roster(cb), .set_on_payload(cb)
```

- Port the existing `RelayTransport` connection loop into `Link`: dedicated
  background asyncio thread with `thread_name`, first-attempt failure raises
  `LinkError` and leaves `connected == False` with no live thread, later drops
  reconnect with the same backoff when `reconnect=True`, `send()` raises when
  disconnected and enforces the size limit with an
  `"... exceeds max_frame_bytes ..."` message, `recv()` raises when disconnected
  and drains queued payload bytes in FIFO order, `close()` is idempotent and stops
  reconnection.
- Sends the `hello` control message immediately after every successful connect when
  `hello` is not None; `hello=None` is the old-client mode (no control messages).
- On control text: store/callback the roster for `roster`, ignore `hello`, and on
  `leaving` close the link. On binary: strip the data envelope if present (sender id
  may be `None`), append the payload bytes to the inbox and call `on_payload` if set.
- `close()` on a helloed link makes a best-effort `leaving` send before closing.

## `meshcore/election.py`

The driver around `coordination.probe_hub` / `HubTracker` with injected seams.

```python
class Election:
    def __init__(self, self_hello: Hello, peers: Iterable[Hello | Device], *,
                 ping: Callable[[str, float], bool],
                 clock: Callable[[], float] = time.monotonic,
                 heartbeat: float = 10.0,
                 missed_pongs: int = 3,
                 probe_timeout: float = 2.0,
                 debounce: float = 30.0,
                 on_hub_changed: Callable[[str], None] | None = None,
                 logger: logging.Logger | None = None) -> None
    # .current_hub -> str | None, .set_devices(hellos), .stop(), .run_once()
```

- `run_once()` performs one election cycle: if the current hub is not self and its
  probe failed `missed_pongs` times in a row, treat it as dead (this is the 3 missed
  pongs rule); otherwise every heartbeat probes the order top-down with
  `probe_hub`. Feed the result into `HubTracker(clock, debounce)`.
- `probe_hub` is called with self included; `ping(self_id, timeout)` must be
  answered by the injected seam (reachable self). A forced result (all reachable
  devices decline `will_hub`) logs one WARNING containing `forced` through
  `logging.getLogger("meshcore.election")` (or the injected logger).
- `start()` runs `run_once()` once immediately and then every `heartbeat` in a
  daemon thread; `stop()` joins it. A hub change (debounced) calls
  `on_hub_changed(hub_id)`.
- `set_devices()` replaces the peer device list (the Node refreshes it from the
  roster); the self Hello is always part of the candidate list.

## `meshcore/node.py`

The small app-facing facade. It ties config + election + hub + link together.

```python
class NodeError(Exception): ...

class Node:
    def __init__(self, config: MeshConfig, *,
                 can_reach: Callable[[str, str], bool] | None = None,
                 clock: Callable[[], float] | None = None) -> None
    # .device_id, .hub_id -> str | None, .is_hub -> bool, .connected -> bool,
    # .start(), .stop(), .publish(payload: bytes), .roster() -> list[Hello],
    # .on_receive(cb(sender_id, payload)), .on_hub_changed(cb(hub_id))
```

- `can_reach(a, b)` is the partition seam: a ping or a link connection to `b`
  succeeds only when `can_reach(self_id, b)` is true (default: always true).
  `ping` first checks the seam and then opens a TCP connection to the peer address
  with `probe_timeout`; self always answers.
- `start()` is idempotent: starts the hub listener on `bind:port` (every node
  listens; being the hub is an election result, not a separate process), starts the
  election, then follows hub changes.
- On a hub change: log INFO with old hub, new hub and reason; log WARNING for a
  forced hub; when the new hub is self, close any client link and serve as hub; when
  it is remote, open a `Link` to that peer's address with the local Hello, reconnect
  with backoff, and replace the old link. A link to a peer is not even attempted
  when `can_reach` is false.
- `publish(payload)` sends immediately: the hub broadcasts to its clients; a client
  sends over its live link. If there is no route (starting up, failover window) it
  raises `NodeError`; callers keep their own outbox and retry (this preserves the
  engine's existing outbox behaviour).
- `on_receive` fires for app payloads from other devices on both roles (hub clients
  and forwarded client payloads), with the sender's device id when known, else
  `None`. A node never receives its own publish from its own hub.
- `roster()` returns the hub's current roster on the hub, the last roster received on
  a client, and `[self_hello]` before the first roster; always sorted.
- `stop()` sends a best-effort `leaving`, stops the link, election and hub, joins
  threads, and is idempotent. No threads outlive `stop()`.
- Hub changes and roster updates never deadlock: callbacks run outside internal
  locks.

## `clipsync/config.py` mapping

`DaemonConfig` gains optional fields, all with defaults so old configs are valid:

```python
name: str | None = None
perm: int | None = None
temp_rank: int | None = None
will_hub: bool = True
bind: str = "127.0.0.1"       # hub listen address (mesh mode only)
port: int = 8765              # hub listen port (mesh mode only)
peers: tuple[meshcore.config.PeerConfig, ...] = ()
relay_url: str | None         # required only when peers is empty
```

`load_config` validates the same way as the rest: `name` 1..128 chars after strip,
`perm`/`temp_rank` non-negative ints (not bool), `will_hub` a bool, `bind` a
non-empty string, `port` 0..65535, `peers` an array of tables with required
`device_id`, `name`, `address` and optional rank fields; unknown peer keys are
errors. Without `peers`, `relay_url` is required and an existing style config loads
byte-for-byte equivalent values (the new fields take their defaults). With `peers`,
`relay_url` may be omitted. `DaemonConfig.to_mesh_config()` returns the
`MeshConfig`: name defaults to `device_id`, token/config timings are reused.

`clipsync.daemon.main` keeps the injected `transport_factory` seam. Its default
factory uses `build_transport` (the legacy `RelayTransport` to a fixed `relay_url`)
when `peers` is empty, exactly as before; when `peers` is non-empty it builds a
`meshcore.node.Node` and a `clipsync.mesh_transport.NodeTransport`. `build_transport`
itself is unchanged and still returns `RelayTransport` for old configs.

## `clipsync/mesh_transport.py`

Clipboard-app glue only, no hub/election logic:

```python
class NodeTransport:  # satisfies clipsync.transport.Transport
    def __init__(self, node: Node) -> None
    # connect() starts the node once; connected is True after start; close() stops it
    # send(frame) -> node.publish(frame); NodeError -> TransportError
    # recv() -> list[bytes] drained from node.on_receive (sender id dropped)
```

## Thin compatibility wrappers

- `clipsync/relay_server.py`: `RELAY_MAX_FRAME_BYTES` re-exported and
  `RelayServer(HubServer)` with the old constructor signature, the **loopback-only
  validation** (`host != "127.0.0.1"` -> `ValueError("host must be '127.0.0.1'")`),
  and no forwarding logic of its own.
- `clipsync/relay_transport.py`: `relay_url`/`RELAY_MAX_FRAME_BYTES` re-exported and
  `RelayTransport` a thin adapter over `Link` (`hello=None`, thread name
  `clipsync-relay-transport`), translating `LinkError` to `TransportError`. No
  WebSocket connection loop remains in the file.

## Config / mode decisions

- Mesh mode (Node connectivity) is selected only when `peers` is non-empty; this
  keeps every old config on the old single-relay path (A9).
- `port` was added although the request listed only `bind`: static peers need a
  dialable address for the hub, so every node needs a known listen port.
- `bind` is only used by the elected hub role; the compatibility `RelayServer`
  stays loopback-only by contract. Binding off-loopback exposes the relay to the
  network with **token authentication only and no TLS yet** (see the report).

## Acceptance tests (frozen)

`tests/acceptance/test_meshcore_hub.py`: A0–A7, meshcore only, in-process Nodes on
loopback, no `clipsync` import, tiny timings passed through `MeshConfig`.
`tests/acceptance/test_clipsync_on_meshcore.py`: A8–A10, clipsync integration.
`tests/acceptance/test_meshcore_exploratory.py`: the marked exploratory failover-copy
case (not part of the acceptance gate).
