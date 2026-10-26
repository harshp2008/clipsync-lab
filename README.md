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
