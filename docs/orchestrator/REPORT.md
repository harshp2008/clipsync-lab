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
