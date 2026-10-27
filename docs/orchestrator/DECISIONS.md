# Decisions log

Each entry: what, why, and the job id where relevant. Assignment changes get a one-line
"assignment reasoning" as required by the run brief.

## D1 — Branch strategy (20:36 IST)
`main` is never committed to. All work lands on `integration/research`, then
`integration/prototype` (branched from research); agy job branches merge only into the
matching `integration/*` branch. Why: the run brief forbids merging into `main`, and a
linear topic branch lets a restarted session resume from a single HEAD.

## D2 — No package installs (20:36 IST)
Use the system Python 3.12 with the already-present `pytest` 7.4.4 and `cryptography`
41.0.7 (also `nacl`). No `.venv` needed and no network installs, which keeps the run
inside the "no packages/network" safety rule. `.venv/` stays git-ignored in case a
session later needs one.

## D3 — Frames are opaque bytes (20:36 IST)
Transports carry `bytes` frames rather than dicts, so the optional Fernet codec is a
pure wrap/unwrap and fakes stay trivial. The plaintext frame is canonical JSON from
`protocol.encode_frame`. Recorded in `docs/design/interfaces.md`.

## D4 — Echo prevention keyed on content hash of the last clipboard state (20:36 IST)
After applying a remote payload the engine sets `_last_local_hash`, so the next local
read cannot re-send it. Dedup also uses `msg_id` and per-origin `seq`. Why not a
time-window suppression set alone: it can drop a legitimate re-copy. Known limitation:
a backend that normalizes image bytes breaks hash equality; documented.

## D5 — Prototype stays phone-free and cloud-free (20:36 IST)
All tests use `FakeClipboard`, `InMemoryTransport`/`FileTransport` and fixed fixtures.
`WlClipboardBackend` is implemented against an injectable runner but never executed;
only its pure command-building/`choose_mime` helpers are tested. Why: the real
clipboard may hold passwords and the brief forbids touching it.

## D6 — Probe before committing research chunks to agy (20:36 IST)
Baseline `agy_usage`: weekly 24.4%, 5h 99.2% (floor 20%/15%). Job 1 is a cheap
read-only research probe so the quota cost per job and agy's research quality are
known before assigning more work. Assignment reasoning: agy is the default owner of
self-contained research; the probe is the cheapest way to price the rest.

## D7 — Research owner split (provisional, 20:36 IST)
R1 (Android restrictions) and R3 (options landscape) are drafted by agy because they
are source-heavy and self-contained; R2 (GNOME Wayland + MIME limits) and R4
(synthesis/recommendation) stay with me because they need judgement and must agree
with the prototype. Reasoning: agy never sees this conversation, so broad-but-
bounded web research is the best fit; synthesis is cross-cutting.

## D8 — Probe result: agy can do web research, and it is cheap (20:41 IST)
Job `ed2ece76`: state done, audit passed, verify passed, 6.9 min, 0.27 weekly / 0.94
5h pct points. `search_web` works; page fetching is limited. Quality is usable but
over-confident: it marked Android 10 background *writes* and "Android 14/15/16
continuity" as VERIFIED citing pages that do not say that. Assignment reasoning: keep
agy on source-heavy breadth research (R3) but every claim gets spot-checked by me
before it reaches `clipboard-sync.md`; R1 is downgraded from a separate agy job to
"probe draft + my verification", which frees quota for code jobs.

## D9 — KDE Connect is text-only, verified (20:47 IST)
KDE UserBase's Clipboard plugin section says "Share the clipboard between devices ...
This does not work with images though", and documents the Android 10+ manual "Send
Clipboard" notification action, the Android 14+ Quick Settings tile, and the
READ_LOGS/Magisk workarounds. Assignment reasoning: this single primary source
disqualifies KDE Connect as the *recommended* option for this brief (images are a hard
requirement) and supports it as a text-only fallback; the custom relay becomes the
prototype target.

## D10 — Recommendation: own relay primary, ClipCascade fallback (21:05 IST)
Primary: Tailscale/WireGuard + a small self-hosted relay, with the `clipsync` message
format/E2E/engine prototyped here. Fallback: **ClipCascade** (Docker, text/images/
files, optional client-side E2E, Android APK, native-Wayland Linux client) because it
is the turnkey version of the same architecture; the prototype is the laptop side of
the primary option. Text-only zero-server fallback: KDE Connect/GSConnect. Assignment
reasoning: the recommendation is cross-cutting synthesis, so I own it; agy's option
survey fed it but its over-claims were corrected and ClipCascade/SyncClipboard were
re-verified from their READMEs.

## D11 — Protocol accepted as-is (21:12 IST)
Job `1929b570` produced a 391-line `protocol.py` and 24 tests. I read the whole diff:
frozen-dataclass `object.__setattr__` is used correctly, the hash matches the contract
exactly, `from_wire` rejects wrong version/id/hash/seq/origin/timestamp/base64, unknown
keys are ignored, and `canonical_json` is compact/sorted/no-ASCII-escape. One residual
edge: `decode_frame` maps only `UnicodeDecodeError`/`JSONDecodeError`, so pathological
deeply-nested JSON could raise `RecursionError` — acceptable for a prototype, noted in
REPORT risks. Assignment reasoning: protocol was the best-specified chunk (exact
formula + 24 acceptance tests) and agy executed it cleanly, so no rework needed; I kept
the trust-boundary review myself.

## D12 — Prototype branch cut after transport (21:20 IST)
`integration/prototype` branches from `integration/research` now that all code modules
exist and the full suite is green (103 tests). Remaining doc/report work lands there so
the final branch is self-contained. Assignment reasoning: the android doc is
self-contained docs work, so it goes to agy as job 5; I keep the report/merges.


## D13 — Android guide merged with two factual corrections (21:18 IST)
Job `3bf97154` produced a well-structured guide but invented a `CC_ADMIN_PASSWORD`
Docker env var (not in the ClipCascade README) and used the wrong Android package id
(`com.sathvikrao.clipcascade`; the README's ADB commands use `com.clipcascade`). I
replaced the Docker block with the README's command + default-credential warning,
fixed the package id, and marked the GSConnect Wayland claim `[ASSUMED]`. Reasoning:
these are two-line fixes, so editing them was cheaper and safer than an `agy_resume`;
the doc is still marked UNTESTED throughout.

## D14 — Final state (21:22 IST)
Plan complete. 104 tests pass; 5/5 agy jobs done and audited; 1.72 weekly / 6.05 5h
points spent; floor never reached. `integration/prototype` is the merge candidate;
`main` is untouched. Only `NEEDS_HUMAN.md` items remain (phone test, accounts/system
changes, build-vs-buy decision, merge to main).

## D15 — Relay micro run: chunking and merge protocol (23:40 IST)
Micromanagement comparison run on `integration/relay-micro`. Chunks M1–M4 are in
`PLAN.md`; each is one agy job with `verify="python3 -m pytest -q"`, `max_minutes=30`,
no `model`, and an explicit "do NOT modify tests/acceptance/" rule. M1 combines
`relay_server.py` and `relay_transport.py` because the acceptance module imports both
at import time: any single-module job would fail the mandated full-suite verify for
reasons unrelated to its own quality. Every job is reviewed, merged only into
`integration/relay-micro`, tested, then cleaned up. `f003260` is the acceptance-test
baseline. No other relay implementation is consulted (contract-only run).

## D16 — M1 accepted with two orchestrator warning fixes (23:56 IST)
Job `8bf328ee-6f23-4141-8164-b2db1c403410` (M1) produced `relay_server.py` (248
lines) and `relay_transport.py` (278 lines); its verify run passed 117 tests, but the
tail showed 25 `process_request` DeprecationWarnings and 10
`PytestUnhandledThreadExceptionWarning`s from an `asyncio.CancelledError` escaping the
transport's shutdown path. I read the merged diff, then made two small edits myself
before the next chunk: made `process_request` a coroutine, and suppressed the expected
`CancelledError` from the connection task after `close()` cancels it. These are
shutdown-noise fixes, not behaviour changes; full suite is 117 passed with no warnings
and the acceptance suite passed 3x consecutively. Reasoning: cheaper and safer than a
resume for two warning-only defects.

## D17 — M2 server unit tests accepted unchanged (00:04 IST)
Job `115f538d-1fec-4a7f-b492-99e39f52542b` added 11 tests covering the size formula,
constructor validation, lifecycle/idempotent stop, port-conflict `OSError`, handshake
403s, verbatim forwarding/no echo, 1009 oversize close, and stop dropping clients.
Verify passed 128 tests on the first attempt, audit passed, and the diff contained only
`tests/test_relay_server.py`. No edits needed. Cost 0.34 weekly / 1.18 5h.

## D18 — M3 transport unit tests accepted unchanged (00:12 IST)
Job `a1b9da5a-8e7a-4549-ba8b-b84515c4707a` added 13 tests: `relay_url`, no-I/O
construction, pre-connect errors, FIFO/opacity with 20 mixed frames, client-side size
limit, wrong-token/closed-port failures with thread-cleanup checks, reconnect
True/False across a server restart, and idempotent close. Verify passed 141 tests on
attempt 2; audit passed; only `tests/test_relay_transport.py` changed. The wrapper also
reported a benign `plan_incomplete` warning while the job's plan showed 4/4 done and
its verify was green, so no resume was needed. Cost 0.27 weekly / 0.96 5h.

## D19 — M4 hardening and E2E tests accepted after one bookkeeping resume (00:16 IST)
Job `d7b216f8-9c60-4f74-9e7a-87c0fb7ad981` added three engine-over-relay tests
(bidirectional text without echo, encrypted 64 KiB image passthrough with a
wrong-key peer dropping it, outbox flush after a relay restart) and made two
hardening edits: `hmac.compare_digest` for the bearer token and an `RLock` lifecycle
lock. The job reached `needs_review` solely because two checklist items said "run
pytest" and agy treats test execution as wrapper-delegated; the wrapper's verify had
already passed 144 tests on attempt 2 with audit pass. I resumed once with that fact
(no code changes requested), it closed the plan (6/6) and finished `done`; I reviewed
the diff and merged it unchanged. This is the only job state anomaly of the run and it
was bookkeeping, not a code/test failure. Cost 0.53 weekly / 1.85 5h (including the
resume).

## D20 — Relay micro final state (00:17 IST)
`integration/relay-micro` (from `f003260`) has the relay modules, 27 new unit tests,
3 new E2E tests, and the orchestrator docs. Gate: `python3 -m pytest -q
tests/acceptance` → 13 passed (also 5 consecutive runs); `python3 -m pytest -q` → 144
passed; `git diff f003260 -- tests/acceptance` empty; suite warning-free. Quota ended
at 20.52 weekly / 85.52 5h (floor not reached), 4 of max 6 jobs used, all cleaned up.
`main` is untouched; merge command is in `REPORT.md`. No part of this run read any
other relay implementation.

## D21 — Daemon run: GATED, one frozen acceptance suite, one job (02:38 IST)
Contract + 34 acceptance tests committed at `3df64e0` before any implementation. The
laptop app is test-checkable and cohesive, so GATED fits; quota (20.5153% weekly)
allowed exactly one job above the run's 20.4% floor. Branch `integration/daemon` from
`main`. The user explicitly asked for the app on `main`, so after the gate + local
re-verification I merged the integration branch into `main` with `--no-ff`; no push.
Job `54f33781-39b2-426b-b286-38f17844fd87`.

## D22 — Gate: rejected an unrelated clipboard change, fixed my own fixture (02:53 IST)
The job passed verify (186 tests, attempt 2) and left `tests/acceptance` byte-identical
(0-line diff vs `3df64e0`), but its diff also changed `clipsync/clipboard.py`:
`choose_mime` now returned `text/plain` before images and gained a
`WlClipboardBackend.choose_mime` alias. That contradicts the frozen contract
("Images win over text") and was only there to satisfy my acceptance fixture, which
wrongly advertised `text/plain` **and** `image/png` and then expected the text read
command. I reverted `clipsync/clipboard.py` to the base version and corrected the
fixture to advertise `text/plain` only (the image path is already covered by
`test_choose_mime`). Decision: never accept a behaviour regression to make a bad test
pass. Also rejected a job-added `WlClipboardBackend.choose_mime` alias as unnecessary.
After the corrections: acceptance 34 passed four times, full suite 186 passed.

## D23 — No fix job; self-fixes and final merge (02:54 IST)
`agy_usage` after cleanup was **19.739% weekly**, below this run's 20.4% floor, so I did
not start/resume another job. The two fixes above were made by me, re-tested, and
documented. `integration/daemon` was merged into `main` (`--no-ff`) because the direct
request said "on main, add"; `main` was then re-verified (`34` acceptance / `186`
full). No push, no force, no system changes. Worktree + job branch cleaned up with
`force` false.

## D24 — Resilience + peer run: GATED with two frozen suites (10:52 IST)
The request is test-checkable (daemon resilience + a new console command), so this run
used GATED again: the contract section and 32 acceptance tests (18 resilience, 14 peer)
were committed at `99ef7bb` before the single implementation job. Branch
`integration/resilience` was cut from `main` at `73b58b3`; `main` is never committed to
or merged into this run (the human asked to leave the merge). Run-specific floor from
the request: **do not start a job below 12% weekly** — checked immediately before the
start at 19.7393%, so the one job was allowed. Job
`d5c77c7b-4c60-4e4d-bbbd-f2b5b9845722`.

## D25 — Job accepted unchanged at the gate (11:05 IST)
The job (6 commits, ~11.2 min, audit passed) passed the wrapper verify with 230 tests
on attempt 1. Gate facts: `git diff 99ef7bb -- tests/acceptance` = **0 lines**; the
job touched only the seven intended files and nothing in `clipsync/clipboard.py`,
`protocol.py`, `engine.py`, `transport.py`, `relay_server.py`, `relay_transport.py`,
`config.py` or `crypto.py`. I read the whole diff before merging and found no
out-of-scope behaviour change, no clipboard access in the peer, and no secret logging.
Kept as-is; no fix job was needed and none was started. Cost 0.73 weekly / 2.55 5h
points (quota after cleanup 19.01% weekly / 97.45% 5h).

## D26 — How resilience is defined (and why `--once` is unchanged) (11:05 IST)
`BackoffPolicy.delay` is `min(max_delay, base * 2**attempt)` scaled by
`1 - jitter + 2*jitter*r`; waits go through `wait_or_stop`, which slices at 0.05 s when
a `stop_event` is supplied, so SIGINT/SIGTERM (and tests) interrupt even a 30 s
backoff promptly while the injected `sleep` is still called exactly once per wait when
there is no event. `ClipboardDaemon.run` counts failed connect/poll iterations as
iterations, logs one WARNING per failure through `clipsync.daemon`, and reconnects on
the next iteration; a healthy iteration is unchanged (one poll, one
`sleep(poll_interval)`), which is why the existing exact-sleep tests still pass.
Continuous `main` no longer exits when the relay is down at startup; it retries until
stopped and returns 0. `--once` intentionally keeps the old fail-fast contract
(`Transport connection error: ...`, rc 1) because it is a one-shot connectivity check,
already covered by the previous acceptance suite. The peer reuses the daemon's
config/transport/codec/backoff so there is one config and one protocol/codec
implementation, and it never imports or touches the clipboard.

## D27 — Final state and the merge left to the human (11:10 IST)
`integration/resilience` is green: **66/66 acceptance** (three consecutive runs) and
**230/230 full suite**; the frozen acceptance files are byte-identical to `99ef7bb`.
`main` remains at `73b58b3`. Job worktree and branch were removed with
`agy_cleanup` (no force). Per the request, the merge of `integration/resilience` into
`main` is **not** performed; the command is in `REPORT.md` and NEEDS_HUMAN H11.
