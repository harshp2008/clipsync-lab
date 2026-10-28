# meshcore LAN transport + discovery — contract (frozen before the implementation job)

Status: frozen 2026-10-29, before the phase-2b implementation job. Implements
`docs/design/coordination-v2.md` §5 (transport and discovery): a peer may have several
addresses, they are tried in a fixed order, the last working one is remembered, and
members of the same L2 network announce themselves so a changed IP is found without
internet or Tailscale.

Frozen test: `tests/acceptance/test_meshcore_lan.py` (L1–L11). Existing tests are
unchanged and must keep passing. This document, not the implementation, is the
interface the tests rely on. Out of scope exactly as in the task: naming service,
panel, routing preferences, Android, TLS, state/event channels, Bluetooth, a
third-party mDNS library, a device in several groups.

## Model

- A peer has an ordered set of **addresses**. Kinds are `tailnet`, `lan`, `hotspot`,
  `static`; the fixed transport order is `tailnet`, then `lan`, then `hotspot`, then
  `static`; inside one kind the config order is kept.
- The legacy single `address` string is one `static` entry. Old configs therefore
  keep working unchanged.
- Discovery is **address-only**: it supplies `lan` addresses for device ids that are
  already in static `peers`. It never adds a device, never changes a rank, the hub
  choice, or group membership.
- Discovery is opt-in (`discovery: true`, default **false**). When false, no discovery
  thread starts and no socket is created.
- `last_good` is the address that last answered a real authenticated handshake. It is
  remembered per peer id as `host:port` only, and tried first when it is still in the
  candidate set.

## `meshcore/addressing.py` (stdlib only)

```python
ADDRESS_KINDS: tuple[str, ...] = ("tailnet", "lan", "hotspot", "static")
DEFAULT_WS_PORT: int = 8765

@dataclass(frozen=True, eq=False)
class Address:
    kind: str                 # one of ADDRESS_KINDS, else ValueError
    host: str                 # non-empty; stripped
    port: int | None = None   # None means DEFAULT_WS_PORT; 1..65535 otherwise

    @property
    def key(self) -> tuple[str, int]:
        # (host.lower(), effective port); kind is metadata, not identity
    @property
    def websocket_url(self) -> str:
        # "ws://host:port/" (brackets for an IPv6 literal)
    # __eq__/__hash__ use key (so a stored last_good compares by host:port)
```

`parse_address(value: str | Address) -> Address` accepts `"host:port"`,
`"host"`, `"ws://host:port/path"`, `"wss://..."` and `"[::1]:port"`, returns
`Address("static", host, port)`, and raises `ValueError` for an empty/invalid value.

`ordered_addresses(peer, discovered=(), last_good=None) -> list[Address]` is pure,
stdlib-only and must not mutate its inputs:

1. Candidate list = `peer.addresses` in config order, followed by the legacy
   `peer.address` (if non-empty, parsed by `parse_address`, kind `static`) as the
   final fallback. A peer with only `address` therefore yields exactly one static
   entry.
2. `discovered` is an iterable of `Address`; only `kind == "lan"` entries are merged
   in, after the configured entries of that kind. Other kinds are ignored.
3. Stable-sort by kind rank (`tailnet`, `lan`, `hotspot`, `static`), so equal-kind
   entries keep their input/config order.
4. Remove duplicates by `Address.key` (first occurrence wins).
5. `last_good` may be an `Address`, a `"host:port"` string, a `(host, port)` pair or
   `None`. If its `key` is in the deduplicated set, move that entry to the front;
   otherwise ignore it. It never adds an address.

`LastGoodStore` (same module):

```python
class LastGoodStore:
    def __init__(self, path: str | Path) -> None
    @property
    def path(self) -> Path
    def get(self, device_id: str) -> Address | None   # kind "static"; never raises
    def set(self, device_id: str, address: Address) -> None  # never raises
```

- JSON file `{"<device_id>": "<host>:<port>", ...}`; only host:port per id. No token,
  key, name, group or rank is ever written.
- Atomic write: create the parent directory, write a temp file in the same directory,
  `flush` + `fsync`, then `os.replace(temp, final)`. A failure cleans the temp file,
  logs one warning and leaves the old file untouched; it never raises.
- A missing file, unreadable file, invalid JSON, wrong shape or a bad entry each
  degrade to "no entry" (never an exception). `set` skips the write when the value is
  unchanged. `get`/`set` are safe from several threads.
- `default_state_dir() -> Path` is `$XDG_STATE_HOME/meshcore` or
  `~/.local/state/meshcore`; `MeshConfig.state_dir` overrides it.

## `meshcore/discovery.py` (stdlib only)

Constants and beacon functions:

```python
MULTICAST_GROUP = "239.255.76.67"
DEFAULT_DISCOVERY_PORT = 8766
BEACON_VERSION = 1
DEFAULT_BEACON_INTERVAL = 5.0
DEFAULT_MAX_AGE = 30.0

@dataclass(frozen=True)
class Beacon:
    group: str
    device_id: str
    port: int
    ts: float

def beacon_mac(group, device_id, port, ts, token) -> str
def encode_beacon(*, group, device_id, port, ts, token) -> bytes
def decode_beacon(data, token, *, now, max_age=DEFAULT_MAX_AGE) -> Beacon | None
```

- Datagram is compact UTF-8 JSON
  `{"v":1,"group":...,"device_id":...,"port":...,"ts":...,"mac":"<hex>"}`.
- `mac = HMAC-SHA256(key=token.encode(), msg=canonical JSON of the other five fields
  with sorted keys and separators (",", ":"))`, hex encoded. `beacon_mac` is that
  computation and is public for the test kit.
- `decode_beacon` returns `None` (never raises) for: non-bytes, invalid JSON, missing
  or wrong types, wrong version, port outside 1..65535, bad mac (constant-time
  compare), or `abs(now - ts) > max_age` (stale **and** far-future timestamps; replay
  window 30 s). It does not check group membership; the `Discovery` does.
- The token is the group's token. A listener with another group's token cannot verify
  the mac; a wrong-group datagram that does verify (same token, different group field)
  is ignored by `Discovery`.

Socket seam and real implementation:

```python
class DiscoverySocket(Protocol):
    def send(self, data: bytes, dest: tuple[str, int]) -> None: ...
    def recv(self, timeout: float) -> tuple[bytes, str] | None: ...
    def close(self) -> None: ...

def local_broadcast_addresses() -> list[str]   # interface broadcast IPs, stdlib only
def create_udp_socket(port: int) -> DiscoverySocket  # real UDP broadcast + multicast
```

- The real socket binds `("", port)` with `SO_REUSEADDR`, joins `MULTICAST_GROUP`,
  sets multicast TTL 1, and sends the same datagram to every broadcast address from
  `local_broadcast_addresses()` plus `"255.255.255.255"` (deduplicated) and the
  multicast group. `recv` returns `(bytes, source_ip)` or `None` on timeout/close.
  `local_broadcast_addresses` must not call a third-party library or a shell command;
  on a platform where it cannot enumerate interfaces it returns `[]` and the
  `255.255.255.255` fallback still applies.

`Discovery`:

```python
class Discovery:
    def __init__(self, *, group_id, device_id, token, port,
                 socket: DiscoverySocket, peers: Iterable[str],
                 clock=time.monotonic, interval=DEFAULT_BEACON_INTERVAL,
                 max_age=DEFAULT_MAX_AGE, broadcast_addresses=None,
                 logger=None) -> None
    def start(self) -> None
    def stop(self, timeout: float = 2.0) -> None     # idempotent, bounded join
    def announce(self) -> None
    def poll_once(self, timeout: float = 0.0) -> int  # datagrams processed
    def addresses_for(self, device_id: str) -> list[Address]  # kind "lan"
    @property ignored_beacons: int   # bad mac/group/stale/unknown/parse, counted
    @property accepted_beacons: int
    @property send_errors: int
```

- Thread name `meshcore-discovery`. `start()` announces once, then announces every
  `interval` seconds and polls the socket in between; `stop()` sets the stop event,
  closes the socket and joins within the bound. `start`/`stop` are idempotent.
- Accepted beacon rules: valid mac/ts, `group == group_id`, `device_id` in `peers`,
  and not self. The sender's **source IP** from `recv` is the address host; the
  beacon's `port` is the port. An accepted beacon replaces that device's entry
  (IP or port change), sets `accepted_beacons += 1`. Everything else increments
  `ignored_beacons` and produces no address. Self beacons are ignored without being
  counted as bad.
- Entries expire after `3 * interval` seconds without an accepted beacon (15 s at the
  default interval), checked lazily by `addresses_for` and by the thread. A replaced
  entry starts a fresh expiry window; the old address is gone immediately.
- `broadcast_addresses` is an injectable list for tests; when `None` it is
  `local_broadcast_addresses()`. The addresses actually used are logged once at INFO.
  A send `OSError` is logged once at WARNING and the loop keeps running.

## Configuration

`MeshConfig` gains:

```python
class PeerConfig:
    address: str                 # legacy; may be "" only when addresses is non-empty
    addresses: tuple[Address, ...] = ()

class MeshConfig:
    discovery: bool = False
    discovery_port: int = 8766
    discovery_interval: float = 5.0
    state_dir: str | None = None       # None -> default_state_dir()
    address_timeout: float = 2.0       # per-address connect/probe timeout
```

`PeerConfig` validates that at least one of `address`/`addresses` is present, that
every entry is an `Address`, and keeps `addresses` as a tuple. `MeshConfig` validates
the new fields with the existing validators; `discovery_port` is 0..65535, the
interval is positive, `address_timeout` is positive. The relay role may set these
fields but does not run discovery (it has no peers); no old config changes behaviour.

`clipsync/config.py` maps `addresses`, `discovery`, `discovery_port`, `state_dir`;
old TOML files load with `discovery=False`, `discovery_port=8766`, `state_dir=None`
and the same `PeerConfig.address` as before. A peer table may carry `addresses` in
addition to (or, for new configs, instead of) `address`; each entry is
`{kind, host, port?}` and is validated. Unknown keys are still configuration errors.

## `meshcore/link.py` and `meshcore/node.py`

- `Node` keeps the existing public surface and constructor, plus test seams:
  `probe_url: Callable[[str, float], bool] | None` (default
  `meshcore.link.probe_hub`) and
  `discovery_socket_factory: Callable[[], DiscoverySocket] | None` (default
  `lambda: create_udp_socket(config.discovery_port)`). Existing positional/keyword
  calls keep working.
- New read-only properties: `Node.discovery -> Discovery | None`,
  `Node.connection_url -> str | None` (the URL of the live client link, or None).
- Probe (`_ping`) for a peer: `ordered_addresses(peer, discovery.addresses_for(id),
  last_good.get(id))`; try each in order, at most `min(address_timeout, remaining)`
  per address, total budget = the election probe timeout passed to `_ping`. The first
  real authenticated `probe_hub` success stores that address as `last_good` and
  returns True. All failed = unreachable, as before. The relay branch is unchanged
  (single address).
- Connect (`_ensure_link`) for a peer: the same ordered list. If a connected link's
  URL is still in the candidate set, keep it and refresh `last_good`; if it is not,
  close it. For a fresh/needed connection, create a `Link` per candidate in order with
  `connect_timeout = min(address_timeout, remaining)`, total budget
  `config.connect_timeout`; the first successful authenticated handshake wins and is
  stored as `last_good`; failures are closed. No candidate left = the peer is
  unreachable (today's behaviour). `_current_hub`/`hub_id` are set only when a link
  is usable.
- Discovery starts after the hub listener (it advertises the actual bound
  `HubServer.port`, which matters for `port = 0`) and before election, only when
  `config.discovery` is true and the role is `member`. A failure to create the socket
  logs one WARNING and leaves the node running without discovery. `stop()` stops the
  election, discovery (bounded join), link and hub in an order that leaves no live
  `meshcore-*` thread; `stop()` remains idempotent.
- A hub change or a changed discovered address takes effect without restarting the
  daemon: the next probe records the new `last_good`, and the next `_ensure_link`
  (publish/election path) switches the link to the new address when the old one is no
  longer in the candidate set.
- Security gate: every configured peer address (legacy and `addresses`) and the relay
  address is checked for loopback exactly like `PeerConfig.address` is today.

## Dependency rule

`meshcore/addressing.py` and `meshcore/discovery.py` import standard-library modules
only. `meshcore` still never imports `clipsync`. No new dependency is added to
`pyproject.toml` (`cryptography` and `websockets` stay the only ones).

## Real-world test kit (not pytest) and docs

`scripts/lan_check.py` uses only the stdlib (plus `meshcore.discovery`), is runnable
by hand on two machines:

```
python3 scripts/lan_check.py listen --group G --token T
python3 scripts/lan_check.py probe  --group G --token T
```

Both roles bind the discovery port, send their own authenticated beacons and listen;
each prints every beacon heard with its source IP, device id, group and port, the
measured time to first sighting, and `PASS`/`FAIL` after 20 s (default; `--duration`).
PASS means at least one valid beacon from another device was seen. Exit code 0 on
PASS, 1 on FAIL. The README section "Testing without internet" documents the hotspot
procedure, the expected result and what to report.

`docs/design/coordination-v2.md` §5 is updated to this behaviour and §13 marks 2b;
this document is the detailed reference. `docs/design/meshcore-lan.md` also records
the limits: client isolation on public Wi-Fi/airports blocks broadcast and multicast;
a hotspot works only if it does not isolate clients; two devices on different
networks with no internet cannot reach each other.
