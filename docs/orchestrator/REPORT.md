# Report — clipsync relay run (network transport)

Run: **2026-10-26 22:20 → 22:50 IST** (deadline 02:30, finished early).
Repo: `/home/harsh/Documents/GITHUB PROJECTS/clipsync-lab`.
Branch: **`integration/relay`** (branched from `integration/prototype`). `main` and
`integration/prototype` are untouched. This report supersedes the prototype report
from the earlier run, which remains readable via
`git show f9e760f:docs/orchestrator/REPORT.md`.

## 1. Result

The relay feature is implemented, reviewed and merged. Acceptance tests written
before the implementation job all pass, as does the full suite:

| Run | Command | Result |
|---|---|---|
| Acceptance only | `python3 -m pytest -q tests/acceptance` | **13 passed** (2.53 s) |
| Full suite | `python3 -m pytest -q` | **130 passed** (2.93 s) |
| Full suite, repeat (flake check) | `python3 -m pytest -q` | **130 passed** (2.96 s) |

`websockets` 10.4 was already installed, so the relay uses real WebSockets; the
stdlib-stream fallback and its NEEDS_HUMAN swap item were **not** needed.

## 2. Base and branch topology

- **Recorded base sha: `f9e760fcae13899b735c34c1ebbbda38897fb0cc`** (`f9e760f`,
  `integration/prototype` HEAD at branch time). Recorded in `PLAN.md`.
- `f003260` — my spec-first acceptance tests + relay contract + plan.
- Job 1 base `f003260` → merged as `8a10748` (3 job commits).
- Job 2 base `8a10748` → merged as `ad163c1` (1 job commit).
- `integration/relay` HEAD = `ad163c1`; all agy job branches were merged with
  `--no-ff` and then deleted after `agy_cleanup` (no `force=true`).

## 3. Acceptance tests (written by me, before implementation)

`tests/acceptance/test_relay_acceptance.py` — black-box, public API only, loopback
and ephemeral ports only, no real clipboard:

1. `test_relay_transport_satisfies_transport_protocol` — `isinstance(…, Transport)`,
   no I/O in the constructor.
2. `test_server_binds_loopback_only` — `127.0.0.1` only; `0.0.0.0`/`::`/`localhost`/
   hostnames rejected; empty token rejected; `running`/`port` lifecycle.
3. `test_two_clients_exchange_text_and_image` — real `ClipMessage` frames with a
   text payload and an 8 KiB+ PNG payload, both directions, bytes intact.
4. `test_message_fans_out_to_every_other_client` — 3 clients; both non-senders get it.
5. `test_sender_never_receives_its_own_message`.
6. `test_per_sender_message_ordering` — 25 frames arrive in exact order.
7. `test_opaque_non_json_frames_round_trip` — arbitrary binary incl. NUL/CRLF/JSON-ish.
8. `test_wrong_token_is_rejected` — `connect()` raises `TransportError`, peer unaffected.
9. `test_missing_token_is_rejected` — raw client without the auth header is refused.
10. `test_oversize_frame_is_rejected_by_the_server` — server closes offender (1009),
    bystander receives nothing and stays connected.
11. `test_oversize_frame_is_rejected_by_the_client` — client `send` raises, nothing sent.
12. `test_client_reconnects_after_server_restart` — server stops and rebinds the same
    port; both clients reconnect and delivery resumes.
13. `test_encrypted_payloads_pass_through_untouched` — Fernet ciphertext is byte-equal
    on the far side and decodes; the relay is constructed with no key.

The contract those tests pin is in `docs/design/interfaces.md` §
"relay_server.py / relay_transport.py" (additive).

## 4. agy jobs

Quota was checked with `agy_usage` immediately before each start. No `model`
parameter, `force` never passed to cleanup.

| # | Job id | Title | State | Wall | Commits | Verify | Audit | Weekly cost | 5h cost |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `cecc329b-972b-4b5e-b318-44b444c00927` | Implement relay server + transport | **needs_review** (wrapper: `invalid_commit_message`) | 8.1 min | 3 | **did not run** | not run | 0.41 | 1.45 |
| 2 | `c6e7dc8e-918f-4c53-83b2-ffb734faf486` | Fix `RelayTransport.connect()` deadlock | **done** | 3.1 min | 1 | **pass** (`130 passed`) | **pass** | 0.16 | 0.57 |
| | | **Total** | | **~11 min** | **4** | | | **0.57** | **2.02** |

Job 1 stopped in `needs_review` because its *last, redundant* `COMMIT_MSG` had a
76-char subject (wrapper limit is 72). The worktree was clean, the three real commits
were already made, and the code was complete; the wrapper therefore never ran verify
or the audit. I treated the branch as unreviewed code, read the whole diff, merged it,
and ran the gate myself. Job 2 (the one allowed fix job) fixed the bug the gate found
and passed verify + audit normally.

## 5. Did agy touch `tests/acceptance/`? No.

- `git diff f003260..job1 -- tests/acceptance` → **empty**.
- `git diff 8a10748..job2 -- tests/acceptance` → **empty**.
- `git log f003260..job1 -- tests/acceptance` → empty; same for job 2.
- `git log f9e760f..HEAD -- tests/acceptance` → only `f003260`, my own test commit.
- `git diff f9e760f..HEAD --stat -- tests/acceptance` → only additions from
  `f003260`; no job commit touched the file.

Nothing needed restoring.

## 6. Gate results (commands actually run)

1. Diff check on the acceptance tests (above) — clean.
2. `agy_review` for both jobs; read every changed line. Only the four allowed files
   in job 1; only `relay_transport.py` + its unit test in job 2. No fatal audits, no
   denials.
3. Merged job 1:
   `git merge --no-ff agy/implement-network-relay-server-transport-bdba1b23`
   → `8a10748`.
4. Ran the tests myself: `timeout 240 python3 -m pytest -q tests/acceptance` →
   **7 passed, then hang** in `test_wrong_token_is_rejected`.
5. Root-caused the hang myself: `RelayTransport.connect()` called `self.close()`
   while already holding the non-reentrant `threading.Lock` `_lifecycle_lock`; any
   initial connect failure (wrong token / server down) self-deadlocked instead of
   raising `TransportError`. Its own
   `tests/test_relay_transport.py::test_relay_transport_connect_failure_to_down_server`
   hung for the same reason. Every other acceptance test passed individually.
6. One fix job with the failing test names and the diagnosis → `done`, verify
   `130 passed`, audit `passed`. Re-ran the diff check, reviewed the one-line RLock
   change + regression test, merged it (`ad163c1`).
7. Re-ran the gate myself: **13/13 acceptance**, **130/130 full suite**, twice.

## 7. What I did myself (and why)

- Wrote the relay contract addition and all 13 acceptance tests, and committed them
  before any implementation job existed (the spec-first part of the run).
- Made the merge/gate decisions; ran every test that decided acceptance.
- Diagnosed the deadlock from the diff + hang (no polling); handed the exact failing
  tests and root cause to the single allowed fix job rather than editing code myself,
  because quota was above the floor (22.28% weekly at that point).
- **No production code was written by me in this run.** I fixed no code by hand; the
  only edits outside the job branches are docs/orchestrator + the acceptance tests
  (both mine by design).
- Recorded the job-1 `needs_review` cause here and in `DECISIONS.md` (D16).

## 8. Quota

- Start: **22.69% weekly / 93.14% 5h** remaining.
- End: **22.12% weekly / 91.12% 5h**.
- Measured spend: **0.58 weekly / 2.02 five-hour points** (2 jobs).
- The run's floor (**22% weekly / 15% 5h**) was respected: checked before both starts
  (22.69% and 22.28%), never below 22%. Weekly ended 0.12 points above the floor, so
  no further job should be started this week without the human's say-so. Max 2 jobs
  for this run was reached exactly; no job was wasted.

## 9. Files

Added: `clipsync/relay_server.py` (216 lines), `clipsync/relay_transport.py`
(331 lines after the fix), `tests/test_relay_server.py`, `tests/test_relay_transport.py`,
`tests/acceptance/test_relay_acceptance.py`.
Changed (mine): `docs/design/interfaces.md`, `docs/orchestrator/{PLAN,DECISIONS,
NEEDS_HUMAN,REPORT}.md`.
No existing module, test, `pyproject.toml`, `main` or `integration/prototype` was
modified.

## 10. Risks / limitations (documented, not hidden)

1. **No TLS**: `ws://` on loopback only. Remote use needs a `wss://` endpoint and a
   reverse proxy/TLS; the token is a single shared secret, not per-device auth.
2. No persistence or offline queue: frames sent while disconnected raise
   `TransportError` (the engine's outbox is what retries them).
3. The relay is a dumb fan-out server: no rooms, no ACKs, no replay protection.
4. `websockets` emits a DeprecationWarning ("declare process_request as a coroutine")
   on 10.4; compatibility with future majors is not tested.
5. No Android/phone client exists yet (unchanged from the prototype run).
6. Reconnect assumes the same URL; DNS/Tailscale address changes are out of scope.

## 11. Commands for you

```bash
cd "/home/harsh/Documents/GITHUB PROJECTS/clipsync-lab"
git checkout integration/relay

# test (system Python; nothing to install)
python3 -m pytest -q                       # 130 passed
python3 -m pytest -q tests/acceptance      # 13 passed

# inspect
git log --oneline --graph -12
git diff f9e760f..HEAD --stat

# merge when ready (main is yours; never merged by the agent)
git checkout main
git merge --no-ff integration/relay
```

See `NEEDS_HUMAN.md` for the two open human items (merge-to-main and production
deployment hardening).
