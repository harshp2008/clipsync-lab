# clipsync-lab

Investigation + prototype for reliable **two-way clipboard sync (text and images)**
between an Ubuntu GNOME **Wayland** laptop and an **Android** phone.

Status: research complete, laptop-side prototype complete with tests. The phone-side
steps are documented but **untested** (no phone access in this run); anything needing
an account, the phone, or system changes is in
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
| [`docs/android-side.md`](docs/android-side.md) | Exact phone steps (UNTESTED). |
| [`docs/orchestrator/`](docs/orchestrator/) | Plan, decisions, needs-human, final report. |

## Quickstart

`clipsync` is the laptop daemon that syncs clipboard events between the local clipboard backend and the relay, while `clipsync-relay` is the loopback-only relay server.

> **Security Note**: Both the shared bearer token and the optional E2E encryption key must stay out of git (e.g. stored in `~/.config/clipsync/` or secret files with restricted permissions `0600`).

### 1. Start the loopback relay

Start `clipsync-relay` on loopback (default port 8765):

```bash
# Save token in a file (ensure it stays out of git)
echo "my-secret-token" > ~/.config/clipsync/token.txt
chmod 600 ~/.config/clipsync/token.txt

# Run loopback-only relay
clipsync-relay --token-file ~/.config/clipsync/token.txt --port 8765
```

### 2. Configure the daemon

Create `~/.config/clipsync/config.toml`:

```toml
device_id = "laptop"
relay_url = "ws://127.0.0.1:8765/"
token_file = "token.txt"
poll_interval = 0.5
```

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
with a canned runner, crypto round-trip/tamper, transport delivery/partial lines, and
engine echo-loop/dedup/ordering/reconnect tests.

## Safety

- Tests never read or write the real clipboard; `WlClipboardBackend` is implemented
  but not executed.
- No network calls, no credentials, no system changes, no ports opened.
- `FileTransport` is a test fake; do not treat it as a production transport.

## Branches

`main` holds only the initial commit. Work is on `integration/research` and
`integration/prototype`; see the report for the merge command.
