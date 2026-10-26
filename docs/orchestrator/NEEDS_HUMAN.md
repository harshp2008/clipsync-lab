# Needs a human

Items that need the account owner, the phone, or system changes. Everything here was
deliberately **not** done by the agent. Work continues around them.

| # | Item | Why it needs you | Exact steps |
|---|---|---|---|
| H1 | Test the Android side on a real phone | The agent cannot use your phone and must not touch a real clipboard | Follow `docs/android-side.md`; report back which steps fail |
| H2 | Real relay/pairing credentials | Creating accounts/keys and storing secrets is out of scope for this run | After choosing the transport, generate the shared key/salt locally (see android doc) and never commit it |
| H3 | Installing anything system-wide (KDE Connect/GSConnect, Tailscale, Termux packages) | System packages/services are forbidden in this run | Install by hand from your distro/F-Droid/Play; phone steps in `docs/android-side.md` |
| H4 | Running the real Wayland backend | `WlClipboardBackend` is unexecuted to protect your clipboard | On the laptop: `python -c "from clipsync.clipboard import WlClipboardBackend; print(WlClipboardBackend().read())"` in a scratch session |
| H5 | Merge `integration/prototype` into `main` | The brief reserves `main` for you | `git checkout main && git merge --no-ff integration/prototype` after reviewing `docs/orchestrator/REPORT.md` |

_Updated as the run progresses; see REPORT.md for the final state._
