# clipsync-lab — Orchestrator Plan

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
