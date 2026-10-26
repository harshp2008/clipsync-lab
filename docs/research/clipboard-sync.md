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

_Detailed per-option notes (verified/assumed, pricing, limits) are in
[`agy-options-landscape.md`](agy-options-landscape.md). Summary and comparison are
below._

<!-- SECTION_2_SUMMARY -->

---

## 3. Comparison

<!-- SECTION_3_TABLE -->

---

## 4. Recommendation, fallback, and privacy

<!-- SECTION_4_RECOMMENDATION -->

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
