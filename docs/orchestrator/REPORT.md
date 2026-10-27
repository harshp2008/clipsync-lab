# Report — clipsync-lab coordinated run

Run window: **2026-10-26 20:31 → 21:20 IST** (deadline 02:30; finished early).
Repo: `/home/harsh/Documents/GITHUB PROJECTS/clipsync-lab`. `main` untouched.
Result: research + laptop-side prototype + tests + orchestrator docs, all on
`integration/*` branches.

## 1. Deliverables

| Deliverable | Where | State |
|---|---|---|
| Research synthesis (why it breaks, 11 options, comparison, recommendation, privacy) | `docs/research/clipboard-sync.md` | complete |
| Raw agy option survey (provenance + reviewer corrections banner) | `docs/research/agy-options-landscape.md` | kept as draft |
| Frozen module contracts | `docs/design/interfaces.md` | complete |
| Prototype (protocol, clipboard, transport, crypto, engine) | `clipsync/` | implemented |
| Tests (protocol, clipboard, crypto, transport, engine) | `tests/` | **104 passed** |
| Android guide, three paths | `docs/android-side.md` | complete, **UNTESTED** |
| Orchestrator docs | `docs/orchestrator/{PLAN,DECISIONS,NEEDS_HUMAN,REPORT}.md` | complete |
| Project overview / run instructions | `README.md` | complete |

## 2. Assignment table (final owner)

| Chunk | Final owner | Why | Outcome |
|---|---|---|---|
| R0 Probe: Android background clipboard + agy research ability | **agy** | Cheapest read-only job; priced the rest | done, 0.27 pts |
| R1 Android restrictions by version | **agy draft + me** | Self-contained research, but claims needed audit | probe draft corrected in synthesis |
| R2 GNOME Wayland access + image MIME/size limits | **me** | Needs judgement; small; feeds the design | written + verified |
| R3 Options landscape (10 families) | **agy** | Source-heavy breadth, self-contained | done, 0.33 pts; 5 claims corrected |
| R4 Comparison table + recommendation + privacy | **me** | Cross-cutting synthesis; must match prototype | written |
| P1 Interfaces / message format design | **me** | Contract that makes separately-owned modules fit | frozen before code jobs |
| P2 `protocol.py` + unit tests | **agy** | Best-specified chunk (exact hash/wire rules + tests) | accepted unchanged, 24 tests |
| P3 `clipboard.py` (fake + unexecuted Wayland) | **me** | Safety-critical: must never touch the real clipboard | 14 tests |
| P4 `transport.py` + tests | **agy** | Self-contained byte-pipe semantics | accepted unchanged, 32 tests |
| P5 `engine.py` (loop/dedup/ordering/reconnect) | **me** | Most subtle logic in the project | 18 tests |
| P6 `crypto.py` optional E2E | **me** | Security-sensitive and short | 8 tests |
| P7 Integration/E2E tests | **me** | Cross-cutting; module tests travelled with P2/P4 | included |
| P8 Android-side guide | **agy + my fixes** | Self-contained docs | 2 invented facts fixed |
| P9 Report, merges, final verification | **me** | Requires the whole conversation | this file |

Every owner change is also in `DECISIONS.md` with a one-line reasoning (D7–D12).

## 3. agy jobs

All jobs used `max_minutes=30`, no `model` parameter, one at a time, `agy_usage` checked
immediately before each start, every diff reviewed with `agy_review` and read, and
every branch merged only into an `integration/*` branch then cleaned with
`force` left at false.

| # | Job id | Title | State | Wall | Commits | Verify | Audit | Weekly cost | 5h cost |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `ed2ece76-537d-4c03-82dd-cc2895eeb29f` | Probe: Android clipboard background limits | done | 6.9 min | 1 | pass | pass | 0.27 | 0.94 |
| 2 | `3f818eca-765c-4952-b751-08db9cf32c0d` | Research: 10-family options landscape | done | 9.3 min | 1 | pass | pass | 0.33 | 1.17 |
| 3 | `1929b570-1807-4966-b23d-8f92d1c9e2fe` | Implement `protocol.py` + tests | done | 8.6 min | 2 | pass | pass | 0.49 | 1.73 |
| 4 | `d0121297-e7d5-4d79-845a-e81d173573a9` | Implement `transport.py` + tests | done | 8.1 min | 2 | pass | pass | 0.38 | 1.33 |
| 5 | `3bf97154-f6f8-42bf-a97e-4d734a6676fa` | Android-side setup guide | done | 6.1 min | 1 | pass | pass | 0.25 | 0.88 |
| | | **Total** | **5/7 jobs** | **~39 min** | **7** | **all pass** | **all pass** | **1.72** | **6.05** |

One benign warning: job 3 reported `plan_incomplete` while its own plan was 6/6 done
and the wrapper's verify ran green. No action needed.
No job hit `quota_exhausted`, `needs_decision`, `auth_required`, `failed`, a security
audit finding, or a permission denial.

## 4. Quota, the floor, and fallback

- Start: **24.42% weekly / 99.19% 5h** remaining. Coherent end: **22.69% / 93.14%**.
- Measured spend: **1.73 weekly points, 6.05 five-hour points** across five jobs.
- The brief's floor (**20% weekly / 15% 5h**) was **never reached**, so there was no
  forced takeover after the floor, no `quota_exhausted` state, and no need to swap
  remaining chunks off agy. Had it been reached, the remaining chunks (android doc,
  report, and any code review) were already marked mine in `PLAN.md`; only job 5 would
  have moved.
- Fallback *was* exercised in a smaller way: R1 was downgraded from a second research
  job to "probe draft + my verification" after the probe showed agy's research was
  cheap but over-confident. That saved a job and is recorded in D8.
- There is budget for ~2 more jobs if a follow-up session wants them (7-job cap, ~2.7
  weekly points above the floor), but the plan is done and more agy work would add no
  deliverable.

## 5. How well the delegation worked

**What agy did well**

- The probe answered the two questions it was built for: `search_web` works, a job
  costs ~0.3 weekly points and ~7–9 minutes, and its worktree/verify/audit plumbing is
  reliable.
- **Protocol** and **transport** were specified tightly enough that both were accepted
  with **zero code changes**: exact hash formula, strict `from_wire` validation,
  base64/strict JSON, file offsets, partial trailing lines, path-safe ids, fsync.
  Their 56 tests were green on the first wrapper run, and my 18 engine tests passed
  against them unchanged.
- The Android guide had the right structure (three paths, banners, placeholders,
  troubleshooting, privacy, "what is still missing") and kept every secret as a
  placeholder.

**What agy did badly**

- **Research over-confidence.** It marked claims VERIFIED against pages that do not say
  them: Android 10 background *writes*, "Android 14/15/16 continuity", ntfy
  password-based AES-GCM E2E, and GSConnect "Mutter D-Bus" internals. It also got the
  KDE Connect port range wrong ("port 1716" vs the documented 1714–1764).
- **Invented operational facts in the Android guide**: a `CC_ADMIN_PASSWORD` Docker
  env var that does not exist in the ClipCascade README, and the package id
  `com.sathvikrao.clipcascade` instead of `com.clipcascade` in the ADB commands.
- Its own reports repeated the same over-claims, so the reports were not a reliable
  signal — `agy_review` + reading the diff were.

**What I had to fix**

- Added a reviewer-corrections banner to the raw options doc instead of rewriting it
  silently, then re-verified ClipCascade and SyncClipboard from their READMEs and wrote
  the comparison/recommendation myself.
- Corrected the KDE Connect facts from its UserBase page (text-only, manual
  "Send clipboard" on Android 10+, QS tile on 14+, READ_LOGS/Magisk workaround).
- Fixed the two invented Android-guide facts and marked the GSConnect Wayland claim
  `[ASSUMED]`.
- Reviewed every diff before merge; no agy code was merged unreviewed.

**Takeaway:** agy is excellent at bounded implementation against a frozen contract and
acceptable at breadth research as a first draft; it is not trustworthy as an
authority. The design-first approach (interfaces frozen before any code job) is what
made the code hand-offs clean.

## 6. Verification

- Final suite: `python3 -m pytest -q` → **104 passed**.
- Per-job verify commands all passed (`tests/test_protocol.py` 24,
  `tests/test_transport.py` 32, research/doc gates 3).
- All five audits passed; no denied commands/tools, no modified host paths.
- Tests never read/write the real clipboard: `WlClipboardBackend` is driven only by a
  canned `CommandRunner`; the engine uses `FakeClipboard` + `InMemoryTransport`.
- No network calls besides research fetches; no packages installed; no services,
  ports, settings, cron/systemd, accounts, keys, or cloud projects created.
- `main` still at the initial commit; no `.agy/` files are tracked; agy worktrees and
  job branches were removed via `agy_cleanup`.

## 7. Untested / not done

- **All phone steps** in `docs/android-side.md` (Paths A, B and C) — no device in this
  run.
- `WlClipboardBackend.read()/write()` were **not executed**; only its command builders
  and MIME choice are tested. GNOME's focus behaviour was not measured on the machine.
- The **WebSocket transport adapter** and the **Android client** for Path A are design
  only; `docs/android-side.md` says so explicitly.
- Background `setPrimaryClip` on stock Android 15/16 and OEM battery behaviour are
  **ASSUMED**.
- `FileTransport` is a test fake, not concurrency-hardened and not a production
  transport.
- Long-run/battery/network-switch behaviour of any client is unmeasured.

## 8. Risks

1. **Expectation risk:** Android 10+ makes fully automatic phone→laptop clipboard reads
   impossible for a normal background app. Every honest solution needs a tap, an IME,
   root, or ADB. A product that promises otherwise will disappoint.
2. **Echo prevention depends on byte-stable clipboard content.** A backend that
   re-encodes images breaks the content hash and can reintroduce loops; the engine
   documents this. Keep image bytes verbatim.
3. **Image size mismatch:** the protocol allows 8 MiB, Android's Binder clipboard is
   ~1 MB. Without downscaling or a FileProvider URI, large images will fail on the
   phone even though sync "succeeded".
4. **Poisoned file transport:** a malformed JSONL line raises `TransportError` without
   advancing the offset, so a wedged file blocks `recv` until the file is fixed. Fine
   for a test fake; not production behaviour.
5. **Outbox cap:** 64 frames; a long outage drops the oldest updates (counted in
   `state_summary()["overflow_dropped"]`).
6. **Restart sequence:** production should let `seq_base` default to the time-based
   value; forcing it to 0 on restart makes the peer drop new frames as stale.
7. **Research gaps:** Mutter/`wlr-data-control` support and Android 14–16 clipboard
   changes are ASSUMED; verify on the target machine/phone.
8. **Secret handling:** E2E is only as good as key distribution. Salt/passphrase must
   never be committed, and a lost phone means rotating the key.

## 9. Commands

Run the tests (system Python; no installs needed):

```bash
cd "/home/harsh/Documents/GITHUB PROJECTS/clipsync-lab"
python3 -m pytest -q
# expected: 104 passed
```

Inspect the work:

```bash
git log --oneline --graph --all
git branch -a
# main, integration/research, integration/prototype
```

Merge when ready (the brief reserves `main` for a human):

```bash
git checkout main
git merge --no-ff integration/prototype
```

`integration/prototype` is a superset of `integration/research` (the research branch
also contains an early WIP commit of the code; the prototype branch is the clean final
history to merge). All agy job branches were already merged into the integration
branches and deleted.

## 10. Recommendation in one paragraph

Own the relay: put the laptop and phone on **Tailscale/WireGuard**, run a small
self-hosted **WebSocket relay** on the laptop, and use the `clipsync` message format
(`kind=text|image`, MIME, content hash, per-origin seq, bounded outbox) with the
optional **Fernet E2E** layer. Phone→laptop needs a **"Send clipboard" notification
action / QS tile** on Android 10+ (the honest limit), and large images should go over
the wire but onto the phone clipboard as a **downscaled image or FileProvider URI**.
If you would rather not write the Android client, use **ClipCascade** as the turnkey
fallback; if you only need text, **KDE Connect/GSConnect** is the zero-server option.

---

# Relay micro run

**Window:** 2026-10-26 23:40 → 2026-10-27 00:17 IST (deadline 02:30).
**Branch:** `integration/relay-micro`, cut from `f003260` ("test: spec-first acceptance
tests + contract for network relay"). `main`, `integration/relay` and
`integration/prototype` were never committed to or merged into.
**Result:** the relay is implemented and the gate passes — **13/13 acceptance tests,
144/144 full suite**, frozen acceptance tests byte-identical to `f003260`.

This was the micromanagement comparison run: the orchestrator split the work into four
small chunks, one fully specified agy job per chunk, sequential, with review + merge +
test + cleanup after each. Build was contract-only; no other relay implementation was
read or referenced.

## Chunk plan

| # | Chunk | Owner | Outcome |
|---|---|---|---|
| M1 | `clipsync/relay_server.py` + `clipsync/relay_transport.py` | agy, reviewed by me | accepted with two warning-only edits by me |
| M2 | `tests/test_relay_server.py` (11 unit tests) | agy | accepted unchanged |
| M3 | `tests/test_relay_transport.py` (13 unit tests) | agy | accepted unchanged |
| M4 | `tests/test_relay_integration.py` (3 engine-over-relay E2E tests) + hardening | agy | accepted after one plan-bookkeeping resume; merged unchanged |

M1 deliberately combines the two modules: `tests/acceptance/test_relay_acceptance.py`
imports both at module import time, so a server-only job would fail the mandated
full-suite `verify` for reasons unrelated to its own quality.

## agy jobs (one at a time, `max_minutes=30`, no `model`)

| # | Job id | State | Wall | Verify | Audit | Weekly cost | 5h cost | What I fixed |
|---|---|---|---|---|---|---|---|---|
| M1 | `8bf328ee-6f23-4141-8164-b2db1c403410` | done | ~10.4 min | pass (117 tests) | pass | 0.46 | 1.61 | 2 warning edits: coroutine `process_request`; suppress expected `CancelledError` on transport shutdown |
| M2 | `115f538d-1fec-4a7f-b492-99e39f52542b` | done | ~7.5 min | pass (128 tests, attempt 1) | pass | 0.34 | 1.18 | none — accepted unchanged |
| M3 | `a1b9da5a-8e7a-4549-ba8b-b84515c4707a` | done | ~5.8 min | pass (141 tests, attempt 2) | pass | 0.27 | 0.96 | none — accepted unchanged; benign `plan_incomplete` warning |
| M4 | `d7b216f8-9c60-4f74-9e7a-87c0fb7ad981` | needs_review → done | ~11.1 min | pass (144 tests, attempt 3) | pass | 0.53 | 1.85 | none in code; one `agy_resume` answering that the wrapper had already run and passed verify |
| | **Total** | **4/4 done** | **~35 min** | **all pass** | **all pass** | **1.60** | **5.60** | |

Quota: start **22.12% weekly / 91.12% 5h**, end **20.52% / 85.52%** — above the
20%/15% floor, so no chunk was handed over and no 5th job was needed. Four of the
six allowed jobs were used. Every `agy_start` was preceded by `agy_usage`; every job
was reviewed with `agy_review`, its diff read, merged only into
`integration/relay-micro`, retested, then cleaned with `agy_cleanup`
(`force` never set, no `model` parameter, job branches deleted).

M4's `needs_review` was purely checklist bookkeeping: agy refuses to run tests itself
(wrapper-delegated), so two "run pytest" checklist items stayed open even though the
wrapper's verify had already passed 144 tests. One resume with that information closed
the plan items; no code changed after review.

## What the orchestrator did directly

- Split the work, wrote all four job prompts/checklists, checked quota before each
  start, reviewed every diff, merged, and ran the full suite after each merge.
- Post-M1: made `process_request` a coroutine (removes 25 `DeprecationWarning`s) and
  suppressed the expected `asyncio.CancelledError` when `close()` cancels the
  transport's connection task (removes 10 `PytestUnhandledThreadExceptionWarning`s).
  Two shutdown-noise edits, no behaviour change; suite went from warnings-visible to
  warning-free.
- Post-M2/M3/M4: accepted unchanged. M4's hardening (constant-time `hmac.compare_digest`
  bearer comparison, `RLock` lifecycle lock) and the three E2E tests were reviewed
  line-by-line and merged as-is.
- Wrote this report, `PLAN.md`/`DECISIONS.md`/`NEEDS_HUMAN.md` updates, and committed
  all orchestrator docs on the integration branch.

## Gate results (commands run by me, on `integration/relay-micro`)

```bash
cd "/home/harsh/Documents/GITHUB PROJECTS/clipsync-lab"
python3 -c "import websockets; print(websockets.__version__)"   # 10.4
python3 -m pytest -q tests/acceptance                            # 13 passed
for i in 1 2 3 4 5; do python3 -m pytest -q tests/acceptance; done  # 13 passed x5
python3 -m pytest -q                                             # 144 passed
git diff f003260 -- tests/acceptance | wc -l                      # 0 (empty)
git branch --show-current                                         # integration/relay-micro
```

Acceptance ran green five times consecutively to check the threaded/reconnect paths
for flakiness. The suite is warning-free. `agy_list` reported zero active jobs and no
drifted worktrees after cleanup.

## How to run the tests and merge (for the human)

```bash
cd "/home/harsh/Documents/GITHUB PROJECTS/clipsync-lab"

# 1. Verify the relay branch
git checkout integration/relay-micro
python3 -m pytest -q tests/acceptance    # expected: 13 passed
python3 -m pytest -q                     # expected: 144 passed
git diff f003260 -- tests/acceptance     # expected: empty

# 2. Merge when ready (main is intentionally left to you)
git checkout main
git merge --no-ff integration/relay-micro
python3 -m pytest -q                     # expected: 144 passed
```

Relay usage sketch (loopback only, token out of band):

```python
from clipsync.relay_server import RelayServer
from clipsync.relay_transport import RelayTransport, relay_url
from clipsync.engine import SyncEngine

with RelayServer(token="change-me") as server:
    t = RelayTransport(relay_url(server.port), "change-me")
    t.connect()
    engine = SyncEngine("laptop", clipboard, t)  # clipboard = your backend
    engine.poll_once()
    t.close()
```

The relay is a byte pipe: frames stay opaque, so an optional `FernetCodec` gives E2E
privacy without the server holding a key. The phone-side client and a real `wss://`
deployment are still human/next-run work (`NEEDS_HUMAN.md`).

---

# Daemon run

**Window:** 2026-10-27 02:37 → ~02:56 IST (deadline 06:00; finished early).
**Branch:** `integration/daemon`, cut from `main` at `0a1f7f4`; merged into `main`
with `--no-ff` after the gate because the request said "on main, add".
**Workflow:** GATED — contract + 34 acceptance tests committed at `3df64e0` before the
single implementation job. **Result:** the laptop app is implemented and green:
**34/34 acceptance, 186/186 full suite**, job branch acceptance diff empty, audit
passed, worktree cleaned up.

## Deliverables

| Deliverable | Where | State |
|---|---|---|
| Config from files (relay URL, token, E2E key; TOML + secret-file indirection) | `clipsync/config.py` | complete |
| Daemon loop wiring clipboard backend ↔ relay transport | `clipsync/daemon.py` (`ClipboardDaemon`) | complete |
| `clipsync` console command (`--config`, `--once`, `--log-level`, `--version`, graceful SIGINT/SIGTERM) | `clipsync/daemon.py`, `[project.scripts]` | complete |
| `clipsync-relay` command (`--token`/`--token-file`, `--port`, `--max-frame-bytes`) | `clipsync/relay_cli.py`, `[project.scripts]` | complete |
| Unit tests for config/daemon/relay CLI | `tests/test_config.py`, `tests/test_daemon.py`, `tests/test_relay_cli.py` | complete |
| Frozen acceptance suite + daemon contract | `tests/acceptance/test_daemon_acceptance.py`, `docs/design/interfaces.md` | complete |
| README quickstart | `README.md` § Quickstart | complete |
| Orchestrator docs | `docs/orchestrator/{PLAN,DECISIONS,NEEDS_HUMAN,REPORT}.md` | complete |

## Job

| Job id | State | Wall | Commits | Verify | Audit | Weekly cost | 5h cost |
|---|---|---|---|---|---|---|---|
| `54f33781-39b2-426b-b286-38f17844fd87` | done | ~11.9 min | 7 | 186 passed (attempt 2) | passed | 0.78 | 2.72 |

Quota before start **20.5153% weekly / 100% 5h**, checked immediately before
`agy_start`; the run-specific floor was **20.4% weekly**. After the job/cleanup:
**19.739% / 97.28%** — below the floor, so **no second/fix job** was started; the two
small corrections below were mine, recorded in `DECISIONS.md` D22–D23.

## Gate evidence (run by me)

```bash
git diff 3df64e0 -- tests/acceptance          # 0 lines on the job branch
python3 -m pytest -q tests/acceptance          # 34 passed (4x consecutively)
python3 -m pytest -q                           # 186 passed
git diff 3df64e0 -- clipsync/clipboard.py      # 0 lines after my revert
```

The first wrapper verify failed on one acceptance test because **my fixture was
wrong**: it advertised `text/plain` **and** `image/png`, while `choose_mime`
deliberately prefers images, so the backend read the image type. The job "fixed" this
by reordering `choose_mime` to prefer text — a contract regression I rejected. I
reverted `clipsync/clipboard.py` to its base version and changed the fixture to
advertise text only. The image-preference path remains covered by the existing
`test_choose_mime` cases. A job-added `WlClipboardBackend.choose_mime` alias was
dropped with the revert. This is the only deviation from a clean accept.

## What the implementation does

- `load_config` is strict: only the documented TOML keys, exactly one of
  `token`/`token_file` and at most one of `e2e_key`/`e2e_key_file`, relative secret
  paths resolved against the config directory, `ws`/`wss` URLs, positive finite
  timings, and Fernet-key validation. Errors never echo secret values.
- `ClipboardDaemon` owns a `SyncEngine` and only touches the injected backend. `run`
  checks `stop_event` before each poll, sleeps only between polls, and returns the
  poll count. Defaults build `WlClipboardBackend` and `RelayTransport`; tests inject
  fakes.
- `clipsync --once` connects, polls once, prints a compact JSON `SyncStats` line, and
  exits; continuous mode runs until SIGINT/SIGTERM. Exit codes: 0 success, 1 connect
  failure, 2 config/CLI error.
- `clipsync-relay` runs the loopback-only server, prints the bound URL once, supports
  an ephemeral port (`--port 0`), and stops cleanly. Signal handlers are skipped when
  a `stop_event` is injected, so tests can run it off the main thread.
- Tests use `FakeClipboard`, `InMemoryTransport`, loopback ephemeral relays and a
  canned `CommandRunner`; **no test executes `wl-paste`/`wl-copy` or reads/writes the
  real clipboard**.

## How to run (human)

```bash
cd "/home/harsh/Documents/GITHUB PROJECTS/clipsync-lab"
python3 -m pytest -q                         # 186 passed
python3 -m pytest -q tests/acceptance        # 34 passed

# Optional: install the two console commands, then follow README Quickstart.
python3 -m pip install --user -e .

clipsync-relay --token-file ~/.config/clipsync/token.txt --port 8765
clipsync --config ~/.config/clipsync/config.toml --once   # JSON stats
clipsync --config ~/.config/clipsync/config.toml          # continuous
```

`main` now contains the app; no merge is pending. This run did not push, force, install
packages, change system settings, or start the daemon against the real clipboard.

## Not done / risks

- Real `WlClipboardBackend` reads/writes and the real daemon loop on a live GNOME
  session remain unexecuted (by design; see NEEDS_HUMAN H6/H10).
- The Android client and a real `wss://` deployment are still missing (H3).
- The relay is loopback-only by design; remote use needs Tailscale/reverse proxy (H2).
- Config secrets are protected only by file permissions; the README says `chmod 600`
  and keep them out of git. There is no keyring integration.
- The daemon's continuous loop has no metrics endpoint; it logs to stderr only.

---

# Resilience + peer run

**Window:** 2026-10-27 10:50 → ~11:10 IST (deadline 14:00; finished early).
**Branch:** `integration/resilience`, cut from `main` at **`73b58b3`**. `main` was
deliberately **not** merged into or committed to; per the request the merge is left to
the repo owner (NEEDS_HUMAN H11).
**Workflow:** GATED — contract + 32 acceptance tests committed at **`99ef7bb`** before
the single implementation job. **Result:** daemon resilience and `clipsync-peer` are
implemented and green: **66/66 acceptance** (three consecutive runs), **230/230 full
suite**, frozen acceptance diff **0 lines**, audit passed, worktree cleaned up.

## Deliverables

| Deliverable | Where | State |
|---|---|---|
| Capped exponential backoff + jitter (`BackoffPolicy`) and interruptible waits (`wait_or_stop`) | `clipsync/daemon.py` | complete |
| Resilient daemon loop: outages and transport errors do not crash, keep polling and reconnect; startup relay-down retries until stopped | `clipsync/daemon.py` (`ClipboardDaemon.run`, `connect_resilient`, `main(stop_event=...)`) | complete |
| `clipsync-peer` console command (`send-text`, `send-image`, `listen --output-dir`; same config/token/E2E key, protocol and codec as the daemon) | `clipsync/peer.py`, `pyproject.toml` | complete |
| Version strings from `clipsync.__version__` in all three CLIs | `daemon.py`, `relay_cli.py`, `peer.py` | complete |
| README two-terminal smoke test | `README.md` § "Two-terminal smoke test" | complete |
| Frozen acceptance suites + contract | `tests/acceptance/test_{resilience,peer}_acceptance.py`, `docs/design/interfaces.md` | complete |
| Unit tests (backoff, peer) | `tests/test_{resilience,peer}.py` | complete |
| Orchestrator docs | `docs/orchestrator/{PLAN,DECISIONS,NEEDS_HUMAN,REPORT}.md` | complete |

## Job

| Job id | State | Wall | Commits | Verify | Audit | Weekly cost | 5h cost |
|---|---|---|---|---|---|---|---|
| `d5c77c7b-4c60-4e4d-bbbd-f2b5b9845722` | done | ~11.2 min | 6 | 230 passed (attempt 1) | passed | 0.73 | 2.55 |

Quota: start **19.7393% weekly / 100% 5h**; after cleanup **19.01% / 97.45%**. The
run-specific rule was “do not start below **12% weekly**”; it was never reached, so no
handover and no fix job were needed. `agy_usage` was read immediately before the start;
`agy_cleanup` ran with `force` false and removed both worktree and job branch.

## Gate evidence (run by me, on `integration/resilience`)

```bash
git diff 99ef7bb -- tests/acceptance          # 0 lines
git diff 99ef7bb -- docs/design/interfaces.md # 0 lines
git diff --name-status 99ef7bb agy/resilience-clipsync-peer-implementation-8d7766f2
                                              # only the 7 intended files
python3 -m pytest -q tests/acceptance          # 66 passed (3 consecutive runs)
python3 -m pytest -q                           # 230 passed
git diff 99ef7bb HEAD -- tests/acceptance      # 0 lines
```

The 32 new acceptance tests (18 resilience, 14 peer) were written before the job and
never edited by it. I read the full diff before merging; no out-of-scope changes, no
clipboard access in the peer, no secret/payload logging. Accepted unchanged; merged
with `--no-ff` into `integration/resilience`.

## What the implementation does

- `BackoffPolicy.delay` = `min(max_delay, base * 2**attempt)` scaled by
  `1 - jitter + 2*jitter*r` (defaults 0.2 s / 30 s / 0.5) with strict validation.
  `wait_or_stop` slices waits at 0.05 s when a `stop_event` exists, so SIGINT/SIGTERM
  interrupt even a 30 s backoff; without an event the injected `sleep` is called once,
  which keeps the old exact-sleep tests valid.
- `ClipboardDaemon.run` treats a falsy `transport.connected` at the top of an
  iteration as an outage: one WARNING through `clipsync.daemon`, wait
  `backoff.delay(consecutive_failures)`, reconnect next iteration; a transport error
  escaping `poll_once()` or a disconnect detected after the poll does the same; any
  other exception still propagates. Healthy iterations are unchanged (one poll, one
  `sleep(poll_interval)`), and failed iterations are counted as iterations.
- Continuous `clipsync` no longer exits when the relay is down at startup: it logs
  warnings and retries until SIGINT/SIGTERM, then returns 0. `--once` deliberately
  keeps the old fail-fast behaviour (`Transport connection error: ...`, rc 1).
- `clipsync-peer` reuses `load_config`, `build_transport`, `build_codec`,
  `BackoffPolicy` and `wait_or_stop`; it never imports the clipboard. `listen` creates
  its output directory, retries a down relay, skips its own origin and undecryptable/
  invalid frames with warnings, prints canonical JSON lines (text fields or an image
  `path`/`bytes`), and writes images verbatim to `<output-dir>/<msg_id>.<ext>`.
- `clipsync --version`, `clipsync-relay --version` and `clipsync-peer --version` all
  resolve `clipsync.__version__` at call time (no hardcoded duplicate).

## Safety

- No test or command in this run read or wrote the real clipboard. The peer does not
  import `clipboard` at all; daemon tests use `FakeClipboard`/in-memory fakes; the
  subprocess SIGTERM test runs against a closed loopback port so the daemon never
  polls a clipboard; every relay is loopback-only on an ephemeral port.
- No packages installed, no external network, no system changes, no secrets created,
  no push/force, and `main` was not touched.

## Verification commands (human)

```bash
cd "/home/harsh/Documents/GITHUB PROJECTS/clipsync-lab"
git checkout integration/resilience
python3 -m pytest -q tests/acceptance    # expected: 66 passed
python3 -m pytest -q                     # expected: 230 passed
git diff 99ef7bb -- tests/acceptance     # expected: empty
```

Merge when ready (left to the repo owner on purpose):

```bash
git checkout main
git merge --no-ff integration/resilience
python3 -m pytest -q                     # expected: 230 passed
```

## Not done / risks

- The merge into `main` is **not** done (H11), and the two-terminal smoke test has not
  been run against the real desktop clipboard (H12).
- The resilience suite uses fake transports for deterministic backoff assertions and
  one real loopback “relay starts later” test; long real outages, network switching,
  laptop suspend/resume and `wss://` reconnects are still unmeasured.
- Jitter is randomness-based in production (deterministic only via the injected `rand`
  in tests); backoff can reach the 30 s cap, so recovery after a long outage may take
  up to ~30 s between attempts (bounded and intentional).
- The peer `listen` has no dedup: replayed or duplicate frames are printed/saved again
  (the file name is the message id, so duplicates overwrite the same image file).
- `clipsync-peer` is a CLI tool, not a long-running phone substitute; the Android
  client and remote relay deployment remain H3/next-run work.
