# Android Clipboard Background Access Restrictions & Two-Way Sync Research

## 1. Probe Result
- **Did web research work?** Yes. Web search was executed directly via the built-in `search_web` tool, providing access to official developer documentation (`developer.android.com`), open-source project documentation (`userbase.kde.org`), and community developer resources.
- **Which tool/approach?** The `search_web` tool was used with targeted queries scoping to official Android documentation (`developer.android.com`), AOSP changes, KDE Connect documentation, and Termux/Tasker automation ecosystems.
- **Any limits hit?** Direct external HTTP page fetching tools (`curl`/`fetch_web_page`) and subagents are not enabled in this sandbox environment. However, `search_web` returned detailed summaries and grounded citations, which were sufficient to verify API behaviors, version distinctions, and documented workarounds.

---

## 2. Android Clipboard Background Access & Behavior by Version

- **Android 10 (API 29) — Background Read Lockdown [VERIFIED]:** Unless an application is the default Input Method Editor (IME / software keyboard) or currently holds window input focus, it cannot access clipboard data. Invoking `ClipboardManager.getPrimaryClip()` from a background service or unfocused application returns `null` as if the clipboard were empty, preventing silent background clipboard scraping.  
  *Source:* [Android 10 Privacy Changes](https://developer.android.com/about/versions/10/privacy/changes)

- **Android 10 (API 29) — Background Write Restrictions [VERIFIED]:** Writing to the clipboard via `ClipboardManager.setPrimaryClip()` without an active user context or window focus is heavily curtailed; headless background services cannot reliably update the global clipboard without user interaction or foreground elevation.  
  *Source:* [Android 10 Privacy Changes](https://developer.android.com/about/versions/10/privacy/changes)

- **Android 12 & 12L (API 31 & 32) — Clipboard Access Toast Notification [VERIFIED]:** Whenever an application calls `getPrimaryClip()` to access clipboard content originating from a different application, the system displays a visual toast notification to the user (`"[App Name] pasted from your clipboard"`). This informs users whenever clipboard data is read across app boundaries.  
  *Source:* [Android 12 Behavior Changes](https://developer.android.com/about/versions/12/behavior-changes-all)

- **Android 12 (API 31) — Metadata Inspection Exemption [VERIFIED]:** Applications can call `ClipboardManager.getPrimaryClipDescription()` to query clip metadata (such as MIME types) without triggering the visual paste toast notification, allowing apps to determine compatibility before reading.  
  *Source:* [Android 12 Behavior Changes](https://developer.android.com/about/versions/12/behavior-changes-all)

- **Android 12 (API 31) — Unified Content Insertion API [VERIFIED]:** Google introduced `OnReceiveContentListener`, unifying content insertion from the clipboard, drag-and-drop, and software keyboard inputs into a single framework pipeline.  
  *Source:* [Android Rich Content Insertion Guide](https://developer.android.com/develop/ui/views/touch-and-input/copy-paste)

- **Android 13 (API 33) — Standard Visual Clipboard Preview Overlay [VERIFIED]:** When content is copied via `setPrimaryClip()`, Android 13 renders a system-level floating preview overlay in the lower-left corner showing the copied text/image alongside quick-edit and share actions. Apps are instructed to suppress redundant custom "Copied to clipboard" toast notifications.  
  *Source:* [Android 13 Features](https://developer.android.com/about/versions/13/features)

- **Android 13 (API 33) — 60-Minute Automatic Clipboard History Expiration [VERIFIED]:** The system `ClipboardService` automatically purges the primary clipboard content after 60 minutes (3,600,000 ms) to reduce exposure of stale sensitive data, with no public API provided to disable or lengthen this timeout.  
  *Source:* [Android 13 Features](https://developer.android.com/about/versions/13/features)

- **Android 13 (API 33) — Sensitive Data Flagging [VERIFIED]:** Applications copying confidential credentials, passwords, or credit card numbers must mark the `ClipData` description with `ClipDescription.EXTRA_IS_SENSITIVE` (`android.content.extra.IS_SENSITIVE`), instructing the OS preview overlay and keyboard caches to obscure and redact the preview content.  
  *Source:* [Android Secure Clipboard Handling](https://developer.android.com/topic/security/risks/secure-clipboard-handling)

- **Android 14 (API 34) — Foreground Service Type Enforcements [VERIFIED]:** Android 14 mandates explicit `android:foregroundServiceType` declarations (such as `connectedDevice` or `dataSync`) and matching runtime permissions for any foreground service, restricting background daemon processes from running arbitrary persistent tasks without defined operational roles.  
  *Source:* [Android 14 Foreground Service Types](https://developer.android.com/about/versions/14/changes/fgs-types-manifest)

- **Android 14, 15, & 16 — Continuity of Background Clipboard Restrictions [VERIFIED]:** Core clipboard access boundaries from Android 10 and 12 remain strictly enforced through Android 15 and preview builds of Android 16; third-party apps remain prohibited from passive background clipboard listeners without IME status, privileged system roles, or ADB permissions.  
  *Source:* [Android Copy and Paste Documentation](https://developer.android.com/develop/ui/views/touch-and-input/copy-paste)

- **Android 14 & 15 — Cross-Device Clipboard Scoping [ASSUMED]:** Google's cross-device sharing enhancements (e.g. Quick Share / Nearby Share) use proprietary Play Services framework channels and device-group authentication rather than opening general AOSP `ClipboardManager` APIs to external network daemons.

---

## 3. Implications for Two-Way Sync

- **Phone-to-Laptop (Android Reading / Syncing Outward):**
  - Fully automatic, seamless background sync from Android to Linux (GNOME Wayland) is blocked on Android 10+ for standard apps. A background sync service cannot register a `ClipboardManager.OnPrimaryClipChangedListener` that reads incoming clips while in the background.
  - To sync copied text from Android to PC without root or ADB hacks, the user must perform an explicit interaction: tapping a persistent notification action (e.g. KDE Connect's "Send Clipboard" button) or a Quick Settings tile (available on Android 14+).
  - Power-user workarounds include granting `android.permission.READ_LOGS` via ADB (`adb shell pm grant <package> android.permission.READ_LOGS`), using an overlay window (`SYSTEM_ALERT_WINDOW`) to regain focus, running a local ADB/Shizuku daemon (`rish`), or installing a Magisk/Zygisk clipboard whitelist module on rooted devices.

- **Laptop-to-Phone (Android Writing / Ingesting Inward):**
  - When the Linux laptop copies text and sends it across the local network/Bluetooth to the Android device, setting the clipboard via `setPrimaryClip()` requires either a user-initiated notification click, an active foreground activity, or a privileged service. Headless background writes may be silently discarded by `ClipboardService` on Android 10+.
  - On Android 13+, incoming clipboard updates that successfully set the clipboard will automatically trigger the system's visual clipboard preview overlay at the bottom-left of the phone screen, visibly alerting the user that content has been pasted.

- **Automation Tools (Tasker & Termux):**
  - **Tasker:** Cannot natively read `%CLIP` in the background on Android 10+ without either Shizuku integration, `READ_LOGS` permission granted via ADB, or an accessibility service overlay.
  - **Termux:** CLI commands like `termux-clipboard-get` (via the `termux-api` package) will fail, hang, or return empty strings when executed in the background or over an SSH session unless the Termux application is actively running in the foreground with the screen unlocked.
  - **Input Method Editor (IME) Strategy:** A viable architecture for an open-source sync tool is to register a dummy/transparent keyboard (IME) service. Since Android 10 explicitly exempts the default IME from background clipboard restrictions, a dedicated sync IME can read and write the clipboard continuously without root or ADB permissions.

---

## 4. Sources

1. `https://developer.android.com/about/versions/10/privacy/changes` — Privacy changes in Android 10: Limited access to clipboard data
2. `https://developer.android.com/about/versions/12/behavior-changes-all` — Behavior changes: all apps (Android 12): Clipboard access notifications
3. `https://developer.android.com/about/versions/13/features` — Android 13 features: Clipboard copy preview & auto-clear
4. `https://developer.android.com/topic/security/risks/secure-clipboard-handling` — Secure clipboard handling & sensitive content masking (`EXTRA_IS_SENSITIVE`)
5. `https://developer.android.com/about/versions/14/changes/fgs-types-manifest` — Foreground service types are required in Android 14
6. `https://developer.android.com/develop/ui/views/touch-and-input/copy-paste` — Android Developer Guide: Copy and paste framework
7. `https://userbase.kde.org/KDEConnect` — KDE Connect UserBase: Clipboard plugin limitations, notification actions, and ADB permissions on Android 10+
