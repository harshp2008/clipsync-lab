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

## D15 — Relay contract fixed before the build job (22:22 IST)
`docs/design/interfaces.md` gained the additive relay section: sync `RelayServer`
(`start`/`stop`, background asyncio loop, `host` must be exactly `127.0.0.1`, token in
the `Authorization: Bearer` handshake header, HTTP 403 rejection, fan-out only to other
clients, oversize -> close 1009) and sync `RelayTransport` (same `Transport` methods,
auto-reconnect, client-side size check, `recv` non-blocking). Token in a header (not
the URL) so it cannot leak via URL logging. Frame limit is
`4 * ((MAX_IMAGE_BYTES + 2) // 3) + 8192` — base64 expansion of the largest legal image
payload plus JSON slack — because `MAX_IMAGE_BYTES` alone would reject a legal encoded
image. Adding this before writing the tests is what let the acceptance tests be
implementation-agnostic. Job: none yet.

## D16 — Job 1 accepted from `needs_review` after full review (22:38 IST)
`cecc329b` ended in `needs_review` with `invalid_commit_message`: its final,
redundant `COMMIT_MSG` had a 76-char subject (wrapper limit 72). The worktree was
clean and the three real commits complete; the wrapper therefore skipped verify and
audit. I did **not** resume (a resume would re-run a finished job for a bookkeeping
message) and did not edit the worktree by hand. I read the whole diff, checked that
`tests/acceptance/` was untouched, merged the branch into `integration/relay`, and ran
every test myself. Assignment reasoning: the code was finished; only the gate mattered.

## D17 — One fix job for the connect deadlock, not a hand-fix (22:40 IST)
The gate found `test_wrong_token_is_rejected` hanging: `RelayTransport.connect()` called
`self.close()` while holding the non-reentrant `threading.Lock` `_lifecycle_lock`.
Quota was 22.28% weekly (above the 22% floor), so the run's rule "at most one fix job"
was used instead of fixing it myself. Short prompt named both hanging tests and the
root cause. Job `c6e7dc8e`: `done`, one-line `Lock` -> `RLock` change plus a regression
test, verify `130 passed`, audit passed. This was job 2 of the 2-job cap.

## D18 — Real WebSockets, no stdlib fallback (22:21 IST)
`python3 -c "import websockets"` succeeded (10.4), so the relay uses the library and
no NEEDS_HUMAN swap item was created. The `api` is still the same byte-pipe interface,
so a future `wss://` production deployment does not change `RelayTransport`.

## D19 — Final state (22:50 IST)
Gate passed on my own runs: 13/13 acceptance, 130/130 full suite, twice.
`integration/relay` HEAD `ad163c1`; job branches merged with `--no-ff` and cleaned up
(`force` never used). Quota 22.69% -> 22.12% weekly (0.58 spent) and 93.14% -> 91.12%
5h (2.02 spent). No production code written by me. Remaining work is human-only:
merge to `main`, TLS/deployment hardening (see NEEDS_HUMAN H10–H11).
