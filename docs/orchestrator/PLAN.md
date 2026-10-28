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
| Contract `docs/design/meshcore-lan.md` | me | in progress |
| Frozen `tests/acceptance/test_meshcore_lan.py` (L1–L11) | me | pending |
| Baseline suite + red run, commit contract/tests, freeze hash | me | pending |
| `agy_usage` + ONE agy implementation job (45 min) | agy | pending |
| Gate: `git diff <contract> -- tests/acceptance` empty, `agy_review`, read diff, merge | me | pending |
| Acceptance 20x; full suite 5x clean + 5x ROS PYTHONPATH | me | pending |
| Docs: coordination-v2 §5/§13, meshcore-lan.md, README "Testing without internet" | me | pending |
| Report, DECISIONS.md, NEEDS_HUMAN.md | me | pending |

## Run log (2026-10-29)

- 01:25 — branch `integration/lan` cut from `main` `0944778` (289 tests); design,
  code and existing acceptance style read. Contract and tests next.

## Prior runs

The groups run (2026-10-29) and the earlier research/prototype/relay/daemon runs are
recorded in `DECISIONS.md`, `REPORT.md` and `NEEDS_HUMAN.md`. `main` contains all of
them up to phase 2a + groups.
