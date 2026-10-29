# Manual checklist — xclip backend on the real GNOME Wayland laptop

**Docs only.** This checklist is for the owner to run on the physical laptop; the
agent never runs it and never touches the real clipboard. It verifies what the
loopback/fake-runner test suite cannot: real XWayland behaviour, real GNOME focus
rules, real clipboard-manager interactions and the taskbar-flicker symptom.

Test date: 2026-10-29. Branch: `feat/xclip-backend`. Baseline: 337 tests on `main`.

## Before you start

1. Install/inspect the tool: `command -v xclip` (expected `/usr/bin/xclip`) and
   `echo "$DISPLAY"` (expected something like `:0`). On a Wayland session this is the
   XWayland display.
2. Confirm the session type: `echo "$XDG_SESSION_TYPE"` → `wayland`.
3. Copy `examples/member-laptop.toml` to `~/.config/clipsync/member-laptop.toml`,
   fill the real token/e2e key (`chmod 600`), and add:

   ```toml
   clipboard_backend = "xclip"
   ```

4. Have a second device (phone or another machine) in the same group with a relay
   running, so "did it sync?" is observable. Non-sensitive test text only.
5. Start the daemon in a terminal and keep the log visible:

   ```bash
   python3 -m clipsync.daemon --config ~/.config/clipsync/member-laptop.toml
   ```

Record for every step: PASS/FAIL, the copied content, whether it reached the other
device, how long the copy took to appear, and any daemon log line.

## Checks

| # | Check | How | Expected |
|---|-------|-----|----------|
| 1 | Startup is clean | Start the daemon with `clipboard_backend = "xclip"` | No `Configuration error`; a single startup line; no repeated warnings in the first 30 s |
| 2 | Plain text | Select a word in a terminal, `ctrl+shift+c`, then the same content in the other device's clipboard | Appears on the other device within ~1 poll interval (0.2 s) |
| 3 | Multi-line | Copy a 5–10 line block from a text editor | Newlines preserved exactly on the other device; no trailing bytes added |
| 4 | Non-ASCII | Copy `héllo wörld — 日本語 🙂` | Identical characters on the other device (no mojibake, no dropped emoji) |
| 5 | Copy from Chromium | Select text in a Chromium page and copy it | Same as #2 |
| 6 | Copy from gnome-text-editor | Copy text from GNOME Text Editor | Same as #2 |
| 7 | Rapid double copy | Copy `A` and then `B` within ~0.2 s | `B` (the last value) reaches the other device; no stuck `A`; no daemon warning |
| 8 | Same text twice | Copy `A`, wait for it, copy `A` again | Second copy does not re-send (engine echo guard); the other side shows no duplicate/mismatch |
| 9 | Other window focused | Copy in a Chromium window, then immediately click another window and wait | The copy still reaches the other device (xclip does not need the source window focused) |
| 10 | Image copy | Take a screenshot / copy an image from a viewer | Image arrives on the other device (if that device runs an image-capable backend); otherwise a clear log line and the daemon stays up |
| 11 | Taskbar flicker | Watch the GNOME dash/taskbar for 60 s while idle with the daemon running | **No** transient windows/flash. Compare with `clipboard_backend = "wayland"`, where the flicker was reproducible |
| 12 | Survive `killall xclip` | While the daemon runs: `killall xclip`, then copy text again after xclip is restarted (`xclip` is started on demand) | Daemon process still alive; at most one rate-limited `Clipboard backend error (retrying, …)` WARNING per distinct error per 60 s; sync resumes on the next successful copy, without restarting the daemon |
| 13 | Clipboard stays owned | After a write from the other device, copy something else locally | Local copy syncs out; the daemon's earlier write is not re-published |
| 14 | Missing binary at startup | Run with a PATH that has python3 but no xclip: `mkdir -p /tmp/no-xclip && for c in python3 sh env; do ln -sf "$(command -v $c)" /tmp/no-xclip/$c; done` then `PATH=/tmp/no-xclip python3 -m clipsync.daemon --config ~/.config/clipsync/member-laptop.toml` from the repo root | Clear `Configuration error: … xclip …` and exit code 2 — no poll-loop crash storms |
| 15 | DISPLAY unset | `env -u DISPLAY python3 -m clipsync.daemon --config …` | Clear `Configuration error: … DISPLAY …` and exit code 2 |
| 16 | Stop/restart | `ctrl+c` the daemon, restart | Clean shutdown, no `Logging error` / `Task was destroyed` lines; sync resumes |

## After the checks

- Restart the daemon without `clipboard_backend = "xclip"` if you do not intend to
  keep it, and restore the clipboard (copy something harmless).
- Do not paste the real token or e2e key into the report/screenshots.
- Report back: OS + GNOME version, `uname -r`, whether XWayland was running
  (`pgrep Xwayland`), the exact daemon log lines for #11/#12, and the FAIL rows with
  their observations. Those decide whether xclip becomes the default on Wayland
  sessions (see `docs/orchestrator/NEEDS_HUMAN.md`, H31).
