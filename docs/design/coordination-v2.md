# meshcore + clipsync: modular design (v2.1)

`meshcore` is a small, app-agnostic network module for a handful of personal devices. `clipsync` is the first app on top of it. Notifications, settings sync etc. can be later apps on the same core. "meshcore" is a working name.

Status: decided in chat 2026-10-28. Anything not here is not decided. Open rules are in section 12.

## 0. Layers

```
  apps:      clipsync (clipboard)     notifications (later)     others (later)
               |  state channel          |  event channel
  ---------------------------------------------------------------------------
  meshcore:  channels API  |  encryption (E2E)  |  routing (who gets what)
             hub election + ordering  |  roster/hello  |  transport + discovery
             [optional: naming service]  [optional: panel]
  ---------------------------------------------------------------------------
  links:     tailnet  |  LAN (mDNS / last-known IP)  |  hotspot / any IP link
```

**Dependency rule:** meshcore never imports an app and never looks inside a payload. Apps import meshcore. A test enforces it.

## 1. meshcore: responsibilities

meshcore does: identity, ordering, hub election, reaching peers (discovery/transport), end-to-end encryption of payloads, routing preferences, two channel types, the roster, events for the app ("peer joined", "hub changed").

meshcore does not: know what a clipboard is, decide what a payload means, store app data beyond what a channel type needs.

## 2. The app interface (what an app sees)

```
mesh = Mesh(config)                         # device_id, name, key, links
ch = mesh.state_channel("clipboard")        # last-writer-wins value, converges
ch = mesh.event_channel("notifications", ttl=600)   # delivered once, queued while offline, expires

ch.publish(payload_bytes, to=None)          # None = per routing prefs
ch.on_receive(fn(sender_id, payload_bytes)) # payload is opaque to meshcore
mesh.on_peers_changed(fn(roster))
mesh.on_hub_changed(fn(hub_id))
```

- **state channel:** each write carries `{hlc, device_id}`; a newer write wins; on reconnect two devices exchange their current value and the newer one wins. Keeps the current value + a short history by hash (GC'd). HLC: `hlc = max(wall_now, highest_seen + 1)` (monotonic, no clock sync). **Design: `docs/design/meshcore-state.md`** (closes H24; frozen acceptance tests S1-S14 in `tests/acceptance/test_meshcore_state.py`; implementation is a separate later run). That design supersedes "short history": the channel keeps one value per key, and the hub keeps the latest value per key per group in memory only.
- **event channel:** each event has an id; the hub's outbox queues it for devices that are offline and drops it after `ttl`; receivers dedupe by id (LRU). An app can pair it with a state channel for flags such as "dismissed".

## 3. Identity and ordering

Every device has a persisted `device_id` (UUID, never changes) and announces `{device_id, name, perm, temp_rank, will_hub}` in its hello.

| Layer | Source | Unique? | Exists when |
|---|---|---|---|
| **perm** | the naming service (section 8) | always | device has reached it |
| **temp_rank** | set by the user (config, or the hub's local panel) | no, can collide between fragments | no perm yet |
| **unranked** | nothing | n/a | brand-new device |

**Sort key, identical on every device:**

```
(has_perm ? 0 : 1, perm ?? 0,  has_temp ? 0 : 1, temp_rank ?? 0,  device_id)
```

Perm always before temp; a temp_rank of 0 never beats a perm; device_id is only the floor. Duplicate temp ranks are not renumbered: the tiebreak picks the hub and the panel warns. Offline devices never get a perm; an offline "next number" is always a temp rank. A preferred hub (e.g. the TV box) is simply the lowest rank in config.

## 4. Hub

- Hub = first device in the sort order, among devices I can currently reach, with `will_hub = true`. If every reachable device has `will_hub = false`, the first reachable is forced (with a notification).
- Detection: heartbeat every 10 s; 3 missed (about 30 s) = unreachable. Election: probe the order top-down, 2 s timeout each; first responder is hub; nobody answers: I am hub.
- Debounce: a hub change takes effect only after 30 s of the same candidate, so a flapping device never causes migration.
- A device may decline `will_hub` (a phone on battery); it stays in the order but is skipped as a candidate.
- Partitions produce two hubs; the sort merges them when the fragments see each other. App data converges via the channels.
- The hub forwards frames and holds the event outbox. It never has the key. Its display state (roster, routing copy, event log) is rebuilt from hellos on a hub change.

## 5. Transport and discovery (works with no internet)

Implemented in phase 2b; the detailed contract is `docs/design/meshcore-lan.md`.

- A peer may carry several addresses (`tailnet`, `lan`, `hotspot`, `static`). They are
  tried in that fixed order, config order inside a kind; an old single `address` is one
  `static` fallback. Duplicates are removed. The last address that answered a real
  authenticated handshake is remembered per peer id (host:port only) and tried first
  while it is still a candidate. A dead address costs at most its short per-address
  timeout (default 2 s), so a live address later in the list is not delayed by it.
- LAN discovery is opt-in (`discovery = true`, default off): each member sends a small
  HMAC-authenticated UDP beacon every 5 s to the subnet broadcast address, to
  `255.255.255.255` and to a fixed multicast group, and listens on a fixed UDP port
  (`discovery_port`, default 8766). A beacon carries the group id, device id,
  WebSocket port, a timestamp and the mac; a wrong-group, bad-mac, stale (>30 s) or
  unknown-device beacon is ignored and counted. The sender's source IP is the address.
- Discovery only supplies addresses for device ids already in static `peers`; it never
  adds a device and never changes ranks, hub choice or membership. An entry is
  replaced when the IP/port changes and expires after 3 missed beacons (15 s at the
  default interval). A hub change or an IP change takes effect without restarting the
  daemon: the next probe stores the new address and the next link connect uses it.
- Election, failover and the channels never need the internet. Only the optional
  naming service does. A phone hotspot or plane Wi-Fi with no internet works without
  Tailscale.
- Limits: client isolation on public Wi-Fi/airports blocks broadcast and multicast, so
  discovery cannot work there (static/tailnet addresses still can); a hotspot works
  only if it does not isolate clients; two devices on different networks with no
  internet cannot reach each other. The beacon itself is unencrypted: someone on the
  same L2 sees the group id, device id and port, but without the group token cannot
  read/forge valid beacons; replay is limited to the 30 s window.
- No link at all between devices: apps stay local, state channels merge when a link
  exists.
- Test to run for real: phone hotspot, mobile data off, laptop connected; confirm sync
  and a hub failover. `scripts/lan_check.py` checks the discovery leg by hand; see the
  README section "Testing without internet".

## 6. Frame envelope and messages

Frame: `{type, channel, kind, msg_id, hlc, from, to[], ciphertext}`. Hello: `{device_id, name, perm?, temp_rank?, will_hub?}`; a hello without the optional fields means unranked with will_hub true. Roster is derived from hellos; `roster_version` exists only when a naming service is in use.

## 7. Routing and security

- **Routing:** each device owns two lists per channel, `send_to` and `receive_from` (`*` or names). Sender attaches `to`, hub forwards to those addresses and never filters, receiver checks its own list. Delivery = sender wants AND receiver accepts. The hub keeps a copy of everyone's lists only to draw the panel and warn about mismatches.
- **Encryption:** payloads are encrypted end to end between devices (Fernet today, as in crypto.py). The hub forwards ciphertext only. A device without the key can join and appear in the roster but cannot read or write.
- **Key:** handed to a new device out of band. Trust = devices with the key plus panel access. Revocation = manual key rotation (removing a device from the roster does not revoke its key).
- README notes: Android allows one active VPN; Tailscale's control plane is proprietary (Headscale is the open alternative).

## 8. Naming service [designed, build LATER, only if needed]

Separate optional component. Without it everything works with temp ranks.

Table `device_id (pk), name, perm, locked_until, pending_perm, last_seen` + a `roster_version` counter (+1 per roster change).

| Endpoint | Does |
|---|---|
| `POST /join` | one new device gets `max+1` |
| `POST /promote` | batch: ordered list of temp devices; **atomic, append after current max, idempotent** (devices that already have a perm are skipped); used when 2 or more temp devices appear together; keeps the order the user set so the hub does not flip |
| `POST /heartbeat` | clears `locked_until`, returns `pending_perm` and `roster_version` |
| `POST /reorder` | 409 if the device is locked; the panel offers "queue for reconnect" (`pending_perm`) |
| `GET /roster` | roster + `roster_version` |

Rules: **frozen when unreachable** (a device uses its cached perm order filtered to reachable members and never writes a perm; local edits are temp); **lock** (an offline device with a perm cannot be reordered or reassigned, so "goes to the back" never applies to a perm device); **roster_version** (a reconnecting device adopts the highest version it can reach). Say "naming service reachable", not "internet". Where it runs: open decision.

## 9. Panel [later]

Served by the hub on the tailnet or LAN regardless of internet. Shows reachable vs heard-about devices, ranks, routing, warnings (duplicate temp ranks, mismatched send/receive lists), and an event log. Edits perm (needs the naming service) or temp_rank (local) with the same UI; a temp_rank edit goes hub -> target device, which stores it and re-announces. The URL changes with the hub; bookmark another device's panel.

## 10. Flows

- **New device:** joins unranked (or temp if the user ranks it), goes to the back; if the naming service is reachable, `/join` gives it a perm.
- **Reconnect / rejoin:** hello with its rank; pull the roster (highest `roster_version` if a naming service exists); state channels exchange current values, newer wins; event channels deliver queued events within ttl.
- **Fragments merge:** everyone sorts the union with the key in section 3. No renumbering.
- **Promotion:** two or more temp devices reach the naming service together -> `/promote` in sort order.
- **Graceful leave:** `{"type":"leaving"}`, no election.

## 11. App: clipsync (first consumer)

- Channel: one state channel `clipboard`; payload = `{content_hash, kind}`; content stored by hash locally (current + last 3, GC'd); newer register wins and is applied to the OS clipboard.
- Echo prevention: Android tags `ClipData` with a label `clipsync:<device_id>:<msg_id>` and checks it; Wayland compares content hashes.
- Images: file + `content://` URI on the Android clipboard, bytes out of band, never base64 in a frame.
- Android: Flutter + `clipshare_clipboard_listener` + Shizuku (watchdog, wireless-debugging re-enable via `WRITE_SECURE_SETTINGS`); notification and QS tile; QS tile as manual fallback.
- Ubuntu/Wayland: existing daemon; Win11Clip bridge later (tagged history entries).
- Proximity gating (Always / Same Network / Proximity / Never) is a clipsync send rule, not a core feature; ship Always/Never first. BLE logic: check my own adapter state first (off or no permission = user choice, fail closed; scan error = broken sensor, fail open; OS-suspended = grace period then fail closed with a notification; healthy = near if the peer advertised in the last 30 s).

## 12. Open decisions

1. Where the naming service runs (on the TV box it is down whenever the box is, including transit). Prefer another always-on device, or accept it.
2. Two fragments that each set temp ranks: default is the device_id tiebreak (phase 1). Alternative: the larger fragment's order is the reference and clashing devices go to the back. Revisit in phase 2 when fragment membership exists.
3. Hub outbox: KEEP until register-on-reconnect is tested end to end (it covers hub death mid-frame).
4. Module name ("meshcore" is a placeholder) and whether it later moves to its own repo.

## 13. Layout and build phases

Layout (same repo for now): `meshcore/` (coordination.py, messages.py, later transport, channels, crypto) and `clipsync/` (app). Moving meshcore to its own repo later is a copy, because of the dependency rule.

1. **Now:** `meshcore.coordination`: sort key, hub pick/probe, debounce, hello message. Pure and tested. No website, no panel.
2. **Now:** hub = the existing relay, elected; hello/roster over the wire; LAN fallback; state channel (the existing clipboard register moves behind it).
2b. **Done (phase 2b, branch `integration/lan`):** multi-address peers, fixed try-in-order with remembered `last_good`, and opt-in authenticated LAN discovery (`discovery: true`, default off) with a stdlib UDP beacon. Contract and limits: `docs/design/meshcore-lan.md`; manual test kit: `scripts/lan_check.py`.
3. **Next:** Android app (the part that can fail).
4. Panel read-only, then temp_rank writes; routing preferences.
5. Event channel + notifications app.
6. **Only if needed:** naming service (section 8). Until then every device is unranked or temp-ranked and everything above still works.
7. Proximity (Always/Never first).

## 14. Known limitations

Intermediate state is lost while a device is offline (current value only). Clock skew is reduced by the HLC, not eliminated. A hub change means clients reconnect (about 30 s degraded). Event log is lost on hub change. State channels are last-writer-wins, so they are not for concurrent text editing or counters. Revocation is manual key rotation. Built for a handful of devices.

## 15. Groups (implemented in phase 2a / `groups`)

A **group** is `(group_id, token, e2e_key)`: a family, flat, or any independent set of
devices. One hub (typically a TV box acting as a relay-only daemon) can serve several
groups at once. This is the implemented isolation model; the rest of this document
still applies inside a group.

1. **Group id.** `group_id` (default `"default"`) is in every hello, the roster and
   the config. A node belongs to exactly one group. Old configs and old clients have
   no `group_id` and land in `"default"` with the existing token, so single-group
   behaviour is unchanged.
2. **Hub side.** The hub config has `serve_groups = [{id, token}, ...]` — ids and
   tokens only, never keys. A client authenticates with its token and claims its
   `group_id` in the hello; the hub maps `token -> group` and rejects a client whose
   claimed group does not match its token. Per group the hub keeps its own client
   set, roster and forwarding: frames are delivered only inside the sender's group,
   and rosters list only that group's members.
3. **Election is per group**, among that group's members (each node's `peers` already
   lists only its own group). One group's hub dying cannot affect another group.
4. **Relay-only role.** `role = "relay"` runs a hub for its `serve_groups`, is not a
   member of any group, is never elected, has no E2E key and never decrypts. Members
   may set `relay = {address}` as a preferred hub. Hub choice is: the preferred relay
   if reachable, otherwise the normal election among members; when the relay returns,
   the existing 30 s debounce hands the hub role back to it. A member without `relay`
   behaves exactly as before.
5. **End-to-end encryption.** Each group has its own Fernet key, configured on
   members as `e2e_key` / `e2e_key_file`; it is never given to the hub. The node
   encrypts on publish and decrypts on receive; a frame encrypted with a different
   key is dropped and counted (`dropped_undecryptable`), never delivered. A key is
   required whenever a member binds to a non-loopback address or any peer/relay
   address is non-loopback: the node refuses to start with a clear error unless
   `allow_insecure = true`, which logs a WARNING. Loopback with no key still works
   (development and the existing tests). On meshcore, meshcore's layer is the only
   encryption layer; the clipsync daemon passes no codec in mesh mode. The legacy
   single-relay path keeps using `clipsync`'s codec, so payloads are not double
   encrypted.

**Trust model.** A group is the account; there are no passwords or user accounts.
The hub operator (and the relay host) never sees plaintext: they see group ids,
device ids, roster membership, frame sizes/timing and ciphertext. The token is a
group secret and still crosses `ws://` in cleartext without TLS; prefer a tailnet or
a trusted LAN, and rotate the token + key if the network is untrusted. Removing a
device from the roster does not revoke its key — rotate the group key manually.

**Out of scope now:** a device in two groups, cross-group sharing/forwarding, and a
panel. A later panel may show only the group the operator authenticated for (it must
never join rosters across groups).
