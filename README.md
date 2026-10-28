# clipsync-lab

Investigation + prototype for reliable **two-way clipboard sync (text and images)**
between an Ubuntu GNOME **Wayland** laptop and an **Android** phone.

Status: research complete; laptop-side app complete (resilient daemon, relay command,
config, peer tool, tests). The phone-side steps are documented but **untested** (no
phone access in this run); anything needing an account, the phone, or system changes is in
[`docs/orchestrator/NEEDS_HUMAN.md`](docs/orchestrator/NEEDS_HUMAN.md).

## What is here

| Path | What |
|---|---|
| [`docs/research/clipboard-sync.md`](docs/research/clipboard-sync.md) | Final synthesis: why sync breaks, 10-option comparison, recommendation + fallback, privacy note. |
| [`docs/research/agy-options-landscape.md`](docs/research/agy-options-landscape.md) | Raw agy survey (kept for provenance; reviewer corrections at the top). |
| [`docs/design/interfaces.md`](docs/design/interfaces.md) | Frozen module contracts that let separately-owned chunks fit together. |
| `clipsync/protocol.py` | Wire format, strict validation of untrusted frames, content hashing. |
| `clipsync/clipboard.py` | Backend interface, `FakeClipboard`, `WlClipboardBackend` (never executed here). |
| `clipsync/transport.py` | `InMemoryTransport` + `FileTransport` fakes carrying opaque byte frames. |
| `clipsync/crypto.py` | Optional Fernet E2E codec + scrypt key derivation. |
| `clipsync/engine.py` | Two-way loop: echo prevention, dedup, ordering, reconnect/outbox. |
| `clipsync/relay_server.py` / `relay_transport.py` | Loopback-only WebSocket relay and its synchronous client transport. |
| `clipsync/config.py` | TOML config loader for the daemon; token/E2E key may come from files. |
| `clipsync/daemon.py` | `ClipboardDaemon` loop and the `clipsync` console command. |
| `clipsync/relay_cli.py` | `clipsync-relay` console command. |
| `clipsync/peer.py` | `clipsync-peer` console command: second device on the relay (`send-text`, `send-image`, `listen`). |
| [`docs/android-side.md`](docs/android-side.md) | Exact phone steps (UNTESTED). |
| [`docs/orchestrator/`](docs/orchestrator/) | Plan, decisions, needs-human, final report. |

## Quickstart

`clipsync` is the laptop daemon that wires a clipboard backend to the relay,
`clipsync-relay` is the loopback-only relay server, and `clipsync-peer` is a second
device on the relay (send text/images, or listen and save received images). From the
repo root, install the console commands once (the runtime dependency is already in
`pyproject.toml`):

```bash
python3 -m pip install --user -e .
```

Without installing, the same commands work as `python3 -m clipsync.daemon ...`,
`python3 -m clipsync.relay_cli ...` and `python3 -m clipsync.peer ...`.

> **Security:** the shared bearer token and the optional E2E key must stay out of git.
> Keep them in `~/.config/clipsync/` with `chmod 600`; never commit or screenshot them.

### 1. Start the loopback relay

Start `clipsync-relay` on loopback (default port 8765):

```bash
mkdir -p ~/.config/clipsync
# Long random token, kept out of git
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.config/clipsync/token.txt
chmod 600 ~/.config/clipsync/token.txt

# Run the loopback-only relay (port 0 picks an ephemeral port)
clipsync-relay --token-file ~/.config/clipsync/token.txt --port 8765
```

### 2. Configure the daemon

Create `~/.config/clipsync/config.toml`:

```toml
device_id = "laptop"
relay_url = "ws://127.0.0.1:8765/"
token_file = "token.txt"
# Optional end-to-end encryption: same key on every peer, never committed.
# e2e_key_file = "e2e.key"
poll_interval = 0.5
```

To enable E2E, generate a Fernet key and point the config at it:

```bash
python3 -c "from clipsync.crypto import FernetCodec; print(FernetCodec.generate().key.decode())" \
  > ~/.config/clipsync/e2e.key
chmod 600 ~/.config/clipsync/e2e.key
```

Relative `token_file` / `e2e_key_file` paths resolve against the directory containing
`config.toml`.

### 3. Run the daemon

Run a one-shot sync to test connectivity and output JSON statistics:

```bash
clipsync --config ~/.config/clipsync/config.toml --once
```

Or run the continuous laptop daemon:

```bash
clipsync --config ~/.config/clipsync/config.toml
```

## Two groups on one TV box

One relay-only daemon can serve several independent groups from the same process. A
group is `(group_id, token, e2e_key)`; the TV box gets only the ids and tokens, never
the keys. Your devices sync only with their own group, and your mother's only with
hers. The normal single-group Quickstart above is unchanged.

> Use a private tailnet or a trusted LAN for these addresses. The tokens still cross
> `ws://` in cleartext because TLS is not implemented yet; the E2E keys stay on the
> member devices. Never put a real token or key in git or a screenshot.

### 1. Generate one Fernet key per group (on a member, not the TV box)

```bash
mkdir -p ~/.config/clipsync
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  > ~/.config/clipsync/family-a.key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  > ~/.config/clipsync/family-b.key
chmod 600 ~/.config/clipsync/family-*.key
```

Also generate two long random tokens (one per group), for example with
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. The TV box and the
members of that group share the same token.

### 2. Relay-only config on the TV box (`relay.toml`)

```toml
device_id = "tvbox"
role = "relay"
bind = "<tvbox-tailnet-or-lan-ip>"
port = 8765

# The schema still requires a top-level token; a relay ignores it and uses the
# per-group tokens below. These entries are ids + tokens only, never keys.
token = "not-used-by-the-relay"

[[serve_groups]]
id = "family-a"
token = "<random-token-a>"

[[serve_groups]]
id = "family-b"
token = "<random-token-b>"
```

Start it with `clipsync --config relay.toml`. It listens once for both groups, never
appears in either roster, is never elected, has no E2E key and forwards only opaque
ciphertext within each group.

### 3. Member config (`laptop-a.toml`, one per device)

```toml
device_id = "laptop-a"
name = "Laptop A"
group_id = "family-a"
token = "<random-token-a>"
e2e_key_file = "family-a.key"
bind = "<laptop-a-tailnet-or-lan-ip>"
port = 8766

# Preferred hub: the TV box relay. If it is unreachable, the members elect a hub
# among themselves (the peers below), and hand back after the debounce when it
# returns.
[relay]
address = "ws://<tvbox-tailnet-or-lan-ip>:8765/"

[[peers]]
device_id = "phone-a"
name = "Phone A"
address = "<phone-a-tailnet-or-lan-ip>:8766"
```

The second family uses a separate file with `group_id = "family-b"`, its own token,
its own `family-b.key`, and its own `peers`; both families may point at the same TV
box address. Every member needs the group key whenever any address is non-loopback,
so the daemon refuses to start if `e2e_key` is missing (use `allow_insecure = true`
only for a deliberate test, which logs a WARNING).

What the operator can and cannot see: the TV box (and anyone who can read its config)
sees group ids, device ids and roster membership, frame sizes/timing and ciphertext.
It cannot read or forge clipboard plaintext without the group key. A device in two
groups and cross-group sharing are out of scope; a group is the account, with no
passwords.

## Testing without internet

Phase 2b adds two independent mechanisms:

- **Several addresses per peer** (`addresses`) tried in the fixed order
  `tailnet`, `lan`, `hotspot`, `static`, with the last working `host:port`
  remembered in `state_dir` (default `$XDG_STATE_HOME/meshcore`, usually
  `~/.local/state/meshcore`). A dead address costs at most its short timeout.
- **Opt-in LAN discovery** (`discovery = true`, default **off**) using a stdlib UDP
  beacon on `discovery_port` (default 8766). It only fills in a `lan` address for a
  device id already listed in static `peers`; it never adds devices or changes the
  hub election. Beacons are HMAC-authenticated with the group token.

A member config for a hotspot (no internet, no Tailscale) can look like this:

```toml
device_id = "laptop"
name = "Laptop"
group_id = "family"
token = "<long random token>"
e2e_key_file = "family.key"      # required for any non-loopback address
bind = "0.0.0.0"
port = 8766                      # TCP hub port
discovery = true
discovery_port = 8766            # UDP; TCP and UDP 8766 do not collide
# state_dir = "~/.local/state/meshcore"   # default

[[peers]]
device_id = "phone"
name = "Phone"
address = "100.64.0.2:8765"      # optional old tailnet fallback
addresses = [
  { kind = "lan", host = "192.168.1.23", port = 8765 },
]
```

### Hotspot procedure (two machines, no internet)

1. On the phone: turn **mobile data off**, then enable the **hotspot**.
2. Connect the laptop to that hotspot. Do not connect the phone to anything else.
3. On both devices run meshcore/clipsync with the **same group** (`group_id` and
   `token`; the same `e2e_key` too if any address is non-loopback) and
   `discovery = true`. Their old `tailnet`/`static` addresses may be dead; discovery
   supplies the current LAN IP.
4. Wait about 10 s, then confirm the peers see each other and a clipboard sync
   arrives. To test a hub failover, stop the hub, wait for the debounce, and sync
   again.

### Discovery-only check (`scripts/lan_check.py`, stdlib only)

This checks the discovery leg without meshcore/clipsync running. Run one command on
each machine, with the same group and token:

```bash
# machine A
python3 scripts/lan_check.py listen --group family --token '<group token>'

# machine B
python3 scripts/lan_check.py probe  --group family --token '<group token>'
```

Both sides send their own authenticated beacons and listen. Each prints every beacon
heard (source IP, device id, group, port) and the measured time to first sighting;
after 20 s (`--duration`) each prints `PASS` (heard at least one valid beacon from the
other device) or `FAIL`, and exits 0/1. Add `--port 8766` if you changed
`discovery_port`, and `--hub-port 8765` to control the advertised WebSocket port.

**Expected result:** within a second or two each side prints the other's source IP and
`PASS` after 20 s. A `FAIL` with no beacons means broadcast/multicast is blocked or the
two devices are on different networks; a `FAIL` with only your own beacons means the
other side's traffic does not reach you.

**Report a failure with:** both commands and their complete output, the operating
systems, whether the network is a hotspot/public Wi-Fi/home LAN, whether the hotspot
has "client isolation" enabled, `ip addr` / `ip route` on both machines, and whether
`discovery_port` is free (UDP).

**Limits (what this cannot fix):** public Wi-Fi and airports often enable client
isolation, which blocks broadcast and multicast (and often direct peer traffic);
a hotspot works only if it does not isolate its clients. Two devices on different
networks with no internet cannot reach each other — no discovery can bridge that.
mDNS/Bonjour-style names are also out of scope; this is a small authenticated UDP
beacon, not a naming service. Discovery is unencrypted metadata: anyone on the same
L2 sees the group id, device id and WebSocket port, but cannot read or forge a valid
beacon without the group token, and replay is limited to the 30 s timestamp window.
Within that window a captured beacon can be replayed from another source IP and move
an already-known peer's address (a redirect/DoS), though the attacker still cannot
complete the authenticated WebSocket handshake without the token. Every 5 s each
member sends a few small broadcast/multicast datagrams; on a large noisy network this
is a small but non-zero broadcast load.

## Two-terminal smoke test

With `clipsync-relay` already running in the background, you can verify sync between the daemon and `clipsync-peer` across two terminals.

> **Warning:** This smoke test uses the real clipboard via the daemon. Run it only with non-sensitive content, or not at all if the machine is currently in use for secrets.

### Terminal A (Daemon)

Run the continuous daemon on the laptop:

```bash
clipsync --config ~/.config/clipsync/config.toml
```

### Terminal B (Peer)

The peer acts as a second device on the relay. When sharing the same configuration file, specify a distinct `--device-id` so payloads are not dropped as self-echoes.

1. Listen for payloads sent from the daemon and save received images:

```bash
clipsync-peer --config ~/.config/clipsync/config.toml --device-id phone-test listen --output-dir ./received
```

Received images land in the output directory `./received`.

2. Send text to the daemon's clipboard:

```bash
clipsync-peer --config ~/.config/clipsync/config.toml --device-id phone-test send-text "hello from the peer"
```

3. Send an image to the daemon's clipboard:

```bash
clipsync-peer --config ~/.config/clipsync/config.toml --device-id phone-test send-image ./screenshot.png
```

## Run the tests

No phone, network or real clipboard needed. Uses the system Python (no installs):

```bash
python3 -m pytest -q
```

The suite covers protocol validation/oversized payloads, clipboard command building
with a canned runner, crypto round-trip/tamper, transport delivery/partial lines,
engine echo-loop/dedup/ordering/reconnect, the loopback relay, daemon backoff/
reconnect resilience, the peer command, and the daemon/config/CLI wiring. Config/CLI
tests use fake clipboards and a canned `CommandRunner`.

The meshcore election probe is a real authenticated WebSocket connect followed by an
immediate close, bounded by the configured `probe_timeout`; it does not change another
library's logger level. A6 waits (up to 8 s) for a non-hub node to attach to the new
hub before asserting its `hub_id`, so a slow attach is no longer a flake.

### Running in a ROS-sourced shell

The suite also passes with a ROS 2 Jazzy environment sourced. ROS auto-loads the
`launch_testing` pytest plugin, which imports `launch.logging` and installs
`LaunchLogger` as the global logger class; that class sets `propagate = False` on
every logger created after it loads. As a result, pytest's `caplog` fixture sees no
records from those loggers. The suite therefore does not rely on `caplog` for
logging assertions (the forced-hub check attaches a handler directly to the
`meshcore.election` logger). If a future test needs `caplog`, either run with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` or disable both ROS plugins with
`-p no:launch_testing -p no:launch_ros` (disabling only the first breaks the second,
which needs its hooks).

## Safety

- Tests never read or write the real clipboard; `WlClipboardBackend` is implemented
  but only driven through canned `CommandRunner` fakes, never through `wl-paste`/
  `wl-copy`.
- Relay/daemon tests bind loopback-only ephemeral ports; no external network calls,
  real credentials, or system changes are used.
- `FileTransport` is a test fake; do not treat it as a production transport.

## Branches

`main` carries the current line: research, prototype, relay, and the laptop daemon.
Feature work happens on `integration/*` branches (`research`, `prototype`, `relay`,
`relay-micro`, `daemon`, `resilience`); see `docs/orchestrator/REPORT.md` for history.
`integration/resilience` (daemon resilience + `clipsync-peer`) is **not merged yet**:
the merge into `main` is left to the repo owner.
