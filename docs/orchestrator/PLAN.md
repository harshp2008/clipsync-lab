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
