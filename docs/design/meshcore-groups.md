# meshcore groups — contract (frozen before the implementation job)

Status: frozen 2026-10-29, before the groups implementation job. Implements exactly
the "groups" design in the task: one hub serving several isolated groups, a separate
end-to-end key per group, and a relay-only role. Out of scope: LAN discovery, naming
service, panel, routing preferences, Android, TLS, state/event channels, and a device
in more than one group.

Frozen test: `tests/acceptance/test_meshcore_groups.py` (G1–G7). Existing tests are
unchanged and must keep passing. This document, not the implementation, is the
interface the tests rely on.

## Model

- A **group** is `(id, token, e2e_key)`.
  - `id` and `token` are hub-side configuration (`serve_groups`), one token per group.
  - `e2e_key` is member-side configuration only. The hub/relay **never** has it.
  - The hub maps `token -> group`. A client that claims another `group_id` is rejected.
- A node is a member of exactly one group (`group_id`, default `"default"`), or it is
  a **relay**: it serves groups but belongs to none.
- Old configs and old clients land in group `"default"` with the existing token, so
  single-group behaviour is unchanged.

## `meshcore/config.py`

Add (stdlib-only module; no cryptography import here):

```python
GROUP_DEFAULT = "default"

@dataclass(frozen=True)
class GroupConfig:
    id: str
    token: str
    # validates: non-empty stripped id/token; no key field, ever

@dataclass(frozen=True)
class RelayConfig:
    address: str            # "host:port" or "ws://host:port/"
    device_id: str = "relay"  # synthetic preferred-hub id; not a roster member
    # validates: both non-empty

@dataclass(frozen=True)
class MeshConfig:
    device_id: str
    name: str
    token: str
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True
    group_id: str = "default"
    e2e_key: str | None = None
    allow_insecure: bool = False
    role: str = "member"                       # "member" | "relay"
    serve_groups: tuple[GroupConfig, ...] = () # hub side: ids + tokens only
    relay: RelayConfig | None = None           # member side: preferred hub
    bind: str = "127.0.0.1"
    port: int = 8765
    # ... existing fields unchanged ...
```

Validation rules (all raise `ValueError`):

- new fields keep the existing strictness: non-empty `group_id`; `e2e_key` is `None`
  or a non-empty string (the Fernet key itself is validated when the cipher is built,
  because `config.py` must stay stdlib-only); `allow_insecure` is a bool; `role` is
  `"member"` or `"relay"`; `serve_groups` is coerced to a tuple of `GroupConfig` with
  unique ids and unique tokens.
- `role == "member"`: `serve_groups` must be empty; `relay` may be set; the relay's
  synthetic `device_id` must differ from the node's `device_id` and from every peer
  `device_id`. A member group without `relay` behaves exactly as before.
- `role == "relay"`: `serve_groups` must be non-empty; `peers` must be empty;
  `e2e_key` must be `None`; `relay` must be `None`. A relay is never elected and never
  decrypts.
- `token` stays required on `MeshConfig` (old configs); for a relay it is ignored in
  favour of the per-group tokens.

`MeshConfig.hello()` returns `Hello(..., group_id=self.group_id)`.

## `meshcore/messages.py`

- `Hello` gains `group_id: str = "default"` (last field, so all existing positional
  construction and equality keep working).
- `encode_hello` includes `"group_id"` **only when it is not `"default"`**, so old
  peers see the exact old JSON and the frozen phase-2a hello-encoding test stays green.
- `decode_hello` defaults a missing/`None` `group_id` to `"default"`; non-empty
  strings are kept. Rosters therefore carry the group of every non-default member.
- Everything else (control constants, roster, leaving, data envelope) is unchanged.

## `meshcore/hub.py`

`HubServer` becomes a per-group forwarder. Existing single-group callers are unchanged.

```python
class HubServer:
    def __init__(self, *, token: str | None = None, host="127.0.0.1", port=0,
                 max_frame_bytes=DEFAULT_MAX_FRAME_BYTES,
                 device_id: str | None = None, name=None, perm=None,
                 temp_rank=None, will_hub=True,
                 on_data=None,
                 group_id: str = "default",              # implicit single group
                 groups: Iterable[GroupConfig] | None = None) -> None
    # existing lifecycle: context manager, .host, .port, .running, .start, .stop
    # new/changed:
    #   .groups -> tuple[GroupConfig, ...]
    #   .roster(group_id: str | None = None) -> list[Hello]
    #   .broadcast(payload: bytes, group_id: str | None = None) -> None
```

- `groups=None`: the existing single-group mode. The implicit group is
  `GroupConfig(group_id, token)`; `token` is required. `roster()`/`broadcast()` with
  no group use that group.
- `groups` given: multi-group mode; `token` may be `None`; ids and tokens are unique.
- Authentication: the bearer token in `Authorization` is accepted when it matches any
  served group token (403 otherwise, exactly as before). The accepted socket is bound
  to the group that owns its token.
  - A **helloed** client whose `hello.group_id` differs from its token's group is
    rejected: close the socket (policy violation / 1008) and never add it to a roster
    or forward its frames. A group mismatch on a later hello is rejected the same way.
  - A client that never sends a hello is an **old client**: it is placed in its
    token's group, gets raw payloads, never receives control messages and never enters
    a roster.
- Per-group state: helloed clients, roster and forwarding are partitioned by group.
  Binary frames are forwarded only to clients in the sender's group (no echo to the
  sender). The data envelope for helloed recipients is unchanged, so the sender id
  remains visible to the receiving mesh client.
- Roster broadcast: only to helloed clients of that group, and it lists only that
  group's helloed clients plus the hub's own `Hello` when the hub has a `device_id`
  and belongs to that group. The relay has `device_id=None`, so it never appears.
- `roster(None)`: the single group's roster when one group is served, otherwise the
  sorted union of all groups (`group_id` filter is the normal path).
- `broadcast(None)` with more than one group raises `ValueError` (prevents a
  cross-group leak); with one group it behaves as before. `Node` always passes its own
  group explicitly.
- `on_data` keeps the `(sender_id, payload)` signature and is called for payloads from
  every served group; only relay-style hubs (no app) use the multi-group form.

## `meshcore/link.py`

No signature change. `Link` sends its `Hello` (which now carries `group_id`) after
every successful connect, and remains payload-opaque. A group mismatch closes the
socket, so `connected` becomes false. `probe_hub` is unchanged.

## `meshcore/election.py`

Add an optional keyword:

```python
class Election:
    def __init__(self, self_hello, peers, *, ...,
                 preferred_id: str | None = None, ...)
```

- Every `run_once()` first probes `preferred_id` (when set) through the same `ping`
  seam and the same missed-pongs accounting. If it answers, the candidate is
  `preferred_id` and `last_forced` is false.
- Otherwise the existing `probe_hub` election among member devices runs unchanged.
- The candidate still goes through `HubTracker`/`debounce`, so a returning relay gets
  the hub role back only after the debounce, and a relay flap cannot cause migration.
- With `preferred_id=None`, behaviour is byte-for-byte the phase-2a behaviour.

## `meshcore/node.py`

New/changed public surface:

```python
class Node:
    # existing: device_id, hub_id, is_hub, connected, start, stop, publish,
    #           roster, on_receive, on_hub_changed
    @property
    def group_id(self) -> str
    @property
    def is_relay(self) -> bool
    @property
    def relay_id(self) -> str | None          # config.relay.device_id
    @property
    def dropped_undecryptable(self) -> int
```

Roles and routing:

- **Member** (`role="member"`): exactly the phase-2a node, plus:
  - its `HubServer` serves the implicit single group `(config.group_id, config.token)`
    and its own `Hello` carries the group;
  - `publish` encrypts with the group key (when set) before broadcasting/sending;
  - receives are decrypted before `on_receive`; a frame that fails to decrypt is
    dropped, never delivered, and increments `dropped_undecryptable`;
  - its roster is filtered to `config.group_id`;
  - with `config.relay`, `relay_id` is the preferred hub. Election is given
    `preferred_id=relay.device_id`. `_ping` probes the relay URL with the member's
    token; when the election result is the relay id, the node opens a `Link` to the
    relay address (the relay is not in `peers` and never in the roster). If the relay
    is unreachable, the normal election among members runs; when it returns, the
    existing debounce hands the hub role back.
- **Relay** (`role="relay"`): `start()` runs one `HubServer` with
  `groups=config.serve_groups`, `device_id=None`, `on_data=None`, and **no**
  election, link, key or decrypt path. `is_hub`/`connected` are false, `hub_id` is
  `None`, `roster()` is `[]`, and `publish()` raises `NodeError`. It forwards opaque
  bytes only. `stop()` is idempotent and leaves no thread.

Encryption in `meshcore/crypto.py` (see below) is applied by the Node, not by
`Link`/`HubServer`, so the hub forwards ciphertext unchanged. There is no double
encryption with clipsync: in mesh mode the clipsync daemon passes `codec=None` and
lets meshcore encrypt; the legacy single-relay mode keeps the existing clipsync codec.

Security gate in `Node.start()` (before any thread starts):

- If `e2e_key` is set, start normally.
- A relay is exempt (it has no key by design) and never decrypts.
- Otherwise, if the node `bind` host or any peer/relay address host is not loopback
  (`127.0.0.0/8`, `::1`, or `localhost`), start raises `NodeError` with a clear
  message containing `e2e_key` and `allow_insecure`, unless `allow_insecure=True`.
  With `allow_insecure=True` the node starts and logs exactly one WARNING through
  `logging.getLogger("meshcore.node")` containing `allow_insecure`.
- Loopback with no key keeps working exactly as before (no warning), so all existing
  loopback tests stay green.

## `meshcore/crypto.py`

Same library as `clipsync/crypto.py` (Fernet from `cryptography`; no new dependency):

```python
class MeshCryptoError(Exception): ...
class GroupCipher:
    def __init__(self, key: str | bytes) -> None   # MeshCryptoError on a bad key
    def encrypt(self, plaintext: bytes) -> bytes
    def decrypt(self, ciphertext: bytes) -> bytes  # MeshCryptoError on wrong key/tamper
```

Frames are the Fernet token bytes verbatim; no extra header is added, so the hub
cannot see the plaintext or the key.

**Frozen-A10 constraint:** the phase-2a AST test is frozen and only permits
`websockets` in `hub.py`/`link.py`; it treats every other meshcore file as
stdlib-only. A direct `import cryptography` in `crypto.py` would break that frozen
test. Therefore `crypto.py` imports `cryptography.fernet` lazily through
`importlib.import_module("cryptography.fernet")` and documents why. The new explicit
allowlist test (G7) records `cryptography` as the declared dependency permitted for
`crypto.py`, keeps the old rule for every other file, and keeps "meshcore never
imports clipsync". This is a recorded owner decision (D46), not an accident.

## Dependency rule (new test, G7)

Explicit allowlist, with `stdlib = sys.stdlib_module_names`:

| File | Allowed third-party roots |
|---|---|
| `hub.py`, `link.py` | `websockets` |
| `crypto.py` | `cryptography` (lazy import as above) |
| all other `meshcore/*.py` | none (stdlib + `meshcore` only) |

No meshcore module may import `clipsync` or any other app package. `pyproject.toml`
must declare both `cryptography` and `websockets` (websockets was used but undeclared
before this change).

## `clipsync/config.py` mapping

`DaemonConfig` gains, with defaults so old configs are unchanged:

```python
group_id: str = "default"
allow_insecure: bool = False
role: str = "member"                    # "member" | "relay"
serve_groups: tuple[meshcore.config.GroupConfig, ...] = ()
relay: meshcore.config.RelayConfig | None = None
# existing e2e_key stays the member's group key
```

`load_config` accepts new keys: `group_id`, `allow_insecure`, `role`,
`serve_groups`, `relay`. TOML shapes:

```toml
group_id = "family-a"
role = "member"
allow_insecure = false
e2e_key_file = "family-a.key"

[relay]
address = "ws://tvbox:8765/"

# relay-only daemon:
role = "relay"
[[serve_groups]]
id = "family-a"
token = "..."
[[serve_groups]]
id = "family-b"
token = "..."
```

`serve_groups` entries are exactly `{id, token}` (both non-empty strings); they never
carry key material. `relay` allows `address` and optional `device_id`. Unknown keys are
errors.
Validation mirrors `MeshConfig`: member + `serve_groups` is an error, relay needs
`serve_groups`, relay rejects `e2e_key`, duplicate ids/tokens are errors. `token`
stays required (old schema); a relay ignores its value and uses the per-group tokens.

`DaemonConfig.to_mesh_config()` passes the new fields through.
`DaemonConfig.uses_meshcore()` returns `bool(self.peers) or self.role == "relay"`.

`clipsync/daemon.py`:

- relay role: start the `Node` and wait for the stop signal (no clipboard, no codec);
  `--once` starts and stops immediately, returning 0;
- member with peers: `NodeTransport` + **codec=None** (meshcore owns encryption);
- member without peers: the legacy `RelayTransport` + the existing clipsync codec
  from `e2e_key` (unchanged).
- An explicitly injected `codec_factory` still wins (test seam).

## Test-side logging hygiene

`tests/conftest.py` adds a session-scoped autouse fixture that raises the
`websockets` logger to WARNING and restores it at session end. Test-side only; no
production logging changes.

G7 runs the full suite (excluding this new acceptance file to avoid recursion) in a
subprocess with `PYTHONPATH` removed and asserts the exit code is 0 and stderr contains
no `Logging error` / `I/O operation on closed file`.

## Acceptance gate

`tests/acceptance/test_meshcore_groups.py` G1–G7:

- G1 isolation: one hub serves A and B; payloads and rosters stay inside a group.
- G2 tokens: wrong group claim rejected, wrong token rejected, old client with the
  default token lands in `default`.
- G3 per-group election: killing group A's hub fails A over and leaves B untouched.
- G4 relay-only: preferred relay, relay death -> per-group fallback, restart ->
  debounced handback; relay never in a roster and has no key.
- G5 encryption: missing-key refusal/allow_insecure warning, wire opacity, wrong-key
  drop counter, forced cross-group delivery cannot decrypt.
- G6 back-compat: old single-group config and a default-group cluster behave as in
  phase 2a.
- G7 hygiene: clean subprocess stderr and the explicit dependency allowlist.

Every cross-node assertion uses `wait_until(..., timeout=8.0)`. Negative delivery
assertions wait for the positive delivery first and then observe a short quiet window
(`0.3 s`); there are no immediate cross-node assertions.
