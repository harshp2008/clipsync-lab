# meshcore state channel (LWW + HLC): design

Status: **contract amended (A1-A3), frozen acceptance tests S1-S14 committed,
implementation NOT started.**
Branch `design/state-channel` (cut from `main` after the state-isolation merge). The
contract lives in this file and in `tests/acceptance/test_meshcore_state.py` (S1-S14,
frozen: written before the implementation job, must not be edited by it).

Owner amendments applied before the implementation run:

- **A1 true HLC**: a local publish uses the highest wall ever accepted or published,
  only accepted frames raise the floor, and causality survives a clock that is behind.
- **A2 non-sticky skew handling**: every frame is re-evaluated; rejection is counted
  and logged (rate-limited), never a permanent quarantine, and a device recovers by
  itself once its clock is right (clock correction below, no restart).
- **A3 harness fix** (owner-approved): D7 asserts "at least 300 tests collected" and
  the `pytest_ignore_collect` shim is gone.

This closes H24: meshcore's hub broadcast is fire-and-forget, so a copy made before a
member attaches, or during the startup attach / hub handover window, is lost while the
clipsync engine treats it as sent. The state channel replaces that for the clipboard
register in mesh mode: **a copy is never silently lost**. After any disconnect,
attach, hub change, hub restart or partition heal every online member converges to the
same latest value.

Starting point: `coordination-v2.md` section 2 "state channel (LWW + HLC)".

---

## 1. Model: state channel vs event channel

Two channel types, one frame envelope family:

| | state channel (this design) | event channel (later, out of scope) |
|---|---|---|
| semantics | last value wins per key, not a log | delivered once, queued while offline, ttl |
| intermediate values | may be skipped | delivered in order |
| offline peer | gets the latest on attach | gets the queue |
| convergence | merge on attach, no ack, idempotent | ids + dedupe |
| payload | opaque to meshcore | opaque to meshcore |

The existing `Node.publish(payload)` / `Node.on_receive(cb)` event path is **not
touched**. Old clients keep using it, and future event-style traffic uses it. The
state API is additive:

```python
node.publish_state(key: str, payload: bytes) -> None
node.on_state(callback(key: str, sender_id: str, payload: bytes)) -> None
node.get_state(key: str) -> bytes | None
node.get_state_version(key: str) -> StateVersion | None   # (wall_ms, counter, author)
node.state_stats -> StateStats                            # frozen snapshot
```

- `publish_state` is **local-first**: it merges the value into the node's own register,
  then sends it best-effort. It never raises because the node is offline or the hub is
  gone; it *does* raise `NodeError` for a relay node, an invalid key, a non-bytes
  payload, or a value above the size cap (loud, never silent). It works before
  `start()` and after `stop()`: only the local register changes, and the next attach
  pushes it. It never triggers a connect attempt, so a publish while detached cannot
  block.
- `on_state` fires only for *received, decrypted, newly-won* values, never for the
  node's own publish (symmetric with `publish`/`on_receive`).
- `get_state` returns the latest plaintext this node knows, `None` if none.
- `state_stats` is a frozen per-node snapshot with `published, sent, received,
  applied, ignored, rejected_skew, rejected_invalid`; `sent` counts wire frames this
  node emitted (a hub broadcast counts one per recipient) and excludes the
  `state_sync` control frame.
- One key is expected in practice (`"clipboard"`). State is memory-only on every node.

## 2. Wire envelope

A state frame is a new binary frame type. The hub must compare versions without the
group key, so the header is in the clear and only the body is encrypted:

```
frame = STATE_MAGIC | u32be(len(header_json)) | header_json | body
STATE_MAGIC = b"\x00mc2s\x00"            (6 bytes, distinct from the data envelope)

header_json = {"v":1, "key":"clipboard", "hlc":[1761713834123, 0], "author":"n1"}
body        = Fernet token (group e2e key)   |   raw payload only when no key is set
```

- `hlc` = `[wall_ms, counter]`; `author` = the device that produced the value (not the
  hop that forwards it).
- The frame is *not* wrapped in the existing data envelope (`\x00mc2d\x00`): a state
  frame carries its own author/version, and an old client must be able to ignore it.
- Control frame: a state-capable client sends the compact text frame
  `{"type":"state_sync"}` immediately after `hello` on **every** connection. This is
  the capability advertisement (no `Hello` change) and the request for the group
  snapshot. An old hub ignores unknown text control types (verified in `hub.py`), so
  the frame is safe in a mixed deployment. Old clients never send it, so they never
  receive state frames (S10).
- `decode_state_frame` rejects a wrong magic, a header above 4 KiB, an unknown `v`,
  a non-integer/negative `hlc`, a bad key or an empty author. Bad frames are counted
  (`rejected_invalid`) and dropped, never forwarded.

### What the hub can see (plainly)

The hub (any hub, including a relay-only box) can see: group id, device ids, the
**state key names** (e.g. `clipboard`), the **author** of each value, the **HLC
version** (wall-clock milliseconds + counter), the **ciphertext size**, and the
**timing** of every value. It can compare versions and keep one ciphertext per key per
group. It **never** sees the payload: that is Fernet ciphertext under the per-group
e2e key, which the hub is never given (`role = "relay"` forbids `e2e_key`). A hub can
drop, delay or replay frames; it cannot read or forge a payload it can decrypt, and
replay is harmless (LWW ignores older versions). A hub can withhold the latest value,
which no transport can prevent.

## 3. Versioning and total order

Every value carries `StateVersion(wall_ms, counter, author)`.

- **HLC per node**, from an injectable wall clock (`Node(config, wall_clock=...)`,
  default `time.time`). The node keeps two things:
  - `accepted_floor = (wall_ms, counter)`: the greatest version it has *accepted* from
    another device. Only accepted frames move it, and only forward. A rejected (skew)
    frame never touches it.
  - `last = (wall_ms, counter)`: the greatest wall ever accepted or published by this
    node (normally the maximum of `accepted_floor` and its own last publish).
  - publish: `w = max(now_ms, last.wall)`; `counter = last.counter + 1 if w ==
    last.wall else 0`; `last = (w, counter)`. The node also remembers `own_wall = w`,
    the wall of its own last publish.
  - accept a frame with version `v`: first check the skew bound; if accepted, set
    `accepted_floor` to the greater of itself and `v` (equal wall: greater counter),
    and `last = max(last, accepted_floor)`. A *rejected* version never advances either
    clock, so an outlier cannot drag honest clocks forward.
- **Call it one rule (A1).** `last` is the floor: a local publish is always at least as
  new as every version this node has accepted or published. After B accepts A's copy
  `v`, B's next copy has `wall >= v.wall`, and when the wall is equal its counter is
  greater, so **B's next copy always wins even if B's clock is behind A's**. A
  far-future frame that B rejects never raises the floor, so it cannot make later
  honest copies lose.
- **Clock correction (A2).** The floor normally only moves forward. The one exception
  is a proven clock fault: if the node's own last published wall is more than
  `MAX_CLOCK_AHEAD_S` ahead of its current wall clock, its clock has moved backwards
  past its own version. That self-authored future version can never be accepted by an
  honest peer, so it is discarded: `last` is rebuilt from `accepted_floor` (never
  below it) and the local register entry this node authored at that future wall is
  replaced by the next local publish. Causality against accepted frames still holds
  because the rebuild is bounded by `accepted_floor`; a rejected frame still never
  raises anything. This is what lets a +10 min device recover without a restart once
  its clock is fixed.
- **Total order**: `(wall_ms, counter, author)`, ascending; the greatest version wins.
  Identical version → keep the existing value (idempotent replay).
- **Deterministic winner for concurrent copies**: two values produced in the same
  millisecond with counter 0 are ordered by `author`, greatest device_id wins. Both
  arrival orders converge to the same winner (S4).
- **Applying a received state never changes its version** and never bumps the local
  clock into a new version; only a local `publish_state` mints one.

No sequence numbers are needed, and none are added:

1. A register value is self-describing and idempotent; the receiver only compares
   versions, so duplicates and reordering are harmless.
2. Gaps are *intended*: state is "latest value", not a log. A missed intermediate
   value must be skipped, not recovered.
3. Reliability comes from attach (push + snapshot), not from acks: every connection
   re-pushes the local register, and every attach pulls the hub's register. A frame
   lost in flight is covered by the next attach; a frame lost at the hub (crash)
   is re-pushed by every member that has the value.
4. Sequence numbers would add a second ordering next to the HLC, and two orderings
   can disagree after a hub restart.

## 4. The hub register (per group, memory only)

`HubServer` keeps, for each group it serves:

```
group_id -> key -> StateEntry(version, ciphertext, frame_bytes)
```

- **One value per key per group.** Bounded by `MAX_STATE_KEYS_PER_GROUP = 16` and
  `MAX_STATE_PAYLOAD_BYTES` (8 MiB), i.e. at most 128 MiB per group in the worst case;
  `"clipboard"` uses one slot. The 17th key is refused (no store, no forward), logged
  and counted; it never silently evicts another key.
- **Memory only, never disk.** A hub restart loses the register by design. Members
  re-push on attach and everyone converges (S6). No plaintext and no ciphertext is
  ever persisted by meshcore; the group key is never at the hub.
- A member that becomes the hub hands its own local register to the hub (same
  `StateRegister` object), so a handover starts from the newest value that device
  knows, not empty.
- A relay (`role = "relay"`) serves several groups with one register per group; it has
  no local register and no state API (`publish_state` raises).

Hub merge rules for a frame received from a client:

1. validate the frame; `author` must equal the sender's hello device id (a member may
   not write under another member's name through the hub);
2. clock bound: reject if `version.wall_ms > now_ms + MAX_CLOCK_AHEAD_S * 1000`;
3. merge LWW; **forward to the other state-capable clients of the same group only on
   a win**; never echo to the sender;
4. a losing or duplicate frame is counted and ignored (no corrective reply; the
   attach snapshot already delivered the winner).

Hub sends its register to a client that sends `state_sync`: one frame per key, exactly
once per attach, to that client only. State frames are sent only to clients that
proved state capability; legacy clients keep receiving legacy broadcasts only.

## 5. Flows

### 5.1 Attach (first time, reconnect, hub change, hub restart)

```
client                          hub
  |-- hello -------------------->|  roster broadcast (existing)
  |-- {"type":"state_sync"} ---->|  mark client state-capable
  |                              |  snapshot: one state frame per key
  |<------------ state(key) -----|
  |-- state(key,my version) ---->|  merge (LWW, clock bound)
  |                              |  forward on win to other capable clients
  |                              |  apply locally if the hub is also a member
```

The push happens on **every** successful connection (`Link.on_connected`, worker
thread), not on roster changes, so the frame count is bounded and a reconnect is
always covered.

### 5.2 Publish (attached)

```
A (publisher)                    hub                      B
  |-- state(key,hlc,ct) -------->|                        |
  |                              | merge win -> store     |
  |                              |-- state(key,hlc,ct) -->|  decrypt, merge win,
  |                              |                        |  on_state(key,"A",payload)
```

A hub that is also a member (no relay) applies the value locally and fires its own
`on_state` for a client's winning value; a relay never decrypts.

### 5.3 Publish while detached (hub dead, gap, partition, offline)

`publish_state` writes the local register (its own HLC is monotonic, so the newest
local copy wins) and skips the send when there is no live link. Nothing is raised and
nothing is queued in a separate outbox: **the register is the outbox**. On the next
attach the whole local register is pushed (one frame per key), so intermediate copies
that were superseded while offline are skipped by construction, never resurrected
(S3), and a copy made in a handover gap is delivered once without a retry (S2).

### 5.4 Handover / hub dies

```
hub0 dies -> publish on A (local only) -> elect hub1
  A --attach--> hub1: sync + push(A's latest)
  hub1 merges; if hub1 is a member it also applies locally
  hub1 forwards the winning value to the other attached members
```

Members that were attached to hub0 reconnect (existing link/elect loop); their push is
the repair. A member that became the hub takes its own register with it.

### 5.5 Partition heal

Two fragments each have a hub. On heal, the sort order keeps one hub (section 3 of
`coordination-v2.md`); the other fragment's members reconnect to it and push. The
demoted hub is a member again and pushes its register too. LWW picks one value; the
loser is superseded on every node. No renumbering, no merge list, no history.

### 5.6 Hub restart

A new hub process starts empty (S6 asserts `get_state` is `None` before members
attach). Members reconnect, push, and the hub's register is rebuilt from their
registers. If the only member holding the newest value also restarted, its local
clipboard re-seeds the register on the next clipsync poll (section 7); if every device
restarted at once, the newest value exists only on the user's clipboards and the
first poll re-publishes it. meshcore never persists state.

## 6. Clock skew

Rule, applied identically by every receiver **and** by the hub before storing or
forwarding:

> Reject a state version whose `wall_ms` is more than **300 seconds ahead** of the
> receiver's wall clock (`MAX_CLOCK_AHEAD_S = 300`). Older versions are never
> rejected for being old (LWW already ignores them).

- **Non-sticky (A2).** The check is made on **every** frame; there is no quarantine
  list, no ban and no restart requirement. A frame within the bound is accepted even if
  an earlier frame from the same author was rejected. A device one hour ahead cannot
  win at all while its clock is wrong: every honest node rejects its frames, nothing is
  stored or forwarded (S5), and its later copies stay rejected only for as long as its
  clock stays wrong.
- A rejected frame increments `state_stats.rejected_skew` and logs a `WARNING`, rate
  limited to **once per 60 s per author**, with the measured lead, e.g. `rejected state
  clipboard from n2: version 3599 s ahead of local clock (max 300 s)`. A rejected
  frame never raises the receiver's HLC floor (A1), so it cannot make later honest
  copies lose (S14).
- **Recovery (no restart).** A device whose clock was wrong recovers by itself as soon
  as its clock is right:
  - a device that was *behind* (e.g. a laptop just resumed from sleep) jumps forward;
    its next publish uses the corrected `now` and is accepted;
  - a device that was *ahead* has minted a future version of its own. When its clock
    moves back by more than the bound, the **clock correction** in section 3 discards
    that self-authored future floor, keeps the accepted floor, replaces the local
    poisoned value with the next local publish and sends it; the hub accepts it on the
    next frame (S13). If peers had accepted the future value (only possible when their
    clocks were ahead too) LWW still holds the future value until the walls catch up;
    that is a deployment-wide clock fault, named by the WARNINGs.
- The bound is not a clamp: clamping a version at the receiver would give different
  registers on different nodes and break convergence. Rejection is deterministic as
  long as honest clocks agree within the bound (they do on any NTP'd or hotspot
  device); a slow hub rejects honest copies and is a deployment error the WARNING
  names (see failure modes).
- No new clock sync is introduced; the HLC keeps ordering monotonic per device and
  preserves causality across an accepted hand-off (A1).

## 7. Size, memory bounds and what happens above them

- `MAX_STATE_PAYLOAD_BYTES = 8 * 1024 * 1024` (8 MiB) of **plaintext** per value.
  Fernet adds ~100 bytes and base64 inflates by 4/3, so 8 MiB encrypts to ~11.19 MB,
  just under `DEFAULT_MAX_FRAME_BYTES` (11,193,004). The encrypted frame must also fit
  the node's `max_frame_bytes`; both checks are made in `publish_state`.
- Above the cap: `publish_state` raises `NodeError("state value too large: ...")`
  immediately, before touching the register or the wire. Nothing is truncated or
  dropped silently, the local register is unchanged and the node stays healthy (S9).
  The clipsync state transport maps this to `FrameTooLarge(TransportError)`; the engine
  drops that frame, increments `rejected_too_large`, logs a warning and continues
  (never an infinite outbox retry).
- Hub memory: one value per key per group, at most 16 keys per group. A 17th key is
  refused with a warning. No history, no per-peer queues, no log.
- Keys: `1..64` chars matching `[a-z0-9][a-z0-9._-]*`. Nothing else is accepted.
- Images: the clipsync state payload is the clipsync wire frame (base64 JSON), so on
  mesh the 8 MiB cap allows images up to roughly **6 MiB**. Larger images are refused
  loudly at the publisher. Chunking/large images are out of scope for this design.

## 8. Echo and loops

1. The hub never sends a state frame back to the connection it came from.
2. Applying a received state **never** re-publishes it: `on_state` is a callback, not a
   send. The app decides; clipsync only writes its clipboard.
3. Applying a received state never mints a version (the stored version is the received
   one), so two nodes echo-free converge and stop.
4. The attach push happens once per connection and only for keys in the local
   register; a snapshot is one frame per key. Wire traffic per publish is at most
   `1` frame from the publisher plus `1` per other capable group member; a settle
   window in S7 asserts this stays bounded.
5. `state_sync` is a text control frame, not counted as a state frame.

## 9. clipsync on top of the state channel (design only)

**Mesh mode only.** The legacy single-relay path (`RelayServer` + `RelayTransport`)
keeps the old broadcast and the clipsync codec byte-for-byte; old clients and old
configs take that path unchanged (S10, existing A9).

- New adapter `clipsync/state_transport.py: MeshStateTransport(node, key="clipboard")`
  implements the existing `Transport` protocol:
  - `send(frame)` → `node.publish_state("clipboard", frame)`; `NodeError` is mapped to
    `TransportError` (`FrameTooLarge` for the size cap). It never fails because the
    node is detached: the state channel owns the frame from that moment.
  - `recv()` → drains payloads delivered by `node.on_state` (decrypted by meshcore).
  - `connect()/close()` start/stop the underlying `Node`; `connected` reflects that.
- `clipsync/daemon.py` mesh branch builds `MeshStateTransport` instead of
  `NodeTransport`, and still passes `codec=None` (meshcore owns encryption, no double
  encryption).
- **The engine's "sent" logic changes meaning** (in state mode): `SyncStats.sent` no
  longer claims delivery, it counts frames **published to the state channel**. The
  engine pops the frame because the channel owns it, keeps no per-frame ack and
  performs no retry; delivery is the channel's job: the local register is re-pushed on
  every attach and the hub's register is pulled on every attach. A transient link
  failure must not stall or retry the outbox (S11: delivered once, no retry, no
  duplicate write). The relay path keeps today's semantics ("handed to the
  transport"). The engine docstring and `SyncStats` comment are updated accordingly;
  the frozen tests' meaning of `sent` on the relay path is unchanged.
- Echo: meshcore's rule 8.2 means a received state is written once to the clipboard
  and not re-published; the engine's existing `_last_local_hash` + content-hash check
  is the second guard, so a snapshot + forward duplicate produces exactly one write.
- Startup: the engine's first poll reads the local clipboard with
  `_last_local_hash is None`, so a restarted daemon re-seeds the register from the
  user's actual clipboard. That is also the bootstrap after "everyone restarted".

### Smoke script (change described here, not edited in this run)

`scripts/smoke_two_processes.py` currently retries a lost copy and reports
`TEXT-RETRY` / `FAILOVER-RETRY` (H24). Once the state channel is implemented, the
mesh-mode daemons use it, so the script changes to:

- write the text **once** before attach and wait bounded (15 s); a timeout is
  `TEXT-FAILED`, no second copy;
- print `TEXT-RETRY 0` (and fail if the delivery needed more than one attempt);
- after the relay is killed, write **once** during the handover and wait bounded;
  print `FAILOVER-RETRY 0` on success;
- the PASS message states that single-copy delivery was verified.

Expected after the implementation run: `TEXT-RETRY 0`, `FAILOVER-RETRY 0`, PASS
20/20. The frozen suite's `test_deploy_smoke.py` runs the script through its own
assertions, so the implementation run must keep those green.

## 10. Security

- State travels under the same per-group e2e key as the legacy broadcast
  (`GroupCipher`, Fernet). The hub is never given the key; a relay has none.
- The hub sees metadata only (section 2): key names, authors, HLC versions, ciphertext
  sizes, timing, group id. It never sees payloads. S8 taps a group client at the hub
  and asserts the plaintext is absent from the frame while key/version/author are
  visible.
- A frame from a member carrying another member's `author` is rejected by the hub
  (author must equal the sender's hello id). The hub is the only forwarder, so this is
  the complete check.
- A node with the wrong key never applies a state (decryption fails) and increments
  `dropped_undecryptable` (S8). Limitation, documented: a member with the correct
  token but the wrong e2e key can publish ciphertext the hub cannot validate; the hub
  may store it as the newest value, honest members drop it (`dropped_undecryptable`)
  and never apply it. Unlike a misclocked device (which recovers frame by frame, no
  restart), a wrong-key device stays unable to apply anything until its key is fixed
  and it restarts. (A key fingerprint in the clear header was
  considered and rejected for this iteration: it needs a new hub config field and
  leaks a key fingerprint.)
- Two groups on one relay are fully isolated: one register per group, forwarding only
  inside the sender's group (S8).
- Groups/tokens/TLS/naming service are unchanged; the token still crosses `ws://` in
  cleartext without TLS (H15).

## 11. Failure modes

| Failure | What the channel does | What the user / app sees |
|---|---|---|
| publish before anyone attaches | local register keeps it; pushed on attach | copy arrives when the member attaches, latest only |
| publish during handover gap | local register; push on reconnect | final value reaches everyone, once, no retry (S2) |
| frame lost in flight / link drops | connection closes, link reconnects, register re-pushed | maybe a delay; never a lost latest value |
| hub dies mid-forward | links close; new hub elected; members push | converge via LWW |
| hub restart (empty) | members push on attach; hub re-learns | converge, no manual step (S6) |
| member offline for a while | only its latest local value exists there | older copies are skipped, never resurrected (S3) |
| receiver missed values | hub keeps one value per key | snapshot on attach |
| device clock > 300 s ahead | every frame re-checked, rejected, counted, rate-limited WARNING; no quarantine | that device is temporarily unable to win; honest copies converge (S5) and it recovers once its clock is fixed, no restart (S13) |
| own clock jumped backwards (resume, NTP) | clock correction drops the self-authored future floor, accepted floor kept | next copy is accepted; no restart, no manual step (S13) |
| hub more than 300 s **slow** | rejects honest copies (they look ahead) | WARNING `rejected_skew` on the hub; fix the hub clock/NTP |
| wrong e2e key member | receiver drops + `dropped_undecryptable` | that member never applies state; hub may cache its ciphertext (limitation above) |
| value above 8 MiB | `NodeError` before send; engine drops + warns | loud error, node healthy, clipboard not corrupted (S9) |
| > 16 keys in a group | hub refuses the new key, warns | app misuse; `"clipboard"` unaffected |
| old client in the group | never sent state frames | legacy broadcast sync unchanged (S10) |
| all devices restart at once | registers empty | each daemon re-seeds from its own clipboard on first poll |
| a copy that loses an exact tie | LWW keeps the higher `author` | one copy wins everywhere; the loser is superseded, not lost by the channel |

## 12. Decisions

Status: **decided** = made in this design; **asked-owner** = the account owner must
confirm (also listed in the final report).

| # | Decision | Status |
|---|---|---|
| D1 | State API on `Node`: `publish_state` / `on_state` / `get_state` / `get_state_version` / `state_stats`; payload-opaque, LWW per key, memory only | decided |
| D2 | Separate binary state frame (new magic) with a clear header `{key, hlc, author}` and encrypted body; data envelope and `Hello` untouched | decided |
| D3 | Capability + snapshot via the text control `{"type":"state_sync"}` sent after every `hello`; no `Hello` field change (old clients/hubs unaffected) | decided |
| D4 | HLC `(wall_ms, counter)` per node, injectable wall clock (`Node(wall_clock=...)`); total order `(wall_ms, counter, author)`, greatest wins | decided |
| D5 | No sequence numbers; attach push + snapshot is the reliability mechanism; gaps are intended | decided |
| D6 | Hub keeps one ciphertext + version per key per group, forwards only on a win, never echoes to the sender | decided |
| D7 | Hub register is **memory only**, lost on restart; members re-push; no meshcore state on disk | decided (owner accepted) |
| D8 | Member-hub shares its local register with the hub; relay keeps one register per served group with no key | decided |
| D9 | Clock bound: reject versions > 300 s ahead of the receiver's clock, count + WARNING (once per 60 s per author), no clamp; rejection is per frame, never a permanent quarantine, and a device recovers once its clock is right without a restart | decided (owner accepted, A2) |
| D10 | Max state payload 8 MiB plaintext (and the encrypted frame must fit `max_frame_bytes`); above it `NodeError` at the publisher | decided (owner accepted) |
| D11 | Memory bound: at most 16 keys per group, one value each; extra keys refused with a warning | decided |
| D12 | Applying a received state never bumps the version and never re-publishes; no self callback on `publish_state` | decided |
| D13 | New `clipsync/state_transport.py` (`MeshStateTransport`) for mesh mode only; legacy relay path unchanged | decided |
| D14 | Engine `sent` means "published to the channel" in state mode; no ack, no retry, no outbox stall; `FrameTooLarge` dropped loudly | decided |
| D15 | State logic lives in existing meshcore modules (`coordination.py` ordering/register, `messages.py` frames, `node.py`, `hub.py`, `link.py`); **no new `meshcore/*.py`** because frozen G7/A10 allowlists would fail without an owner-approved frozen-test edit | decided (forced) |
| D16 | Hub rejects a member frame whose `author` differs from the sender's hello id | decided |
| D17 | Wrong-key members are quarantined at the receiver (apply nothing, count it); hub may cache their ciphertext (accepted limitation) | decided |
| D18 | Images on mesh are limited to ~6 MiB by the 8 MiB state cap; chunking out of scope | decided (owner accepted) |
| D19 | 300 s bound is relative to the receiver; honest clocks must agree within the bound for a consistent decision near the edge; keep the hub NTP'd | decided (owner accepted) |
| D20 | `publish_state` works before `start()` and after `stop()`, never blocks on a connect, and never raises because the node is detached | decided |
| D21 | **True HLC (A1)**: the floor is the highest wall ever accepted or published; only accepted frames raise `accepted_floor`; publish takes `max(now, floor)` with the counter rule; causality means B's next copy wins after accepting A's even when B's clock is behind | decided (owner mandated) |
| D22 | **Non-sticky skew (A2)**: check every frame against the local wall, no quarantine list, no restart; count `rejected_skew`, WARNING rate-limited once per 60 s per author; rejected frames never move the floor | decided (owner mandated) |
| D23 | **Clock correction** (needed for A2's "recovers as soon as its clock is right"): if the node's own last publish is > 300 s ahead of its current clock, drop that self-authored future floor, rebuild from `accepted_floor`, and replace the local self-authored poisoned entry on the next publish. The only case where the floor goes down; accepted causality is preserved | decided |
| D24 | **Harness fix (A3, owner-approved)**: D7 asserts at least 300 collected and the `pytest_ignore_collect` shim is removed, so the historical count is honest and the new files are always visible | decided (owner mandated) |

## 13. Frozen acceptance tests (S1-S14)

`tests/acceptance/test_meshcore_state.py` (in-process `Node`s, loopback only, ports
from `free_port()` / bind-to-0, `FakeClipboard` only, injected clocks, every cross-node
assertion through `wait_until(..., 8.0)`):

| Test | Proves |
|---|---|
| S1 | a value published before B ever attached is delivered as the attach snapshot |
| S2 | handover: the hub dies, A publishes once during the gap, the new hub comes up and every online member ends on A's value (no retry) |
| S3 | offline publishes v1..v3 converge to v3 on reconnect; the older ones are skipped, never resurrected |
| S4 | concurrent A/B copies with the same injected clock converge to the deterministic `author` winner in both arrival orders |
| S5 | the 300 s bound: +240 s accepted, +360 s and +1 h rejected, later honest copy wins, bound constant asserted |
| S6 | hub restart: a fresh hub starts empty, members re-push on attach, all converge, no manual step |
| S7 | no echo loop, no version bump, `on_state` once, wire frame count bounded by a small constant |
| S8 | two groups on one relay stay isolated; a tapped frame carries no plaintext but shows key/version/author; a wrong-key node never applies and increments `dropped_undecryptable` |
| S9 | an oversized value is refused loudly at the publisher and the node stays healthy |
| S10 | old clients (no `state_sync`) never receive state frames; legacy `publish`/`on_receive` still works both ways |
| S11 | three clipsync daemons on meshcore with `MeshStateTransport`: a copy during a hub failover is delivered once, with no retry and no duplicate write |
| S12 | **A1 causality under skew**: A is 120 s ahead of B; A copies, B receives, B copies 1 s later (and the reverse direction); every node ends on B's value, B's version is strictly greater even though B's clock is behind |
| S13 | **A2 recovery**: a +10 min device is rejected and counted; its injected clock is fixed and its next copy is accepted by the hub and applied everywhere, with no restart |
| S14 | **A1 floor safety**: a rejected +1 h frame does not move the hub's floor; a later honest copy still wins and every node converges to it |

Not covered by the frozen tests (the implementation run adds its own unit tests): frame
codec edge cases, key validation, HLC monotonicity and clock correction, WARNING rate
limiting, register bounds, `FrameTooLarge` in the engine, daemon mesh wiring, and the
updated smoke script.

While this contract is red, one existing meta-test is red too: G7's clean-shell test
runs the whole suite in a subprocess and asserts a zero exit code, so it necessarily
fails until the implementation makes S1-S14 green. That is a consequence of the
contract being red, not a broken existing test, and no test was edited for it; the
implementation gate must see the full suite green (G7 included).

## 14. Out of scope

Event channel / notifications; Android; TLS and token transport security; the naming
service; the panel; conflict UI; history or log of clipboard values; multi-key CRDTs;
cross-group sharing; compression, chunking or on-disk persistence of state.

## 15. Implementation notes (for the later run)

Expected changes: `meshcore/coordination.py` (HLC with accepted floor + clock
correction, `StateVersion`, `StateEntry`, `StateRegister`, `StateStats`, limits,
~+180 lines), `meshcore/messages.py` (`STATE_MAGIC`, state frame + `state_sync` codec,
~+80), `meshcore/node.py` (API, local register, attach push, receive path, skew check,
rate-limited WARNING, stats, ~+180), `meshcore/hub.py`
(register wiring, snapshot, merge/forward, author check, per-frame skew check, ~+120),
`meshcore/link.py` (`on_connected`, `send_control`, ~+30),
`clipsync/state_transport.py` (new, ~+90), `clipsync/transport.py`
(`FrameTooLarge`, ~+5), `clipsync/engine.py` (drop-on-too-large, docstrings, ~+15),
`clipsync/daemon.py` (mesh wiring, ~+8), `scripts/smoke_two_processes.py` (single copy,
0 retries), plus `tests/test_meshcore_state.py` unit tests and the smoke re-run.

Frozen-test constraint for the implementation job: `git diff <frozen-tests-commit> --
tests/acceptance` must be empty (`<frozen-tests-commit>` is the contract commit with
S1-S14). If a G7/A10 conflict appears anyway, stop and report it, do not edit a frozen
test.
