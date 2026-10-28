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

## D28 — Coordination phase 1: GATED contract from `main` (13:05 IST)
The deliverable is pure, exactly-specifiable logic, so this run used GATED again.
`integration/resilience` was confirmed **already merged** into `main` (`4c85114`), so
`integration/coordination` was cut from `main` at `d429f66` (the design-doc commit) and
`main` was not touched. The frozen contract is `tests/acceptance/test_coordination.py`
at **`e148a06`**: 12 test functions covering the 13 numbered scenarios (item 12 is the
required comment inside scenario 6, not a separate test) plus an AST test that fails if
any module under `meshcore/` imports an app package. Quota before the start was **100%
weekly / 100% 5h**, above the standing 10%/15% floor.

## D29 — Job accepted, with a gate cleanup for pytest import mode (13:15 IST)
Job `a7181258-a6de-48e3-a7b8-cd7df10a697b` ("meshcore coordination phase 1 pure core",
base `e148a06`, 4 commits, ~7.3 min, audit passed). The wrapper's first verify failed
with a pytest **import-file mismatch**: the job's own `tests/test_coordination.py`
collided by basename with the frozen `tests/acceptance/test_coordination.py`. The job
fixed it by adding `--import-mode=importlib` to `pyproject.toml`; verify then passed
**253 tests** on attempt 2. I accepted the implementation, but at the gate I reverted
that global pytest-config change and renamed the two job unit tests to
`tests/test_meshcore_coordination.py` / `tests/test_meshcore_messages.py`, leaving
`pyproject.toml` with only the required `packages = ["clipsync", "meshcore"]` change.
Reason: the request scoped `pyproject` to packaging, the project already relies on
unique test basenames, and default prepend mode keeps the existing suite's import
behaviour unchanged. Cost measured by the wrapper: **0.36 weekly / 1.27 5h points**.

## D30 — Coordination interfaces where the spec allowed more than one shape
The design doc fixes the behaviour but not every Python signature; these are the
choices frozen into the acceptance tests and recorded here rather than silently
guessed: `HubTracker(clock)` adopts the **first** non-None candidate immediately (a
"change" needs the previous hub, so boot is not delayed; the 30 s debounce applies to
every later change); `probe_hub` probes the given `devices` list top-down and does not
special-case `self_id` (callers may exclude themselves; if they are listed and their
ping answers, self is a normal first responder), and only `(self_id, False)` is
returned when no ping answers; `duplicate_temp_ranks` returns
`(device_id, device_id)` pairs (ids sorted inside a pair, list sorted by rank then id),
not `Device` objects; `encode_hello` returns a JSON-able `dict` and always emits
`will_hub` while omitting `perm`/`temp_rank` when None, and `decode_hello` accepts any
Mapping. Phase 1 deliberately keeps the device_id tiebreak for colliding temp ranks;
the "larger fragment is the reference" alternative (design §12.2) is **not**
implemented and needs phase-2 fragment membership.

## D31 — Phase 2a: GATED contract first (2026-10-28 13:50 IST)
Deliverable is end-to-end test-checkable, so GATED again. `integration/hub-election`
was cut from `main` at `5a0069e` (phase-1 coordination already merged; baseline **253
passed**). The interface contract is `docs/design/meshcore-phase2a.md`; the frozen
tests are `tests/acceptance/test_meshcore_hub.py` (A0–A7), 
`tests/acceptance/test_clipsync_on_meshcore.py` (A8–A10) and the marked exploratory
`tests/acceptance/test_meshcore_exploratory.py`. Quota before the start: weekly
**99.64%** / 5h **98.73%**, far above the 10%/15% floor.

## D32 — meshcore transport may use `websockets` (planned deviation, documented)
The request says "meshcore/ imports only the standard library", but phase 2a moves the
existing WebSocket relay into `meshcore/hub.py` and its client into
`meshcore/link.py`; the project already depends on `websockets` 10.4. Re-implementing
RFC 6455 in the standard library (handshake, masking, fragmentation, ping/pong,
close codes) would be a larger correctness and security risk than reusing the existing
transport, and would put the frozen relay compatibility tests at risk. Decision: the
**pure core** (`config`, `coordination`, `messages`, `election`, `node`) is
standard-library-only and the A10 test enforces it; `hub.py`/`link.py` are the only
modules allowed to import `websockets`, and A10 enforces exactly that allowlist plus
the no-app-import rule. This is the one literal deviation from the request and is
reported to the owner.

## D33 — Mode switch: `peers` non-empty selects mesh; old configs stay legacy
`relay_url` becomes optional only when `peers` is non-empty. With no peers,
`load_config` still requires `relay_url` and `daemon.main` keeps the existing
`RelayTransport` path, so old config files behave exactly as before (A9). With peers,
`daemon.main` builds a `meshcore.node.Node` and `clipsync.mesh_transport.NodeTransport`.
`build_transport` is unchanged for compatibility (`test_builders` expects a
`RelayTransport`). Adding `port` (0..65535, default 8765) was necessary because static
peers must dial a known hub address and the task listed only `bind`; `bind` is used by
the elected hub role.

## D34 — Every node listens; being hub is an election result
`probe_hub` needs to probe devices before any election has completed, so every Node
starts its hub listener on `bind:port` and only the elected node behaves as the hub
for its app (publishing/forwarding still happens on the server, but clients connect
to the elected hub and migrate away on a change). This avoids a bootstrapping
chicken-and-egg where nobody answers the first probe. Consequence: a stale client can
briefly sit on a non-hub listener during a transition; election plus debounce moves
it, and tests wait for the converged hub before asserting payload flow.

## D35 — `Node.publish` raises without a route; the engine keeps its outbox
The contract says publish is immediate so a failure is visible to callers. The
clipsync `NodeTransport` maps `NodeError` to `TransportError`, which the existing
`SyncEngine` already handles by keeping frames at the head of its outbox and retrying
on the next poll. This preserves the tested outbox behaviour and is what the
exploratory failover-copy test exercises; the result is reported, not patched over.

## D36 — `RelayTransport` is a thin subclass of `Link`
`clipsync/relay_transport.py` becomes `class RelayTransport(Link)` with `hello=None`
and the legacy thread name `clipsync-relay-transport`, translating `LinkError` to
`TransportError` in `connect`/`send`/`recv`. A10 checks the subclass relation, the
absence of the WebSocket loop in the wrapper, and the line budget. `RelayServer`
becomes a `HubServer` subclass keeping the loopback-only constructor validation so
every existing relay test stays green.

## D37 — External API choices frozen by the tests
`Node.hub_id` and `Node.is_hub` are read-only observations added to the minimal API so
tests can assert convergence and "exactly one hub"; callbacks remain the primary
notification path. A hub's roster includes the hub's own Hello; a client's roster is
the last roster received and starts as `[self]`. Only helloed clients receive control
messages (old clients stay payload-only), which keeps their frame stream byte-clean.
A reserved data envelope is used only hub→helloed-client; client→hub data remains raw
opaque bytes. `can_reach(a, b)` blocks both probes and link connects for the partition
test. The `roster`/`leaving` codecs use compact JSON text frames.

## D38 — Phase 2a takeover after a verify timeout (2026-10-28 14:44 IST)
The single implementation job (`4e1b3b79…`) produced 7 commits, plan 9/9 and a clean
audit, but the wrapper verify timed out. The cause was a real self-deadlock:
`Election.start()` held its non-reentrant `threading.Lock` and then called
`run_once()`, which acquires the same lock. I reproduced it, resumed the job once
with precise instructions (RLock, merge roster with configured peers, retarget the
link when the hub changes, stop outside the lifecycle lock). The job applied all four
(commits `a02a17e`, `fa2cdd5`, `11dcc25`, `f5b5339`), but the wrapper verify still
reported rc 124 and repeated full-suite runs exposed a remaining race: `hub_id`
followed the election result before the new client link was attached, so A5/A7 could
observe convergence while a node was not yet reachable. I took over at the gate,
merged the job branch into `integration/hub-election`, and added the attached-hub
split (`_desired_hub` vs `_current_hub`), the forced-reason log, and the final gate
runs. The frozen `tests/acceptance` were never edited (`git diff 906c2e0 --
tests/acceptance` empty). Cost to this point: **1.93 weekly / 6.76 5h points**; job
worktree and branch removed without force.
Reason for takeover rather than a second fix job: the cumulative 45-minute job budget
was already spent, the failure was a small, well-understood race, and I had already
reproduced and fixed it locally; the user's rule allows taking over and logging it.

## D39 — `hub_id` means "attached", `is_hub` means "elected" (14:50 IST)
The frozen tests use `node.hub_id` as the convergence signal, then publish. If
`hub_id` follows the election tracker, it moves before the new `Link` is connected
(the election callback runs the connect afterwards), so a peer can wait for
`hub_id == "n0"` and then publish into a node that is still reconnecting (observed as
intermittent A5/A7 failures in the full suite). `Node` now keeps `_desired_hub` (the
election result, drives `is_hub`, publish routing and `_ensure_link`) and
`_current_hub` (the attached hub, returned by `hub_id`; set immediately for self,
only after a successful client connect for a remote hub). A failed connect leaves
`hub_id` unset and `publish()` retries `_ensure_link`; the next election change also
retries. This is a semantic clarification of the contract, not a test change.

## D40 — Forced-hub logging and the reason on every hub change (14:52 IST)
Election logs one WARNING `All reachable devices decline will_hub; forced hub: <id>`
when `probe_hub` returns `forced=True` (timestamped once per forced candidate, not
per heartbeat). `Election.last_forced` is exposed so `Node` logs every hub change at
INFO with old hub, new hub and a reason string (`forced: no reachable device
will_hub` or `election probe`). The task asked for the reason at INFO and a WARNING
for the forced case; both are now explicit.

## D41 — Config/API decisions for phase 2a (14:52 IST)
- Mesh mode is selected by a non-empty `peers` list. `relay_url` is required only in
  legacy mode; old config files load with the new fields defaulted and the daemon
  keeps the old single-relay path (A9). This is the smallest change that satisfies
  both "use Node" and "old configs behave exactly as before".
- `port` was added next to the requested `bind`: static peers must dial a known
  address, and a node cannot discover its own ephemeral port from config. Default
  `8765`, validated 0..65535.
- `DaemonConfig.to_mesh_config()` maps name (defaulting to `device_id`), token, rank
  fields and peers; mesh timings keep the task defaults (10 s heartbeat, 3 missed
  pongs, 2 s probe, 30 s debounce).
- The hub listener runs on every node from `start()`, because `probe_hub` must be
  able to reach a candidate before that candidate has won an election; being the
  active hub is the election result. This is logged here because the wording "the
  next one starts the relay" could also be read as lazily starting the listener.

## D42 — Security: off-loopback `bind` is token-only, no TLS yet (14:52 IST)
`bind` is honoured by the elected `HubServer`; the compatibility `RelayServer`
remains loopback-only by its existing contract. Binding a tailnet/LAN address makes
the relay reachable to anyone who can route to it: the only authentication is the
shared bearer token, sent in the WebSocket handshake over cleartext `ws://` (no TLS
in phase 2a). On a shared LAN an attacker can observe/replay the token and join the
mesh; the hub forwards opaque bytes, so the optional clipsync Fernet key still
protects payloads end to end, but without it the hub can read clipboard frames. Treat
off-loopback binding as trusted-network only until a TLS/reverse-proxy phase. This is
recorded in NEEDS_HUMAN H15 and the REPORT security note.
