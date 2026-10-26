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
