# clipsync-lab

Investigation + prototype for reliable **two-way clipboard sync (text and images)**
between an Ubuntu GNOME **Wayland** laptop and an **Android** phone.

Status: research complete; laptop-side app complete (resilient daemon, relay role,
config, peer tool, 300 tests) and the packaged thing has been run as real daemon
processes outside pytest (relay + two members on loopback). The phone side and any
TV box are **not** implemented; deployment for the laptop is documented below. Items
needing an account, the phone, or system changes are in
[`docs/orchestrator/NEEDS_HUMAN.md`](docs/orchestrator/NEEDS_HUMAN.md).

## What is here

| Path | What |
|---|---|
| [`docs/research/clipboard-sync.md`](docs/research/clipboard-sync.md) | Final synthesis: why sync breaks, 10-option comparison, recommendation + fallback, privacy note. |
| [`docs/research/agy-options-landscape.md`](docs/research/agy-options-landscape.md) | Raw agy survey (kept for provenance; reviewer corrections at the top). |
| [`docs/design/interfaces.md`](docs/design/interfaces.md) | Frozen module contracts that let separately-owned chunks fit together. |
| `clipsync/protocol.py` | Wire format, strict validation of untrusted frames, content hashing. |
| `clipsync/clipboard.py` | Backend interface, `FakeClipboard`, `WlClipboardBackend` (the default, never executed in tests), and the opt-in `FileClipboardBackend` (headless/test only). |
| `clipsync/transport.py` | `InMemoryTransport` + `FileTransport` fakes carrying opaque byte frames. |
| `clipsync/crypto.py` | Optional Fernet E2E codec + scrypt key derivation. |
| `clipsync/engine.py` | Two-way loop: echo prevention, dedup, ordering, reconnect/outbox. |
| `clipsync/relay_server.py` / `relay_transport.py` | Loopback-only WebSocket relay and its synchronous client transport. |
| `clipsync/config.py` | TOML config loader for the daemon; token/E2E key may come from files, groups/relay/peers/discovery/tuning, and the opt-in `file` clipboard. |
| `clipsync/daemon.py` | `ClipboardDaemon` loop and the `clipsync` console command/`python -m clipsync.daemon` entry point. |
| `clipsync/relay_cli.py` | `clipsync-relay` console command (legacy single-relay server). |
| `clipsync/peer.py` | `clipsync-peer` console command: second device on the legacy relay (`send-text`, `send-image`, `listen`). |
| `examples/` | Commented configs that load through the real loader: relay, laptop member, phone placeholder, and the two-groups note. |
| `scripts/gen_secrets.py` | Prints a fresh group token + Fernet key; `--write FILE` is 0600 and never overwrites. |
| `scripts/smoke_two_processes.py` | Headless real-process smoke: relay + two members, file clipboard, loopback, failover check. Stdlib only. |
| `deploy/` | systemd units (written, not installed) and install notes. |
| [`docs/android-side.md`](docs/android-side.md) | Exact phone steps (UNTESTED). |
| [`docs/orchestrator/`](docs/orchestrator/) | Plan, decisions, needs-human, final report. |

## Run it for real (laptop)

This is the supported laptop-first path. `clipsync` is the daemon; `role = "relay"`
makes the same command a relay. You do not need to install anything: every command
below also works as `python3 -m ...` from the repo root. If you prefer console
scripts, `python3 -m pip install --user -e .` provides `clipsync`, `clipsync-relay`
and `clipsync-peer`.

> **Security:** the shared group token and the E2E key must stay out of git. Keep
> them in `~/.config/clipsync/` with `chmod 600`. The relay gets each group's token
> only, never the E2E key. Tokens cross `ws://` in cleartext (TLS is not implemented),
> so bind the relay to loopback or a private tailnet/trusted LAN only.

### 1. Generate secrets

`scripts/gen_secrets.py` prints a fresh token (43 chars) and a fresh Fernet key.
Nothing is written by default:

```bash
python3 scripts/gen_secrets.py
# or write one 0600 note per group and copy the values from it:
mkdir -p ~/.config/clipsync
python3 scripts/gen_secrets.py --write ~/.config/clipsync/family-a.secrets
```

The relay config needs the **token** only; each member of that group needs the same
**token** and the **e2e_key**.

### 2. Edit the configs

```bash
mkdir -p ~/.config/clipsync
cp examples/relay.toml examples/member-laptop.toml ~/.config/clipsync/
```

Replace every `CHANGE-ME-...` value. The relay token inside `[[serve_groups]]` must
equal the member's `token`; the member's `e2e_key` is never copied to the relay.
`relay.address` is the relay's `host:port`; `[[peers]]` lists the other devices in
the same group. See `examples/two-groups-on-one-box.md` for your group vs your mom's.
The daemon refuses to start while any placeholder remains (`Configuration error:
... CHANGE-ME ... generate real secrets with scripts/gen_secrets.py ...`).

Useful fields: `group_id`, `token`/`token_file`, `e2e_key`/`e2e_key_file`, `[relay]`
(one preferred hub), `[[peers]]` (+ optional `addresses`), `discovery` (off by
default), `bind`/`port`, `state_dir`, `allow_insecure` (loopback/private tests only),
and the optional mesh tuning `heartbeat`/`missed_pongs`/`probe_timeout`/`debounce`
(defaults 10/3/2/30; lower values fail over sooner).

### 3. Start the relay

```bash
python3 -m clipsync.daemon --config ~/.config/clipsync/relay.toml
# with the installed console script: clipsync --config ...
```

It serves both `[[serve_groups]]` and never appears in either roster. Keep
`bind = "127.0.0.1"` for the laptop-only run; for a second machine use its private
tailnet/LAN address.

### 4. Start the member (laptop)

```bash
python3 -m clipsync.daemon --config ~/.config/clipsync/member-laptop.toml
```

The default clipboard backend is Wayland (`wl-paste`/`wl-copy`). Copy text as usual;
it syncs to the other members of the group. A headless machine has no real clipboard;
use the file backend described next only for tests.

### Clipboard backends on GNOME Wayland

GNOME's compositor (Mutter) does not implement the wl-clipboard data-control
protocol, so the default Wayland backend falls back to the normal data device. Every
0.2 s poll therefore opens a transient window: the taskbar flickers constantly, some
copies take seconds, and one `wl-paste` hang used to be able to kill the daemon (it
no longer can; see below). On such a session use the opt-in X11/XWayland backend
instead, which talks to the XWayland clipboard bridge and returns in milliseconds
with no window and no focus requirement:

```toml
clipboard_backend = "xclip"   # needs the xclip package and a set DISPLAY
```

`wayland` stays the default (this run deliberately did not change it). The daemon
refuses to start with a clear config error (exit code 2) if the selected backend's
programs are missing — `xclip` for `"xclip"`, `wl-paste`/`wl-copy` for `"wayland"` —
or if `DISPLAY` is unset for `"xclip"`. At run time no clipboard read/write failure
takes the daemon down: it logs a WARNING at most once per 60 s per distinct error,
backs off from `poll_interval` up to 5 s, and resumes on the first good poll. Manual
verification steps are in
[`docs/xclip-manual-checklist.md`](docs/xclip-manual-checklist.md).

### 5. Prove it without touching your clipboard

```bash
python3 scripts/smoke_two_processes.py
```

Stdlib only, about 2 s, exit code 0 = PASS. It starts three real daemon processes
from generated TOML files (one relay, two members), picks free loopback ports by
binding port 0, uses the `file` clipboard backend (a text file per member), waits for
text to arrive in the second member's file, kills the relay, waits for the members to
elect a hub, and confirms sync still works. It prints a short timeline, child logs on
failure, and terminates every process (SIGTERM then SIGKILL) even on failure.

The `file` backend is test/headless only and is never the default:

```toml
clipboard_backend = "file"   # opt-in; requires the next line
clipboard_file = "/tmp/clipsync-test.txt"
```

### 6. Optional: run it under systemd

`deploy/clipsync-relay.service` (system unit, dedicated `clipsync` user) and
`deploy/clipsync-member.service` (user unit) are provided **but not installed**.
See [`deploy/README.md`](deploy/README.md). This run did not run `systemctl`.

### Two groups, one relay

One relay process can serve your group and your mom's at the same time; each group
has its own id, token, E2E key and peers, and the relay sees only ids/tokens/ciphertext.
The walkthrough is in
[`examples/two-groups-on-one-box.md`](examples/two-groups-on-one-box.md).

## TV box (later)

**No TV box work was done in this run: its operating system is unknown.** Before
writing a unit, package or OS-specific path for it, collect:

- `uname -a` and `uname -m` (kernel + architecture);
- `python3 --version` (or whether `python3` exists at all);
- `ps -p 1 -o comm=` and `systemctl --version` (does it run systemd?);
- how it installs/updates software (distro/package manager) and whether it is always on;
- whether it can join the same private tailnet/LAN as the laptop.

Until then the relay can run on the laptop (or any Linux box with Python 3.11+ and
systemd) with `deploy/clipsync-relay.service`; no TV-specific file is shipped.

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

## Manual real-clipboard check (owner-only)

The automated smoke above never touches Wayland. To exercise the real clipboard, run
the relay and the member in two terminals with the configs from step 2, then copy
text in a scratch session:

```bash
# terminal 1: relay
python3 -m clipsync.daemon --config ~/.config/clipsync/relay.toml
# terminal 2: laptop member
python3 -m clipsync.daemon --config ~/.config/clipsync/member-laptop.toml
```

> **Warning:** this reads and writes your real clipboard. Use non-sensitive content in
> a scratch session, and do not run it while secrets may be copied. The GNOME/Wayland
> caveat (background reads may fail or return stale offers) still applies.

A second member needs its own config with a distinct `device_id` and `port`, the same
group token/key, and each other listed under `[[peers]]`. The phone placeholder is
not a working client yet.

## Quickstart (legacy single relay)

The older single-relay workflow is still supported (and still needed by
`clipsync-peer`); the current laptop/mesh path is **"Run it for real (laptop)"**
above. It uses the real clipboard.

```bash
# long random token, kept out of git
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.config/clipsync/token.txt
chmod 600 ~/.config/clipsync/token.txt

# loopback-only legacy relay
clipsync-relay --token-file ~/.config/clipsync/token.txt --port 8765

# one poll, JSON statistics (legacy relay_url config)
clipsync --config ~/.config/clipsync/config.toml --once
```

Without installing the console scripts, use `python3 -m clipsync.relay_cli` and
`python3 -m clipsync.daemon`.

## Two-terminal smoke test (legacy, real clipboard)

This older manual test uses the real clipboard and the legacy `clipsync-relay` /
`clipsync-peer` pair. Prefer the headless `scripts/smoke_two_processes.py` unless you
specifically need to exercise the peer tool.

> **Warning:** this uses the real clipboard. Use non-sensitive content in a scratch
> session, and do not run it while secrets may be copied.

1. Start the relay: `clipsync-relay --token-file ~/.config/clipsync/token.txt --port 8765`.
2. Run the daemon: `clipsync --config ~/.config/clipsync/config.toml`.
3. Listen with the peer (save received images):
   `clipsync-peer --config ~/.config/clipsync/config.toml --device-id phone-test listen --output-dir ./received`
4. Send text: `clipsync-peer --config ... --device-id phone-test send-text "hello from the peer"`.
5. Send an image: `clipsync-peer --config ... --device-id phone-test send-image ./screenshot.png`.

`--device-id` must differ from the daemon's id so payloads are not dropped as
self-echoes.

## Run the tests

No phone, network or real clipboard needed. Uses the system Python (no installs):

```bash
python3 -m pytest -q
```

The suite covers protocol validation/oversized payloads, clipboard command building
with a canned runner, crypto round-trip/tamper, transport delivery/partial lines,
engine echo-loop/dedup/ordering/reconnect, the loopback relay, daemon backoff/
reconnect resilience, the peer command, and the daemon/config/CLI wiring. Config/CLI
tests use fake clipboards and a canned `CommandRunner`; nothing in the suite drives
`wl-paste`/`wl-copy`.

`tests/acceptance/test_deploy_smoke.py` adds 7 deployment tests (D1-D7): the example
configs load and placeholders fail loudly, `gen_secrets.py` output/mode/overwrite
behaviour, the real two-process smoke script (exit 0, under 60 s, no stray children),
the systemd units parse and name a real entry point, the `file` backend is opt-in,
and the previous 300 tests still collect. The full total is **307 passed**.

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
- The deployment smoke starts real daemon processes but selects the opt-in `file`
  clipboard backend by config; the default stays Wayland, and old configs never
  select the file backend.
- Relay/daemon tests bind loopback-only ephemeral ports; no external network calls,
  real credentials, or system changes are used.
- `FileTransport` is a test fake; do not treat it as a production transport.
- The systemd units under `deploy/` are written but not installed; no `systemctl`
  command is run by this repo's tests.

## Branches

`main` carries the merged line through meshcore phase 2b (LAN transport and
discovery). Feature work happens on `integration/*` branches; the deployment work in
this run is on `integration/deploy` (examples, secrets/smoke scripts, systemd units,
docs) and is **not merged**: the merge into `main` is left to the repo owner. See
`docs/orchestrator/REPORT.md` for history.
