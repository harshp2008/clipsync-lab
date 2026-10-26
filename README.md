# clipsync-lab

Investigation + prototype for reliable **two-way clipboard sync (text and images)**
between an Ubuntu GNOME **Wayland** laptop and an **Android** phone.

Status: research complete; laptop-side app complete (daemon, relay command, config,
tests). The phone-side steps are documented but **untested** (no phone access in this
run); anything needing an account, the phone, or system changes is in
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
| [`docs/android-side.md`](docs/android-side.md) | Exact phone steps (UNTESTED). |
| [`docs/orchestrator/`](docs/orchestrator/) | Plan, decisions, needs-human, final report. |

## Quickstart

`clipsync` is the laptop daemon that wires a clipboard backend to the relay, while
`clipsync-relay` is the loopback-only relay server. From the repo root, install the
console commands once (the runtime dependency is already in `pyproject.toml`):

```bash
python3 -m pip install --user -e .
```

Without installing, the same commands work as `python3 -m clipsync.daemon ...` and
`python3 -m clipsync.relay_cli ...`.

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

## Run the tests

No phone, network or real clipboard needed. Uses the system Python (no installs):

```bash
python3 -m pytest -q
```

The suite covers protocol validation/oversized payloads, clipboard command building
with a canned runner, crypto round-trip/tamper, transport delivery/partial lines,
engine echo-loop/dedup/ordering/reconnect, the loopback relay, and the daemon/config/
CLI wiring. Config/CLI tests use fake clipboards and a canned `CommandRunner`.

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
`relay-micro`, `daemon`); see `docs/orchestrator/REPORT.md` for history.
