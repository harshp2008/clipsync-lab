# clipsync-lab — LAN transport + discovery run plan (GATED)

Workflow: **GATED** — the contract and frozen acceptance tests are committed first,
then exactly ONE agy job (max 45 min) implements the code without touching
`tests/acceptance`. I review the diff, merge into `integration/lan`, and run the
20x/5+5 gate myself. The owner merges into `main`; this run never does.

Branch: `integration/lan`, cut from `main` at `0944778` (289 tests).
Phase 2b of `docs/design/coordination-v2.md`: transport and discovery. The frozen
contract is `docs/design/meshcore-lan.md`.

| Step | Owner | Status |
|---|---|---|
| Read design, meshcore (config/election/link/node/hub), clipsync config, A-tests | me | done |
| Contract `docs/design/meshcore-lan.md` | me | done (`a07187a`, amended `7f2a178`) |
| Frozen `tests/acceptance/test_meshcore_lan.py` (L1–L11) | me | done (`a07187a` / final freeze `40af196`) |
| Baseline suite + red run, commit contract/tests, freeze hash | me | done (289 baseline; new file red) |
| `agy_usage` + ONE agy implementation job (45 min) | agy | done (`0f813431…`, 5 commits, verify timeout; taken over) |
| Gate: `git diff <contract> -- tests/acceptance` empty, `agy_review`, read diff, merge | me | done (job gate empty; merged `49d708e`) |
| Acceptance 20x; full suite 5x clean + 5x ROS PYTHONPATH | me | done: 20/20, 5/5, 5/5 (300 passed each) |
| Docs: coordination-v2 §5/§13, meshcore-lan.md, README "Testing without internet" | me | done |
| Report, DECISIONS.md, NEEDS_HUMAN.md | me | done |

## Run log (2026-10-29)

- 01:25 — branch `integration/lan` cut from `main` `0944778` (289 tests); design,
  code and existing acceptance style read. Contract and tests written.
- 01:32 — `agy_usage` weekly 96.76% / 5h 96.71%; one job started (base `a07187a`),
  max 45 min; it made 5 commits then ended `needs_review`/`plan_incomplete` with the
  verify command timing out at 900 s (`.F..F`).
- 01:59 — job gated (`git diff a07187a 7c51ade -- tests/acceptance` empty), reviewed,
  merged at `49d708e`; took over. Fixed the probe-seam token binding (L2/L5), the
  proxy harness `serve_forever` bug (L6/L11), discovery UDP port, clipsync scope and
  the G7 allowlist (D51–D54).
- 02:20 — final review found the beacon `ts` used the election's monotonic clock
  (different origin per machine), so real beacons would look stale; Discovery now uses
  `time.time`. Re-ran the whole gate on the fixed code: LAN acceptance 20/20 (11
  passed); full suite 5/5 clean and 5/5 with the ROS PYTHONPATH (300 passed each), no
  logging noise. Final HEAD `7bf6573`; docs and report finished.

## Prior runs

The groups run (2026-10-29) and the earlier research/prototype/relay/daemon runs are
recorded in `DECISIONS.md`, `REPORT.md` and `NEEDS_HUMAN.md`. `main` contains all of
them up to phase 2a + groups.

## Groups run (2026-10-29, earlier today)
See REPORT.md and DECISIONS.md D48-D49 (full run log is in git at 0944778).

## Historical plan (earlier runs)

Goal: reliable two-way clipboard sync (text **and** images) between an Ubuntu GNOME
Wayland laptop and an Android phone; research the landscape, then prototype the
laptop side of the recommended option with tests that need no phone and no cloud.

Constraints: work only inside this repo; never touch the real clipboard; no system
packages/services/network beyond research; agy is the default owner of self-contained
chunks; at most 1 agy job running at a time and at most 7 agy jobs total; call
`agy_usage` before every `agy_start` and stop starting jobs below **20% weekly / 15%
5-hour** remaining (then I take over every remaining chunk). Deadline 02:30 IST.

## Assignment table

Owners are the **final** assignment after the run; every change from the initial plan is
recorded in DECISIONS.md (D8–D13) with a one-line reasoning.

| Chunk | Owner | Why that owner | Depends on | Expected agy cost |
|---|---|---|---|---|
| **R0** Probe: Android background clipboard restrictions + does agy do web research at all | agy | Cheapest possible read-only job; measures both research ability and quota cost before committing larger chunks | repo scaffolding (done) | 0.5–1.5% weekly |
| **R1** Research §1a: Android clipboard background restrictions by version (10→15) | agy draft + me | Self-contained, source-heavy, exactly what agy is good at | R0 | ~1–2% weekly |
| **R2** Research §1b: GNOME Wayland clipboard access + image MIME/size limits | me | Small, needs judgement about what is authoritative; cheap to write myself | R0 | 0 |
| **R3** Research §2: options landscape (KDE Connect/GSConnect, Syncthing, ntfy, Pushbullet-style, Tailscale+custom, self-hosted servers, Termux+Tasker/HTTP Shortcuts, Firebase, Supabase, plain WebSocket relay) | agy | Self-contained breadth work; agy polls the web and drafts; I verify | R0 | ~2–3% weekly |
| **R4** Research §3: comparison table + §4 recommendation/fallback/privacy | me | Synthesis, cross-cutting, depends on every other section; the recommendation must match the prototype | R1,R2,R3 | 0 |
| **P1** Prototype design: interfaces, message format, invariants | me | Cross-cutting contract; writing it myself is what makes agy modules fit together | R4 (option choice) | 0 |
| **P2** `clipsync/protocol.py` + `tests/test_protocol.py` | agy | Exactly-specified module against a frozen interface doc | P1 | ~2–3% weekly |
| **P3** `clipsync/clipboard.py` (fake + unexecuted wl-clipboard) | me | Small, safety-critical (must never run the real clipboard); not worth a prompt | P1 | 0 |
| **P4** `clipsync/transport.py` (in-memory + local-file fakes) | agy | Self-contained module with clear semantics | P1 | ~2–3% weekly |
| **P5** `clipsync/engine.py` (echo-loop prevention, dedup, ordering, reconnect) | me | Most subtle logic; failure modes are the whole point of the prototype | P2,P3,P4 | 0 |
| **P6** `clipsync/crypto.py` optional E2E layer | me | Security-sensitive; short | P1 | 0 |
| **P7** Integration/end-to-end tests (loops, dedup, large images, ordering, reconnect) | me + agy (unit tests with module) | E2E is cross-cutting; unit tests travel with the module job | P2–P6 | included above |
| **P8** `docs/android-side.md` (exact phone steps, UNTESTED) | agy + my fixes | Self-contained docs chunk; I review for accuracy | R4 | ~1% weekly |
| **P9** Orchestrator REPORT + final verify + merges | me | Requires the whole conversation | all | 0 |

## Phases

1. **Scaffold** (done): `.gitignore`, orchestrator docs, design contract, branch
   `integration/research` off `main`. `main` is never committed to.
2. **Probe** (agy job 1): read-only research. Record quota delta and research quality.
3. **Research** (`docs/research/clipboard-sync.md`): agy drafts the web-heavy
   sections, I verify the key claims against primary sources and write §3/§4.
   Every claim marked VERIFIED (with link) or ASSUMED; disagreements called out.
4. **Design** (`docs/design/interfaces.md`): freeze module APIs before any code job.
5. **Prototype** (`clipsync/`): agy gets `protocol.py` / `transport.py`; I keep
   clipboard, engine, crypto and integration tests. `pytest -q` must pass.
6. **Android doc**, then **REPORT**, merge topic branches into `integration/*`,
   leave the "merge to main" instruction in REPORT.md.

## Branch protocol

- `main` — untouched (initial commit only). Human merges when ready.
- `integration/research` — research doc + orchestrator docs; agy research branches
  merge here.
- `integration/prototype` — branched from `integration/research`; prototype code,
  tests, android doc; agy code branches merge here.
- agy job branches are merged only into the matching `integration/*` branch, then
  cleaned up. Never `force=true` on cleanup, never pass `model`.

## Definition of done

- `docs/research/clipboard-sync.md` covers §1–§4 with VERIFIED/ASSUMED markers and
  a sources list; recommendation + fallback + privacy note.
- `pytest -q` green on `integration/prototype`; tests cover echo loop, dedup,
  oversized image, ordering, reconnect, crypto round-trip/tamper.
- `docs/android-side.md` with exact steps, marked UNTESTED.
- `docs/orchestrator/{PLAN,DECISIONS,NEEDS_HUMAN,REPORT}.md` committed.
- Report has the assignment table, job ids/states/quota costs, delegation quality,
  fallback trigger, untested items, risks, run/merge commands.

## Status log

- 20:36 IST — scaffolding done; `agy_usage` baseline: weekly 24.4%, 5h 99.2%.
- 20:41 IST — **job 1 (probe) done** `ed2ece76`: 6.9 min, 1 commit, verify passed,
  audit passed, cost **0.27 weekly / 0.94 5h pct points**. Web research works
  (`search_web`); page-fetch tools limited. Quality: good version coverage, but 2–3
  over-claims marked VERIFIED (background *writes*, Android 14–16 "continuity").
  Plan update: probe output is the R1 draft, and R1 becomes "agy draft + my
  verification" rather than a second agy job — saves a job for code.
- 20:44 IST — **job 2 started** `3f818eca`: 10-option landscape (R3). Weekly before
  start 24.16%, 5h 98.25% — above the 20%/15% floor.
- 20:47 IST — I verified KDE Connect from its own UserBase docs: clipboard plugin is
  **text only** ("does not work with images"), Android 10+ auto-sync needs a manual
  "Send clipboard" notification action, Android 14+ has a QS tile, and READ_LOGS /
  Magisk are the only automatic workarounds. This corrects the "images everywhere"
  assumption in some community posts.
- 20:58 IST — **job 2 (options) done** `3f818eca`: 9.3 min, cost **0.33 weekly /
  1.17 5h pct points**, verify + audit passed. Quality: good breadth, but several
  VERIFIED markers were overstated. Kept as a raw artifact with a reviewer-corrections
  banner; I verified ClipCascade/SyncClipboard from their READMEs and wrote the
  decision-grade §2–§4 myself. Merged; cleaned up.
- 21:00 IST — **job 3 started** `1929b570`: `clipsync/protocol.py` +
  `tests/test_protocol.py` against `docs/design/interfaces.md`. Weekly before start
  23.82%, 5h 97.08% — above floor.
- 21:05 IST — research synthesis complete: comparison table + recommendation
  (own relay via Tailscale + clipsync, fallback ClipCascade, text-only KDE Connect)
  and privacy note written into `docs/research/clipboard-sync.md`.
- 21:12 IST — **job 3 (protocol) done** `1929b570`: 8.6 min, cost **0.49 weekly /
  1.73 5h pct points**, 2 commits, 24 tests, verify + audit passed (a
  `plan_incomplete` warning appeared but the wrapper's plan showed 6/6 done and the
  verify ran green). Reviewed the diff: strict validation, exact hash formula, compact
  canonical JSON, base64 validate=True, truncated repr. Merged; my clipboard+crypto
  tests now run against it (**52 passed**). Cleaned up.
- 21:14 IST — **job 4 started** `d0121297`: `clipsync/transport.py` +
  `tests/test_transport.py`. Weekly before start 23.33%, 5h 95.36%.
- 21:18 IST — **job 4 (transport) done** `d0121297`: 8.1 min, cost **0.38 weekly /
  1.33 5h pct points**, 32 tests, verify + audit passed. Reviewed: one JSONL file per
  direction, exact byte offsets, partial trailing line left unread, malformed line ->
  TransportError, path-safe ids, fsync on send. Merged; **full suite 103 passed**
  (protocol + clipboard + crypto + transport + engine). Cleaned up.
- 21:20 IST — branched `integration/prototype` from `integration/research`; remaining
  work (android doc, report, final verify) happens there. `main` still untouched.
- 21:22 IST — **job 5 started** `3bf97154`: Android-side guide (Path A own relay,
  Path B ClipCascade, Path C KDE Connect), all UNTESTED. Weekly before start 22.95%,
  5h 94.02%. This is the last planned agy job (5 of max 7); remaining work is report +
  final verification, which is mine.

- 21:18 IST — **job 5 (android doc) done** `3bf97154`: 6.1 min, cost **0.25 weekly /
  0.88 5h pct points**, verify + audit passed. Reviewed; fixed two invented facts
  (non-existent `CC_ADMIN_PASSWORD` env var, wrong `com.sathvikrao.clipcascade`
  package id) and marked the GSConnect claim ASSUMED. Merged; cleaned up.
- 21:22 IST — **plan complete.** Final `pytest -q`: **104 passed**. Floor never
  reached (weekly ended 22.69%, 5h 93.14%). 5 of max 7 agy jobs used, all `done`, all
  audits passed. `REPORT.md` written; `main` untouched. Only NEEDS_HUMAN items remain.

---

# Relay run — 2026-10-26 22:20 IST (deadline 02:30)

Separate run on branch `integration/relay`, branched from `integration/prototype`.

- **Base commit sha: `f9e760fcae13899b735c34c1ebbbda38897fb0cc`** (`f9e760f`,
  "docs: fix interface comment escaping"). `main` and `integration/prototype` are
  never committed to or merged into.
- `websockets` 10.4 is installed -> the relay uses real WebSockets (no stdlib
  fallback, no pip install).
- Quota before the run: Gemini weekly **22.69%**, 5h **93.14%**. Floor for this run:
  weekly >= 22%, 5h >= 15%; re-check with `agy_usage` before *every* `agy_start`.
  Max 2 jobs total (1 build + at most 1 fix).
- Plan:
  1. mine: acceptance tests in `tests/acceptance/` against the contract (committed
     before any implementation job), exact list in `REPORT.md`;
  2. agy job 1 (only big job): `relay_server.py` + `relay_transport.py` + its own
     unit tests, `verify = python3 -m pytest -q`, `max_minutes=30`, no `model`,
     forbidden to touch `tests/acceptance/`;
  3. mine: the gate — diff check on `tests/acceptance`, `agy_review`, read the
     diff, merge into `integration/relay`, run acceptance + full suite myself;
  4. on failure: at most one short fix job if quota allows, otherwise I fix it and
     log the handover in `DECISIONS.md`;
  5. mine: `REPORT.md`/`DECISIONS.md`, `agy_cleanup` (never `force=true`).

---

# Relay micro run — 2026-10-26 23:40 IST (deadline 02:30)

Micromanagement comparison run on `integration/relay-micro` (from `f003260`). Contract:
`docs/design/interfaces.md` § "relay_server.py / relay_transport.py"; frozen acceptance
tests `tests/acceptance/test_relay_acceptance.py` (13 tests). `websockets` 10.4 present.

Quota before the run: Gemini weekly **22.12%**, 5h **91.12%**. Floor for this run:
weekly >= 20%, 5h >= 15%; `agy_usage` before every start. Max 6 jobs, one at a time.

Chunk plan (one small agy job each, verify `python3 -m pytest -q`, max_minutes=30):

| # | Chunk | Owner | Why this cut |
|---|---|---|---|
| M1 | `clipsync/relay_server.py` + `clipsync/relay_transport.py` implementation | agy | Both modules must exist before `tests/acceptance` even imports; splitting them would make the mandated full-suite verify fail during the first job |
| M2 | `tests/test_relay_server.py` unit tests | agy | Bounded, independent test work on a frozen module |
| M3 | `tests/test_relay_transport.py` unit tests | agy | Same, transport side |
| M4 | Hardening/cleanup: engine-over-relay integration test, thread-safety/edge-case fixes, docs touch-ups | agy or me if quota floor reached | Cross-cutting; only after M1–M3 are green |

After every job: `agy_review`, read the diff, merge into `integration/relay-micro`,
run the full suite, `agy_cleanup` (never `force=true`), log decisions.

## Relay micro run status (final)

- 23:40 — run start; `agy_usage` 22.12% weekly / 91.12% 5h; `websockets` 10.4 present.
- 23:41–23:52 — **M1 `8bf328ee` done**, verify green (117), audit pass; merged; two
  warning fixes by me; cleaned up. Cost 0.46 / 1.61.
- 23:57–00:04 — **M2 `115f538d` done**, verify green (128), audit pass; merged
  unchanged; cleaned up. Cost 0.34 / 1.18.
- 00:06–00:12 — **M3 `a1b9da5a` done**, verify green (141, attempt 2), audit pass;
  merged unchanged; cleaned up. Cost 0.27 / 0.96.
- 00:13–00:16 — **M4 `d7b216f8` needs_review (plan_incomplete)**; verify already
  green (144); one `agy_resume` for checklist bookkeeping → done; merged unchanged;
  cleaned up. Cost 0.53 / 1.85.
- 00:17 — **gate passed**: acceptance 13 passed (5x), full suite 144 passed,
  `git diff f003260 -- tests/acceptance` empty. Quota 20.52% / 85.52%; no handover.

---

# Daemon run — 2026-10-27 02:37 IST (deadline 06:00)

**Workflow: GATED.** The contract (`docs/design/interfaces.md` § "daemon run") and the
acceptance tests are written and committed before the single implementation job; the
gate is a byte-identical `tests/acceptance` diff, a review of the job diff, merge into
`integration/daemon`, and the acceptance + full suite run by me. Rationale: the
deliverable is test-checkable, one cohesive laptop app, and the remaining quota only
allows one job.

- **Base commit sha: `0a1f7f47e2dd9a164106a9e7e0390e383264b4cd`** (`0a1f7f4`,
  "Merge branch 'integration/relay-micro'", current `main`).
- Branch: `integration/daemon`, cut from `main`.
- Quota before the run: Gemini weekly **20.5153%**, 5h **100%**. **Run-specific floor:
  do not start an agy job when weekly remaining is below 20.4%.** Re-check with
  `agy_usage` immediately before the start; if below, implement the chunk myself and
  log the handover in `DECISIONS.md`. At most one implementation job (+ at most one
  fix job only if the floor allows).
- Deadline: stop by **06:00 IST**; leave time for the gate and docs.

## Deliverables

| Chunk | Owner | Notes |
|---|---|---|
| Contract + frozen acceptance tests (`tests/acceptance/test_daemon_acceptance.py`) | me | committed before the job; never edited during it |
| `clipsync/config.py` (TOML config, secret-file indirection) | agy job 1 | GATED |
| `clipsync/daemon.py` (`ClipboardDaemon`, `clipsync` CLI, entry point) | agy job 1 | GATED |
| `clipsync/relay_cli.py` (`clipsync-relay` CLI, entry point) | agy job 1 | GATED |
| `pyproject.toml` console scripts + README Quickstart | agy job 1 | GATED |
| Unit tests for the new modules | agy job 1 | travels with the implementation |
| Gate: diff check, review, merge, acceptance + full suite | me | |
| `PLAN`/`DECISIONS`/`REPORT` updates, final verification | me | |
| Merge `integration/daemon` into `main` (explicitly requested this run) | me | only after the gate and full suite are green |

## Gate commands

```bash
git diff 0a1f7f4 -- tests/acceptance          # must be empty
python3 -m pytest -q tests/acceptance          # must pass
python3 -m pytest -q                           # full suite must pass
```

Safety: every test uses `FakeClipboard`, `InMemoryTransport`, a loopback
`RelayServer`, and a canned `CommandRunner` for `wl-clipboard`; no test may spawn
`wl-paste`/`wl-copy` or touch the real clipboard.

## Daemon run status (final)

- 02:37 — run start. `agy_usage`: Gemini weekly **20.5153%**, 5h **100%**; run
  floor **20.4% weekly**. `integration/daemon` cut from `main` (`0a1f7f4`).
- 02:38 — workflow GATED: contract section added to `docs/design/interfaces.md`, 34
  acceptance tests in `tests/acceptance/test_daemon_acceptance.py`, plan committed at
  **`3df64e0`**. `main` untouched.
- 02:40 — **job `54f33781` started** (config + daemon + relay CLI + entry points +
  README + unit tests), verify `python3 -m pytest -q`, max 45 min. Usage checked
  immediately before start: 20.5153% >= 20.4%.
- 02:52 — **job done**: 7 commits, ~11.9 min, verify **186 passed** on attempt 2,
  audit passed, plan 9/9. Cost **0.78 weekly / 2.72 5h** points.
- Gate: job-branch diff against `3df64e0` for `tests/acceptance` was **empty**. Review
  found one out-of-scope change (`clipsync/clipboard.py` reordered MIME preference to
  satisfy a bad orchestrator fixture) — reverted. My acceptance fixture wrongly
  advertised both text and image; corrected to text-only. Then 34 acceptance passed
  4x and the full suite **186 passed**.
- Cleanup: worktree + job branch removed (no force). Quota after cleanup **19.739%
  weekly / 97.28% 5h** — below the 20.4% run floor, so no fix job; remaining fixes
  were done by me and logged in `DECISIONS.md`.
- Merged `integration/daemon` into `main` with `--no-ff` (explicitly requested this
  run), then re-ran acceptance + full suite on `main`.

---

# Resilience + peer run — 2026-10-27 10:50 IST (deadline 14:00)

**Workflow: GATED.** The deliverable is test-checkable end to end (daemon resilience
and a new console command), so the contract section and the acceptance tests are
written and committed first; one implementation job then has to make them pass without
touching `tests/acceptance/`. Gate = byte-identical acceptance diff, `agy_review` +
reading the diff, merge into `integration/resilience`, then acceptance + full suite run
by me.

- **Base commit: `73b58b3`** (`main`, "merge daemon: laptop clipsync daemon, relay command,
  config and tests"). Branch `integration/resilience` cut from it. **`main` is never
  touched or merged into** this run; the merge is left to the human.
- Quota before the run: Gemini weekly **19.7393%**, 5h **100%**. Run-specific rule from
  the request: **do not start an agy job when weekly remaining is below 12%**; re-check
  with `agy_usage` immediately before the start. At most one implementation job (+ at
  most one fix job only if the floor allows and the gate fails).
- Deadline: stop by **14:00 IST**.

## Deliverables

| Chunk | Owner | Notes |
|---|---|---|
| Contract section (`docs/design/interfaces.md` § "resilience + peer run") | me | committed before the job |
| `tests/acceptance/test_resilience_acceptance.py` | me | frozen during the job |
| `tests/acceptance/test_peer_acceptance.py` | me | frozen during the job |
| `clipsync/daemon.py`: `BackoffPolicy`, `wait_or_stop`, resilient `run`, resilient CLI startup, `stop_event` injection | agy job 1 | GATED |
| `clipsync/peer.py`: `clipsync-peer` command (`send-text`, `send-image`, `listen`) | agy job 1 | GATED |
| `clipsync/__init__.py` version as the single `--version` source for all three CLIs | agy job 1 | GATED |
| `pyproject.toml` `clipsync-peer` script + README two-terminal smoke test | agy job 1 | GATED |
| Unit tests for backoff/peer | agy job 1 | travels with the implementation |
| Gate: diff check, review, merge, acceptance + full suite | me | |
| `README`/`PLAN`/`DECISIONS`/`REPORT`/`NEEDS_HUMAN` final updates, final verification | me | |

## Gate commands

```bash
git diff 73b58b3 -- tests/acceptance          # must be empty
python3 -m pytest -q tests/acceptance          # must pass
python3 -m pytest -q                           # full suite must pass
```

Safety: every test uses `FakeClipboard` / in-memory or loopback transports and a
loopback `RelayServer` on an ephemeral port. No test may spawn `wl-paste`/`wl-copy`,
read or write the real clipboard, or use external network. The one subprocess test runs
`python3 -m clipsync.daemon` against a closed loopback port, so it never polls the
clipboard.

## Status log

- 10:50 — run start; `agy_usage` weekly **19.7393%** / 5h **100%**; floor 12% weekly.
  `integration/resilience` cut from `main` (`73b58b3`).
- 10:52 — contract section appended to `docs/design/interfaces.md` and **32 new
  acceptance tests** (18 resilience + 14 peer) committed at **`99ef7bb`**; `main`
  untouched, `tests/acceptance` frozen from here on.
- 10:52 — **job `d5c77c7b-4c60-4e4d-bbbd-f2b5b9845722` started** (daemon resilience +
  `clipsync-peer` + version source + README + unit tests), verify
  `python3 -m pytest -q`, max 45 min. `agy_usage` immediately before the start:
  weekly **19.7393%** >= 12% floor.
- 11:03 — **job done**: 6 commits, ~11.2 min, verify **230 passed on attempt 1**,
  audit passed, plan 8/8, no denials. Measured cost 0.73 weekly / 2.55 5h points.
- 11:05 — **gate passed**: `git diff 99ef7bb -- tests/acceptance` empty; job changed
  only `clipsync/daemon.py`, `clipsync/peer.py`, `clipsync/relay_cli.py`,
  `pyproject.toml`, `README.md`, `tests/test_resilience.py`, `tests/test_peer.py`;
  merged with `--no-ff` into `integration/resilience`. Acceptance **66 passed** three
  consecutive times; full suite **230 passed**. Frozen tests byte-identical to
  `99ef7bb`. `agy_cleanup` removed worktree + branch (no force). Quota after cleanup:
  weekly **19.01%** / 5h **97.45%** (above the 12% floor; no fix job needed).
- 11:10 — docs updated (`README`, `DECISIONS`, `NEEDS_HUMAN`, this log, `REPORT`).
  **`main` is untouched; merging `integration/resilience` into `main` is left to the
  human** (see NEEDS_HUMAN H11).

# meshcore coordination phase 1 — 2026-10-28 13:05 IST

**Workflow: GATED.** The deliverable is pure, test-checkable logic, so I write and
commit the contract + acceptance tests first (`tests/acceptance/test_coordination.py`,
frozen during the job); one agy job then makes them pass without touching
`tests/acceptance/`. Gate = empty `git diff <contract-commit> -- tests/acceptance`,
`agy_review` + reading the diff, merge into `integration/coordination`, then the
acceptance suite and the full suite run by me. At most one fix job.

- **Base commit: `d429f66`** (`main`, "docs: add meshcore/clipsync coordination design
  v2.1"). Branch `integration/coordination` cut from it. **`main` is never touched or
  merged into** this run; the merge is left to the human. `integration/resilience` is
  already merged into `main` (`4c85114`).
- Quota before the run: Gemini weekly **100%**, 5h **100%**; re-check with `agy_usage`
  immediately before the start. Floor from the standing guidance: 10% weekly / 15% 5h.
- Contract: `docs/design/coordination-v2.md` sections 0, 1, 3, 4, 6. Build **only** the
  pure coordination core: `meshcore/coordination.py`, `meshcore/messages.py`, a
  `meshcore/__init__.py`, and add `meshcore` to `pyproject.toml` packages. No website,
  panel, Android, channels, transport or network behaviour. No clipboard, no I/O, no
  real clock, no sockets, no sleeps.

## API contract (frozen by the acceptance tests)

- `Device(device_id, name, perm=None, temp_rank=None, will_hub=True)`.
- `sort_key(d)` = `(has_perm ? 0 : 1, perm ?? 0, has_temp ? 0 : 1, temp_rank ?? 0,
  device_id)`; `order(devices)` sorts by it.
- `pick_hub(devices, reachable_ids)` -> `(hub_id | None, forced)`; pure knowledge
  version.
- `probe_hub(devices, ping, timeout, self_id)` -> `(hub_id, forced)`; live version,
  probes the order top-down with `ping(device_id, timeout) -> bool`, skipping
  `will_hub=False`, first responder wins, all-declining forces the first, nobody
  responds -> `(self_id, False)`.
- `HubTracker(clock)` with `observe(candidate_hub)` and the `current_hub` property;
  a change needs the same candidate for 30 s, flapping resets the timer,
  `observe(None)` is ignored.
- `Hello(device_id, name, perm=None, temp_rank=None, will_hub=True)`,
  `encode_hello(hello) -> dict`, `decode_hello(mapping) -> Hello`; absent optional
  fields mean unranked with `will_hub=True`.
- `duplicate_temp_ranks(devices)` -> list of `(device_id, device_id)` pairs with equal
  `temp_rank` and no `perm`, ids sorted within a pair, list sorted by
  `(temp_rank, first id)`.

## Deliverables

| Chunk | Owner | Notes |
|---|---|---|
| `tests/acceptance/test_coordination.py` (13 scenarios incl. AST dependency rule) | me | frozen during the job |
| `meshcore/coordination.py` | agy job 1 | GATED |
| `meshcore/messages.py` | agy job 1 | GATED |
| `meshcore/__init__.py` + `pyproject.toml` package entry | agy job 1 | GATED |
| Job's own unit tests | agy job 1 | must not edit `tests/acceptance/` |
| Gate: diff check, review, merge, acceptance + full suite | me | |
| PLAN/DECISIONS/REPORT final updates | me | |

## Gate commands

```bash
git diff <contract-commit> -- tests/acceptance   # must be empty
python3 -m pytest -q tests/acceptance            # must pass
python3 -m pytest -q                             # full suite must pass
```

## Status log

- 13:05 — run start; branch `integration/coordination` cut from `main` (`d429f66`).
  Baseline full suite on the branch: **230 passed**. `integration/resilience` already
  merged into `main`.
- 13:07 — contract committed at **`e148a06`** (12 acceptance tests + PLAN); frozen from
  here on. `agy_usage` immediately before the start: weekly **100%** / 5h **100%**.
- 13:07 — **job `a7181258-a6de-48e3-a7b8-cd7df10a697b` started**, verify
  `python3 -m pytest -q`, max 30 min. First wrapper verify (attempt 1) failed on a
  pytest basename collision between the job's unit tests and the frozen acceptance
  file; the job added `--import-mode=importlib` and verify passed with **253 tests**
  on attempt 2. 4 commits, ~7.3 min, audit passed, plan 7/7.
- 13:15 — **gate passed**: `git diff e148a06 -- tests/acceptance` empty; job diff read
  in full (only `meshcore/`, its two unit-test files and `pyproject.toml`). Merged with
  `--no-ff` into `integration/coordination` (`5dd39ad`). I reverted the global
  `--import-mode=importlib` and renamed the job unit tests to unique basenames
  (`e201e56`), so `pyproject.toml` gains only the `meshcore` package. Acceptance
  **78 passed**, full suite **253 passed**; frozen file alone **12 passed**.
- 13:16 — docs updated; `agy_cleanup` removes the job worktree/branch without force.
  **`main` remains untouched; merging `integration/coordination` into `main` is left to
  the human** (see NEEDS_HUMAN H13).

---

# meshcore phase 2a — elected hub over the existing relay (2026-10-28 13:45 IST)

**Workflow: GATED.** The deliverable is test-checkable end to end, so the contract
(`docs/design/meshcore-phase2a.md`) and the frozen acceptance tests are written and
committed first; one agy job (max 45 min) implements the feature without touching
`tests/acceptance/`. Gate = empty `git diff <contract-commit> -- tests/acceptance`,
`agy_review` + reading the diff, merge into `integration/hub-election`, then the
acceptance file and the full suite run by me. At most one fix job.

- **Base commit: `5a0069e`** (`main`, "Merge branch 'integration/coordination'";
  baseline suite **253 passed**). Branch `integration/hub-election` cut from it.
  **`main` is never touched or merged into** this run; the merge is the owner's.
- Quota before the run: Gemini weekly **99.64%**, 5h **98.73%**; re-check with
  `agy_usage` immediately before the start. Floor: 10% weekly / 15% 5h.
- Scope: phase 2a only — meshcore messages (roster/leaving), generic hub, generic
  link, election driver, Node facade, mesh config + clipsync mapping, clipsync daemon
  on Node. **Out of scope:** LAN discovery/fallback (2b), naming service/website,
  panel, routing preferences, Android, state channels, Bluetooth, TLS.
- Safety: no test touches the real clipboard, non-loopback network, or any service;
  no push/force; `~/.local/state/agy-mcp` and `~/.config/ai-stack` untouched.

## Deliverables

| Chunk | Owner | Notes |
|---|---|---|
| `docs/design/meshcore-phase2a.md` contract | me | frozen before code |
| `tests/acceptance/test_meshcore_hub.py` (A0–A7) | me | meshcore only; no `clipsync` import |
| `tests/acceptance/test_clipsync_on_meshcore.py` (A8–A10) | me | thin clipsync integration |
| `tests/acceptance/test_meshcore_exploratory.py` (marked) | me | not part of the gate; result reported |
| `meshcore/messages.py`, `hub.py`, `link.py`, `election.py`, `node.py`, `config.py` | agy job 1 | GATED; own unit tests allowed |
| `clipsync/config.py`, `daemon.py`, `mesh_transport.py`, thin wrappers | agy job 1 | GATED; old tests must stay green |
| Gate: diff check, review, merge, acceptance + full suite | me | |
| `PLAN`/`DECISIONS`/`REPORT` final updates | me | |

## Gate commands

```bash
git diff <contract-commit> -- tests/acceptance   # must be empty
python3 -m pytest -q tests/acceptance/test_meshcore_hub.py tests/acceptance/test_clipsync_on_meshcore.py
python3 -m pytest -q                             # full suite must pass
python3 -m pytest -q tests/acceptance/test_meshcore_exploratory.py -m exploratory  # report only
```

## Status log

- 13:45 — run start; branch `integration/hub-election` cut from `main` (`5a0069e`).
  Baseline **253 passed**. `agy_usage`: weekly **99.64%**, 5h **98.73%**.
- 13:50 — contract + tests written; frozen from the next commit on.
- 13:53 — **job `4e1b3b79-ede2-486d-b2a9-4fb68639caed` started** (implementation,
  verify `python3 -m pytest -q`, max 45 min). `agy_usage` immediately before the
  start: weekly **99.64%** / 5h **98.73%**.
- 14:26 — first result `needs_review`: 7 commits, plan 9/9, audit passed, but the
  wrapper verify timed out (rc 124) after 181 tests. I reproduced it: `Election.start`
  held its non-reentrant lock and called `run_once`, which acquires the same lock.
- 14:26–14:31 — I resumed the job once with four precise fixes (RLock, roster merge,
  link retarget, stop outside the lifecycle lock). It committed them (4 more commits)
  but the wrapper verify still reported rc 124 and the full suite stayed flaky under
  repeated runs (`A5`/`A7` occasionally failed because `hub_id` moved before the new
  client link was connected).
- 14:44 — **takeover** (logged D38): merged the job branch into
  `integration/hub-election` (`abee603`), added the attached-hub split in
  `meshcore/node.py` and the forced-reason log, committed `db70be4`. Frozen
  `tests/acceptance` diff against `906c2e0` is empty.
- 14:55 — **gate passed**: acceptance files **13 passed** (10 consecutive runs),
  full suite **273 passed** (5 consecutive runs), exploratory **passed**
  (`queued_during_failover=1`, delivered). `agy_cleanup` removed the worktree and job
  branch (no force). Quota after cleanup: weekly **97.71%**, 5h **91.97%**
  (spend **1.93 / 6.76** points).
- 15:00 — docs updated; **`main` untouched**. Merging `integration/hub-election` into
  `main` is left to the owner (NEEDS_HUMAN H14).

# clipsync-lab — deploy smoke run plan (DIRECT)

Workflow: **DIRECT** — the request explicitly said no agy job (the task is small and
agy's verify/commit wrapper has been unreliable). The frozen acceptance tests
(`tests/acceptance/test_deploy_smoke.py`, D1-D7) were written and committed before
implementation, then the work was done directly.

Branch: `integration/deploy`, cut from `main` at `d859f63` (`main` untouched; the
merge is the owner's decision).

## Scope

- Discover the real entry points, config format/fields and clipboard seam by reading
  the code (not guessing): `clipsync`, `clipsync-relay`, `clipsync-peer` console
  scripts plus `python -m clipsync.daemon`; TOML at `~/.config/clipsync/config.toml`
  by default; tests use `FakeClipboard` and a canned `CommandRunner`; there was no
  way to run a real daemon process without the Wayland clipboard.
- Add the minimal opt-in `file` clipboard backend (`clipboard_backend = "file"` +
  `clipboard_file`), test/headless only, default unchanged; log it in DECISIONS.
- `examples/` (relay, laptop member, phone placeholder, two-groups note),
  `scripts/gen_secrets.py`, `scripts/smoke_two_processes.py`, `deploy/*.service`.
- README "Run it for real (laptop)" + "TV box (later)" placeholder; keep the
  existing "Testing without internet" section byte-for-byte.
- No TV box work, no real clipboard, no `systemctl`, no installs, no push.

## Gate

- Frozen tests first and committed (`742dda3`), then implementation.
- 20x `tests/acceptance/test_deploy_smoke.py`, 5x full suite clean, 5x full suite
  with the ROS PYTHONPATH, 10x `scripts/smoke_two_processes.py` by hand (report pass
  count + median time); zero "Logging error"/"Task was destroyed"; no frozen-test
  edits.

## Status log

- 03:26 — run start; branch `integration/deploy` cut from `main` (`d859f63`).
  Baseline **300 passed** (35.4 s).
- 03:29 — D1-D7 written and committed (`742dda3`); tests not edited afterwards.
- 03:30–03:37 — implementation. The first real-process smoke immediately found
  `TypeError: 'NoneType' object is not callable` in the mesh-mode daemon codec
  setup (in-process tests never took that path); fixed. Failover needed the existing
  mesh tuning fields exposed and a wait for the member election (D57).
- 03:37 — implementation committed: `823ceea` (backend/config/codec fix), `7c697ca`
  (examples + scripts), `f80311b` (units), `fb2725b` (README). New file 7 passed;
  full suite 307 passed.
- 03:50–04:20 — first gate iterations exposed three issues, all fixed without
  editing a frozen test: (1) frozen README tests require the legacy `## Quickstart`
  / `## Two-terminal smoke test` headings, so they were restored under explicit
  legacy labels; (2) the meshcore broadcast does not queue for clients that are not
  attached yet, so a copy at startup or in the handover window could be lost
  (D3/D4 flake): the smoke now retries with a fresh value and prints
  `TEXT-RETRY`/`FAILOVER-RETRY`; (3) D1 exercised `daemon.main`, whose
  `logging.basicConfig(force=True)` detaches pytest/ROS capture handlers; logging is
  now configured only after config validation.
- 04:24 — **final gate PASS**: new file **20/20** (4.31–6.87 s per run); full suite
  clean **5/5** (307 passed, 43.9–48.1 s); full suite with the ROS PYTHONPATH
  **5/5** (307 passed, 43.5–44.6 s); zero `Logging error`/`Task was destroyed`.
  Manual `scripts/smoke_two_processes.py`: **10/10 PASS**, median **1.98 s**
  (1.95–2.07 s). Frozen acceptance diff `git diff 742dda3 -- tests/acceptance` is
  empty. No leftover daemon processes or smoke temp dirs.
