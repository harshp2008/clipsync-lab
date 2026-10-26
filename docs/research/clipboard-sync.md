# Two-way clipboard sync: Ubuntu GNOME Wayland ↔ Android

Research and design note for a reliable two-way clipboard sync (text **and** images)
between an Ubuntu GNOME **Wayland** laptop and an **Android** phone.

Method: every factual claim is marked **[VERIFIED]** with a link to a source that
actually says it, or **[ASSUMED]** where it is inference/experience that this run
could not confirm from a primary source. Where sources disagree, both are shown.
Research date: 2026-10-26. Option survey detail lives in
`docs/research/agy-options-landscape.md`; the prototype lives in `clipsync/`.

---

## 1. Why the current setup is flaky, one-way, and has no images

Four independent limits stack up. Fixing only one (e.g. "use a better app") leaves the
others, which is why clipboard sync between these platforms feels unreliable.

### 1.1 Android: background apps may not *read* the clipboard (Android 10+)

- **[VERIFIED]** On Android 10 (API 29) and higher, an app can read clipboard data only
  if it is the **default input method editor (IME)** or the app that **currently has
  focus**; apps running in the background cannot read the clipboard. A foreground
  service does **not** count as focus. [Android 10 privacy changes](https://developer.android.com/about/versions/10/privacy/changes)
- **[VERIFIED]** The restriction is about *reading*. Copying on the phone still works
  normally; the system clipboard is shared by all apps. What breaks is any app that
  wants to *watch* the clipboard and push changes out.
- **[VERIFIED]** Because of this, KDE Connect's clipboard plugin on Android 10+ is not
  automatic: it exposes a **"Send Clipboard"** action in its persistent notification
  (and a **Quick Settings tile on Android 14+**), and the user must tap it to push the
  current phone clipboard. Rooted phones can use a Magisk module; a non-root workaround
  uses `READ_LOGS` + `SYSTEM_ALERT_WINDOW` granted over ADB, which makes KDE Connect
  briefly pop an invisible focused window to grab the clipboard. [KDE UserBase — Clipboard plugin](https://userbase.kde.org/KDEConnect)
- **[ASSUMED]** Writing the clipboard from the background (`setPrimaryClip`) is less
  restricted than reading and generally works from a service on stock Android 10–15,
  but it is not a documented guarantee and some OEM builds drop it. This needs an
  on-device test (see `NEEDS_HUMAN.md`).
- **[VERIFIED]** Android 12 added a system toast when an app reads clipboard content
  from another app. [9to5Google](https://9to5google.com/2021/05/18/android-12-beta-1-clipboard-notifications-are-coming-and-toasts-now-show-their-source/)
- **[VERIFIED]** Android 13 added a system clipboard preview overlay and an automatic
  clear of the primary clip after about **one hour** (3,600,000 ms). Sources differ on
  whether the auto-clear is on by default in every build: 9to5Google reports the
  released behaviour, while XDA's Developer Preview write-up describes it as an
  option. [9to5Google](https://9to5google.com/2022/08/16/android-13-clipboard/),
  [XDA](https://www.xda-developers.com/android-13-clipboard-auto-clear/)
- **[VERIFIED]** Apps can mark a clip as sensitive (`EXTRA_IS_SENSITIVE`) so previews
  and keyboard caches redact it; a sync tool must respect that and should not upload
  marked clips. [Secure clipboard handling](https://developer.android.com/topic/security/risks/secure-clipboard-handling)
- **[ASSUMED]** Android 14's foreground-service-type rules (a `dataSync` service needs
  a declared type and matching permission) add friction for a long-running sync
  service, and nothing in Android 14/15/16 documented here relaxes the Android 10 read
  restriction. [FGS types](https://developer.android.com/about/versions/14/changes/fgs-types-manifest)

**Consequence:** fully automatic phone→laptop sync of arbitrary copies is impossible
for an ordinary background app on stock Android. Any honest design either (a) asks for
one tap per phone→laptop transfer (notification action / QS tile / share sheet), or
(b) ships an IME, which is exempt but is a keyboard the user must select, or (c)
requires root/ADB. Laptop→phone is the easy direction.

### 1.2 GNOME Wayland: background clipboard readers are second-class

- **[VERIFIED]** The core Wayland clipboard/selection protocol gives selection data to
  the client that currently has **keyboard focus**. A clipboard manager that wants to
  read while unfocused needs a privileged protocol extension. [Clipmer — Wayland clipboard managers](https://clipmer.app/blog/best-clipboard-manager-wayland)
- **[VERIFIED]** In practice `wl-paste` can return stale data or hang when its client
  does not have focus, depending on the compositor and how the tool gets a data
  device. [Super User — copying on Wayland](https://superuser.com/questions/1189467/how-to-copy-text-to-the-clipboard-when-using-wayland)
- **[ASSUMED]** wlroots' `wlr-data-control` protocol, which tools such as `cliphist`
  use for background watching, is **not implemented by Mutter (GNOME)**; `wl-paste
  --watch`-style loops are therefore not a reliable cross-app watcher on GNOME.
  Evidence is indirect (bug reports asking for wlroots-protocol support on GNOME:
  [cursor-clip issue](https://github.com/Sirulex/cursor-clip/issues/5)). This is the
  single biggest "it works on my Sway box but not on GNOME" trap.
- **[VERIFIED]** `wl-clipboard` itself handles images: `wl-copy < photo.png` copies an
  image, `wl-paste --list-types` lists the offered MIME types, and `wl-paste --type
  image/png` reads one. [wl-clipboard README](https://github.com/bugaevc/wl-clipboard)
- **[ASSUMED]** The reliable background reader on GNOME is a **GNOME Shell
  extension** (GSConnect does this), because it runs inside the compositor process and
  is not subject to the focus rule. A plain user process (Python, `wl-paste` loop) is
  not reliable while another window is focused.
- **[ASSUMED]** XWayland apps bridge their X11 selection to the Wayland clipboard
  through the compositor; this mostly works but can add a focus/ownership race and is
  another source of "sometimes it doesn't paste" reports.

**Consequence:** the laptop side needs either a Shell-level integration (GSConnect) or
an explicit focus-coupled poll (`wl-paste` while a terminal has focus), or a small
compositor-portal/extension helper. A pure background Python loop using `wl-paste` is
not a dependable watcher on GNOME Wayland.

### 1.3 Images: different MIME vocabulary and a hard Android size ceiling

- **[VERIFIED]** Android passes clipboard data through Binder transactions, whose
  buffer is about **1 MB shared per process**; exceeding it raises
  `TransactionTooLargeException`. Large images placed directly in a `ClipData` can
  therefore fail, and practical limits are device-specific, often ~500 KB.
  [TransactionTooLargeException](https://developer.android.com/reference/android/os/TransactionTooLargeException),
  [Stack Overflow](https://stackoverflow.com/questions/61146485/clipboard-transactiontoolargeexception-data-parcel-size)
- **[ASSUMED]** The workaround is to put a **content URI** (FileProvider, with a read
  grant) on the clipboard instead of the bytes, or to downscale/re-encode to well under
  ~500 KB before setting the clip. The URI approach avoids Binder size limits but the
  receiving app must honor the grant and the file must stay readable.
- **[ASSUMED]** Wayland apps advertise different image MIME sets: `image/png` is nearly
  universal, `image/jpeg`, `image/webp`, `image/gif` and `image/bmp` are common but not
  guaranteed, and animated GIFs may arrive as a single frame. A sync tool should
  negotiate a supported type instead of assuming PNG.
- **[ASSUMED]** There is no cross-platform "image clipboard" standard: some apps copy
  a file *path* (`text/uri-list`), some copy HTML with an `<img>`, some copy raw
  `image/png`. A naive reader picks the wrong representation; the prototype prefers an
  image MIME when one is offered and otherwise text.
- **[VERIFIED]** KDE Connect explicitly does not sync images: "Share the clipboard
  between devices ... This does not work with images though." [KDE UserBase](https://userbase.kde.org/KDEConnect)

**Consequence:** images need an explicit `kind` + MIME + byte/URI representation, a
size cap, and compression/URI fallback on Android. Text-only tools will never grow
image support by accident.

### 1.4 Other reasons it feels flaky or one-way

- **[VERIFIED]** KDE Connect discovers peers with UDP broadcast and uses dynamic ports
  **1714–1764** TCP/UDP; it expects devices on the same LAN and fails across guest
  Wi-Fi, VLANs, and some VPNs unless configured. [KDE UserBase — Troubleshooting](https://userbase.kde.org/KDEConnect)
- **[ASSUMED]** Battery optimisation/Doze kills background sync services, so transfers
  silently stop until the app is reopened; this is a common "it worked yesterday"
  cause.
- **[ASSUMED]** Many "clipboard sync" features are deliberately **one-way** (desktop
  pushes to phone, or phone shares to desktop) because the phone→desktop direction is
  the one Android restricts. A tool advertising "clipboard sync" may only implement
  the easy direction.
- **[ASSUMED]** Reconnect handling is often missing: after Wi-Fi drops, a naive client
  loses the queued clip or replays old ones, which looks like duplication or lost
  copies.
- **[ASSUMED]** Echo loops: if both sides blindly re-upload whatever they see on their
  clipboard, A→B→A can ping-pong forever. Without content hashes + "last applied"
  tracking this is easy to hit.

---

## 2. The options

Full per-option notes (mechanism, pricing, limits, sources) are in
[`agy-options-landscape.md`](agy-options-landscape.md); this is the decision-relevant
summary. The eleven concrete options (the brief's ten families, with self-hosted split
into the two usable projects) cluster into five families.

**A. Peer-to-peer LAN tools**

1. **KDE Connect / GSConnect** — mature, free, no account, paired over the LAN
   (UDP discovery, TCP/UDP 1714–1764). Text is bidirectional; **images are explicitly
   not supported** by the clipboard plugin. On Android 10+, phone→laptop is a manual
   "Send clipboard" notification action (QS tile on Android 14+) unless you use the
   READ_LOGS/Magisk workaround; desktop→phone is automatic. GSConnect is the
   GNOME-Shell-native variant and is the best Wayland story of any option.
   [VERIFIED] [KDE UserBase](https://userbase.kde.org/KDEConnect)
2. **Syncthing-based** — Syncthing is file sync, not clipboard sync; you build
   watcher scripts that write `clipboard.txt`/`clip.png` into a shared folder. Text
   and images both work as files, but the glue is fragile and the Android watcher hits
   the same background-read wall. [ASSUMED] High effort, high maintenance.

**B. Push / notification relays**

3. **ntfy** — simple HTTP pub/sub with Android push. Text can be delivered and copied
   with a notification action; image attachments are possible but are not injected
   into the clipboard automatically, and the free hosted tier caps attachments at
   2 MB. Self-hostable. Its E2E story is not documented on the pages checked here, so
   do not assume it. [VERIFIED for service/limits](https://docs.ntfy.sh/publish/#attachments) [ASSUMED for E2E]
4. **Pushbullet-style** (Pushbullet, Join) — cloud account, closed clients, text-only
   universal copy/paste (images not supported by the automatic path), paid tier
   (~$5/mo Pushbullet Pro). No maintained native Linux/Wayland client. [VERIFIED for features](https://blog.pushbullet.com/2014/08/20/introducing-universal-copy-paste/) [ASSUMED for pricing/current state]

**C. Self-hosted clipboard servers (the turnkey end of the spectrum)**

5. **ClipCascade** — Docker/JAR server, desktop clients (including a Linux CLI with
   native-Wayland `wl-paste --watch` + polling fallback) and an Android APK. Text,
   images and files; optional **client-side E2E** with a shared salt and PBKDF-style
   key derivation. Android automatic phone→laptop uses the same ADB `READ_LOGS` +
   overlay workaround as KDE Connect; without it you use the app's manual actions.
   Default server message cap is **1 MiB**, configurable (`CC_MAX_MESSAGE_SIZE_IN_MiB`)
   or bypassed in P2P mode; large images need that raised.
   [VERIFIED] [ClipCascade README](https://github.com/Sathvik-Rao/ClipCascade)
6. **SyncClipboard** — .NET server, or WebDAV/S3 storage, or the desktop client's
   built-in server. Text/images/files, desktop clients for Linux/Windows/macOS, and
   several Android clients: manual notification-triggered, AutoJs6 overlay, and the
   **Fcitx5 IME plugin** which gets seamless phone→laptop by riding the IME
   exemption. Linux docs recommend installing `wl-clipboard` on Wayland. HTTPS is
   supported; E2E is not advertised, so the server sees content unless you add it.
   [VERIFIED] [SyncClipboard README](https://github.com/Jeric-X/SyncClipboard)

**D. Build-it-yourself**

7. **Tailscale + custom relay** — put the laptop and phone on a WireGuard tailnet and
   run your own small HTTP/WebSocket relay (on the laptop or a VPS). You control the
   message format, image cap, E2E and reconnect logic; you also own the Android client
   and its battery/background behaviour. [VERIFIED for Tailscale pricing/transport](https://tailscale.com/pricing)
8. **Plain WebSocket relay** — the same idea without Tailscale; needs `wss://` plus
   auth, or it is plaintext on the wire. Custom client required. [ASSUMED]
9. **Termux + Tasker / HTTP Shortcuts** — scripted glue: Tasker/HTTP Shortcuts trigger
   HTTP calls, Termux pushes/pulls. Text works; Termux's clipboard API is text-only;
   Tasker can handle image URIs but background clipboard reads are still restricted.
   High effort, brittle. [VERIFIED for Termux API limits](https://github.com/termux/termux-api) [ASSUMED for Tasker specifics]

**E. Cloud backends**

10. **Firebase Firestore/Realtime DB** — realtime sync for free, but Firestore
    documents are hard-capped at **1 MiB**, so images need Cloud Storage, an account,
    and custom clients on both ends. [VERIFIED for the 1 MiB limit](https://firebase.google.com/docs/firestore/quotas)
11. **Supabase** — Postgres + Realtime + Storage; 50 MB objects, free tier with
    project pausing after inactivity. Also needs a custom Android client. Pricing/limits
    should be re-checked before committing. [ASSUMED]

**What this means:** only the custom relay (D) and the self-hosted servers (C) satisfy
"both directions **and** images **and** no third-party cloud" at once. KDE Connect
wins on setup and Wayland integration but loses images and automatic phone→laptop.


---

## 3. Comparison

## 3. Comparison

Scale: Reliability = how often a copy lands without babysitting; Setup/Maintenance =
effort. "Manual" means a tap per phone→laptop transfer is required by Android 10+.

| Option | Reliability | Images | Both directions | Phone app | Server / account | Cost | Privacy / encryption | Setup | Wayland | Maintenance |
|---|---|---|---|---|---|---|---|---|---|---|
| KDE Connect / GSConnect | Medium | **No** | Text: yes; phone→laptop manual | KDE Connect (FOSS) | None (LAN) | Free | TLS, TOFU pairing | Low | **Best** (GSConnect in Shell) | Low |
| Syncthing + scripts | Low | Yes (as files) | Yes, via scripts | Syncthing-Fork | Discovery/relay, no account | Free | Device-to-device TLS | High | Poor (watch needs focus) | High |
| ntfy | Low–Medium | Attachments only, not auto-clipboard | Yes, with two topics | ntfy (FOSS) | Public or self-host | Free / ~$5 mo | TLS; E2E not confirmed | Medium | Poor | Low–Medium |
| Pushbullet / Join | Medium (text) | **No** | Yes (text) | Closed app | Account, cloud | ~$5/mo or one-off | E2E on Pushbullet | Low | No native client | Low |
| Tailscale + custom relay | **High** if built well | **Yes** | **Yes** | Custom or Termux | Tailscale acct + relay | Free (self-host) | WireGuard + optional E2E | High | Custom `wl-clipboard` | Medium |
| ClipCascade | Medium–High | **Yes** | **Yes** (manual or ADB workaround) | ClipCascade APK | Self-host Docker/JAR | Free (GPL) | Optional client-side E2E | Medium | Native Wayland + polling fallback | Medium |
| SyncClipboard | Medium–High | **Yes** | **Yes** (manual / IME plugin) | Several Android clients | Self-host / WebDAV / S3 | Free | TLS; no built-in E2E | Medium | `wl-clipboard` + input perms | Medium |
| Termux + Tasker / HTTP Shortcuts | Low–Medium | Tasker yes, Termux text-only | Manual | Tasker ($), Termux, HTTP Shortcuts | None | ~$3.49 Tasker | Depends on SSH/TLS | High | `wl-clipboard` | High |
| Firebase Firestore/RTDB | Medium–High | Via Storage; 1 MiB doc cap | Yes | Custom | Google account | Free tier / Blaze | TLS; Google sees data unless client E2E | High | `wl-clipboard` | Medium |
| Supabase | Medium–High | Yes (Storage; 50 MB objects) | Yes | Custom | Supabase account or self-host | Free tier / paid | TLS + RLS; server sees data unless client E2E | High | `wl-clipboard` | Medium |
| Plain WebSocket relay | High if built | Yes | Yes | Custom | Self-host | Free | `wss` + auth + optional E2E | High | `wl-clipboard` | Medium |

Two rows deserve emphasis: **GSConnect** is the least-effort Wayland-native text
option, and **ClipCascade** is the least-effort option that actually does images.
Everything that solves both directions and images without a third-party cloud requires
either running a server (ClipCascade/SyncClipboard) or building a client (custom relay).


---

## 4. Recommendation, fallback, and privacy

## 4. Recommendation, fallback, and privacy

### 4.1 Recommendation — own the relay: Tailscale + a small self-hosted clipsync relay

Recommended architecture (the laptop side is prototyped in `clipsync/`):

- **Network:** laptop and phone join a private **Tailscale/WireGuard** tailnet
  (free tier covers this). No port forwarding, no public listener; the relay binds to
  the tailnet address.
- **Transport:** a small WebSocket (or HTTP long-poll) relay on the laptop, with
  per-device auth tokens. The prototype's `Transport` interface is the seam: tests use
  `InMemoryTransport` and `FileTransport`, production plugs in a WebSocket adapter.
- **Message format:** `ClipMessage{origin, seq, payload, content_hash, msg_id}`, with
  payload `kind=text|image`, MIME, and a size cap. Content-hash echo prevention,
  per-origin sequence ordering, msg-id dedup, bounded outbox + replay on reconnect —
  all implemented and tested in `clipsync/engine.py`.
- **Images:** cap at 8 MiB in the protocol, but on Android keep the *clipboard* entry
  under the ~1 MB Binder ceiling by downscaling/re-encoding or by putting a
  FileProvider **content URI** on the clipboard. Transfer the full image over the
  relay; put a URI on the clip.
- **Crypto:** optional Fernet (AES-CBC + HMAC) frame encryption with a key derived by
  scrypt from a shared passphrase + random salt. The relay then sees ciphertext.
- **Phone→laptop:** be honest — on stock Android 10+ the client cannot silently read
  the clipboard. Expose a **notification action / QS tile / share target** that uploads
  the current clip (one tap), and optionally add a later IME-based mode for fully
  automatic capture. Laptop→phone can be automatic (write the clip on receive).
- **Why this one:** it is the only design here that delivers **text + images, both
  directions, no third-party cloud, E2E, and full control of the Android build** — and
  the laptop half is already implemented and tested in this repo. The cost is real:
  you must write and maintain the small Android foreground-service client.

### 4.2 Fallback — ClipCascade (turnkey, same architecture)

If you do not want to own an Android client, **ClipCascade** is the fallback and the
honest "works this weekend" choice: self-hosted Docker/JAR, text/images/files,
optional client-side E2E, native-Wayland Linux client, and an Android APK. Two
caveats, both verified in its README: the default message cap is **1 MiB** (raise
`CC_MAX_MESSAGE_SIZE_IN_MiB` or use P2P mode for larger images), and fully automatic
phone→laptop needs the ADB `READ_LOGS` + overlay workaround; otherwise you use its
manual actions. Its default admin password must be changed before exposing it.
[VERIFIED] [ClipCascade](https://github.com/Sathvik-Rao/ClipCascade)

If you only need **text** and want zero servers, use **KDE Connect/GSConnect**; it is
the lowest-effort option and the most Wayland-native, but it does not sync images and
phone→laptop is manual on Android 10+.

If you want images *and* seamless phone→laptop without ADB, **SyncClipboard +
Fcitx5-SyncClipboard** is the interesting niche: the IME plugin rides Android's IME
exemption, at the cost of using that keyboard. [VERIFIED] [SyncClipboard](https://github.com/Jeric-X/SyncClipboard)

### 4.3 Privacy and threat note

- Clipboard content routinely contains passwords, 2FA codes, addresses and private
  messages. Treat any sync path as handling secrets.
- **E2E is the difference between "the relay can't read it" and "trust the operator".**
  With plaintext relays (ntfy public topics, `ws://`, a cloud DB) the server, its
  backups, and anyone with the token can read clips. Use the Fernet layer and a
  passphrase that is not stored with the salt.
- **Metadata still leaks:** timing, sizes, and device identifiers tell an observer when
  you copy and roughly how much. Tailscale hides the path, not the pattern.
- **Endpoints are the weakest link:** malware or a malicious app on either device can
  read the clipboard. Android shows a preview and auto-clears after ~1 hour; the sync
  tool should honor `EXTRA_IS_SENSITIVE` and skip sensitive clips.
- **Retention:** the relay should keep clips in memory only, never on disk, and the
  local-file transport should be a test tool with `0600` permissions, not production.
- **Key handling:** generate the salt randomly, distribute the passphrase out of band,
  rotate on device loss, and never commit keys to git.
- **No real credentials or cloud projects were created in this run;** anything needing
  an account, phone, or system change is listed in `docs/orchestrator/NEEDS_HUMAN.md`.

### 4.4 What is not verified

- Android 14/15/16 clipboard specifics beyond the documented Android 10/12/13 changes:
  no primary source found for new read/write relaxations; the "nothing relaxed" claim
  is **[ASSUMED]**.
- Mutter lacking `wlr-data-control`: **[ASSUMED]** from indirect bug reports; worth a
  30-second check on the target machine (`wl-paste --watch` while another window is
  focused).
- Background `setPrimaryClip` on stock Android 15/16, OEM battery killers, and the
  real-world reliability of every phone-side flow: **UNTESTED** here; needs the phone.


---

## Sources

1. Android 10 privacy changes — https://developer.android.com/about/versions/10/privacy/changes
2. Android 12 clipboard toast — https://9to5google.com/2021/05/18/android-12-beta-1-clipboard-notifications-are-coming-and-toasts-now-show-their-source/
3. Android 13 clipboard preview/auto-clear — https://9to5google.com/2022/08/16/android-13-clipboard/
4. Android 13 DP auto-clear option (alternate view) — https://www.xda-developers.com/android-13-clipboard-auto-clear/
5. Android secure clipboard handling — https://developer.android.com/topic/security/risks/secure-clipboard-handling
6. Android 14 foreground service types — https://developer.android.com/about/versions/14/changes/fgs-types-manifest
7. Binder transaction limit — https://developer.android.com/reference/android/os/TransactionTooLargeException
8. Clipboard + TransactionTooLargeException — https://stackoverflow.com/questions/61146485/clipboard-transactiontoolargeexception-data-parcel-size
9. KDE Connect UserBase (clipboard plugin, Android 10+, ports) — https://userbase.kde.org/KDEConnect
10. wl-clipboard — https://github.com/bugaevc/wl-clipboard
11. Wayland clipboard focus limitation — https://clipmer.app/blog/best-clipboard-manager-wayland
12. Wayland focus/stale paste — https://superuser.com/questions/1189467/how-to-copy-text-to-the-clipboard-when-using-wayland
13. GNOME/wlroots protocol gap — https://github.com/Sirulex/cursor-clip/issues/5
