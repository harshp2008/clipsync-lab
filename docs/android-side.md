# Android side (UNTESTED)

> [!WARNING]
> **UNTESTED HARDWARE NOTICE:** No physical Android phone or active mobile device was used, attached, or tested during this run. All setup procedures, ADB commands, configuration parameters, and mobile workflows documented below are **UNTESTED** on hardware and derived strictly from official upstream documentation and project READMEs:
> - Android OS clipboard behaviors: [Android Developer Documentation](https://developer.android.com/about/versions/10/privacy/changes)
> - Tailscale connectivity: [Tailscale Documentation](https://tailscale.com/kb/)
> - ClipCascade server and client configuration: [ClipCascade README](https://github.com/Sathvik-Rao/ClipCascade)
> - KDE Connect & GSConnect: [KDE Connect UserBase](https://userbase.kde.org/KDEConnect) and [GSConnect Wiki](https://github.com/GSConnect/gnome-shell-extension-gsconnect/wiki)
> - Termux clipboard tooling: [Termux:API Documentation](https://github.com/termux/termux-api)
>
> Do not assume these steps run without modification on specific OEM Android builds (MIUI, OneUI, ColorOS, etc.), which frequently enforce aggressive background process limits.

---

### Scope
This setup guide is written for a human operator pairing an **Ubuntu GNOME Wayland laptop** with an **Android smartphone**. It details three concrete deployment paths evaluated in [docs/research/clipboard-sync.md](file:///docs/research/clipboard-sync.md#L250-L302):
1. **Path A — Own Relay (Recommended Target):** Private Tailscale mesh network with a custom WebSocket relay built on the `clipsync/` engine.
2. **Path B — ClipCascade (Turnkey Fallback):** Self-hosted Docker server with official mobile APK supporting text and images today.
3. **Path C — KDE Connect / GSConnect (Zero-Server Fallback):** Direct LAN synchronization for text-only workflows.

### Read this first: The Android 10+ Background Read Boundary
Starting in Android 10 (API 29), background applications are strictly prohibited from reading the system clipboard (`getPrimaryClip()` returns `null`), unless the app is the currently focused foreground window or the default Input Method Editor (IME / software keyboard). A standard background service or persistent foreground notification does **not** grant clipboard read access.

Consequently, across all unrooted, standard implementations:
- **Laptop → Phone (Ingestion):** Automatic. The phone service receives network frames and writes directly to the clipboard via `setPrimaryClip()`.
- **Phone → Laptop (Outward Sync):** Requires a manual trigger (a single tap on a notification action button, Quick Settings tile, or Android Share Sheet), unless a privileged workaround (ADB `READ_LOGS`, an IME, or root) is explicitly configured. For full API citations and architecture analysis, see [docs/research/clipboard-sync.md §1.1](file:///docs/research/clipboard-sync.md#L19-L59).

---

## Path A — Own relay (recommended, laptop side in this repo) [UNTESTED]

The core data structures, dedup logic, content hashing, and encryption engine for this path are already implemented and tested in the laptop library [`clipsync/`](file:///docs/design/interfaces.md). However, **the WebSocket network transport adapter and the native Android client application were NOT built during this run**. Path A currently serves as an architectural blueprint for client development, accompanied by an immediate Termux-based text-only workaround.

### Step 1: Install Tailscale and Join Tailnet [UNTESTED]
1. Install Tailscale on the Ubuntu laptop:
   ```bash
   # [FROM UPSTREAM DOCS, UNTESTED]
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
2. Install the **Tailscale** application on your Android phone via Google Play Store or F-Droid.
3. Sign into both devices using the same identity provider.
4. Verify the laptop's assigned Tailscale IP address:
   ```bash
   # [FROM UPSTREAM DOCS, UNTESTED]
   tailscale ip -4
   # Example output: 100.x.y.z
   ```
   Note this address as `<tailnet-ip>`.

### Step 2: Intended Laptop Relay Configuration [UNTESTED]
The custom relay daemon runs on the laptop and binds strictly to the private Tailscale interface (`<tailnet-ip>`), preventing exposure to public networks. 

*(Note: A standalone production relay binary is not yet compiled. In `clipsync/transport.py`, `InMemoryTransport` and `FileTransport` exist; an `asyncio` WebSocket transport adapter implementing `Transport` is the missing component).*

Intended invocation shape:
```bash
# Intended server execution shape [UNTESTED]
python3 -m clipsync.relay \
  --bind "<tailnet-ip>" \
  --port 8765 \
  --auth-token "<pick-a-random-auth-token>" \
  --max-image-bytes 8388608
```

### Step 3: Android Client Requirements Checklist [UNTESTED]
When developing or configuring the native Android companion app, the client must implement the following behaviors:
- [ ] **Foreground Service:** Run a persistent `dataSync` or `connectedDevice` foreground service with an ongoing notification to prevent Android Doze mode termination.
- [ ] **Manual Outward Trigger:** Provide a "Send Clipboard" action button on the persistent notification, a Quick Settings (QS) Tile, or an Android Share Sheet target to satisfy the Android 10+ background read restriction.
- [ ] **Automatic Ingestion:** Automatically parse incoming frames from the WebSocket stream and write them to `ClipboardManager.setPrimaryClip()`.
- [ ] **Binder Limit Handling:** Android limits inter-process Binder transactions to ~1 MB total shared memory across the process. The client MUST NOT place raw multi-megabyte image byte arrays directly into `ClipData`. It must either:
  1. Downscale/compress images to well under 500 KB before calling `setPrimaryClip()`, OR
  2. Write incoming image bytes to local app cache and share via an Android `FileProvider` `content://` URI with transient read permissions (`FLAG_GRANT_READ_URI_PERMISSION`).
- [ ] **Exponential Backoff Reconnect:** Implement reconnect loops (e.g. 1s, 2s, 5s, max 30s) when switching between cellular and Wi-Fi networks.
- [ ] **Battery Optimisation Exemption:** Explicitly prompt the user to disable battery optimizations (`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`) and allow unrestricted background battery usage.
- [ ] **Keystore Protection:** Store the derived encryption key and relay auth token securely inside the hardware-backed `AndroidKeyStore`.

### Step 4: Pairing and Encryption Setup [UNTESTED]
All frames can be end-to-end encrypted using Fernet (AES-128-CBC + HMAC-SHA256) with a key derived via `scrypt` (`clipsync/crypto.py`).

1. Generate a 16-byte random salt and select a strong passphrase out of band on the laptop:
   ```bash
   python3 -c 'import secrets; print("Salt (hex):", secrets.token_hex(16))'
   ```
2. Derive the key using the parameters specified in `clipsync/crypto.py` (`n=16384, r=8, p=1, dklen=32`):
   ```python
   # Key derivation shape (do not commit real outputs)
   from clipsync.crypto import FernetCodec, derive_key

   passphrase = "<pick-a-long-passphrase>"
   salt = bytes.fromhex("<salt-hex-from-step-1>")
   derived_key = derive_key(passphrase, salt)
   codec = FernetCodec(derived_key)
   # derived_key is a urlsafe-base64 encoded string: b'<base64-key>...'
   ```
3. Enter `<pick-a-long-passphrase>` and `<salt-hex>` into both the laptop relay configuration and the Android client settings. Never commit these secrets or transmit them unencrypted.

### Step 5: Text-Only Workaround Today via Termux [UNTESTED]
If you need immediate text sync before the custom native client is written, use Termux and Termux:API:
1. Install **Termux** and **Termux:API** from F-Droid (do not use the abandoned Google Play version).
2. Inside Termux, install the API package:
   ```bash
   # [FROM UPSTREAM DOCS, UNTESTED]
   pkg update && pkg install termux-api curl
   ```
3. Read the phone clipboard:
   ```bash
   # [FROM UPSTREAM DOCS, UNTESTED]
   termux-clipboard-get
   ```
   > [!IMPORTANT]
   > On Android 10+, `termux-clipboard-get` returns an empty string if executed while Termux is in the background. Termux must be focused on screen, or triggered via Termux:Widget / Quick Settings / Share Sheet.
4. Set the phone clipboard from incoming data:
   ```bash
   # [FROM UPSTREAM DOCS, UNTESTED]
   echo "Text from laptop" | termux-clipboard-set
   ```

### Step 6: Acceptance Checklist for Path A Implementation [UNTESTED]
Once the client component is built, verify the pipeline against these gates:
- [ ] **Text Laptop → Phone:** Copy text on laptop; phone clipboard updates silently; Android 13+ preview toast confirms paste.
- [ ] **Text Phone → Laptop:** Copy text on phone; tap "Send Clipboard" QS tile or notification button; laptop clipboard reflects content.
- [ ] **Image Laptop → Phone:** Copy PNG/JPEG on laptop; arrives on phone and pastes into messaging apps without Binder `TransactionTooLargeException`.
- [ ] **Image Phone → Laptop:** Share/send image on phone; laptop clipboard receives `image/png` or `image/jpeg` verifiable with `wl-paste --type image/png`.
- [ ] **Airplane Mode / Reconnect:** Enable Airplane mode on phone for 30s, disable it; client reconnects automatically and flushes queued outbox clips without manual app restart.
- [ ] **Oversized Image Rejection:** Attempt sending a >8 MiB image; rejected cleanly with `PayloadTooLarge` rather than crashing the client daemon.
- [ ] **No Echo Loops:** Verify that syncing an item from laptop to phone does not immediately trigger the phone to send the same clip back to the laptop (engine matches `content_hash`).

---

## Path B — ClipCascade fallback (works today, turnkey) [UNTESTED]

ClipCascade is a mature, self-hosted cross-platform clipboard synchronizer that supports text, images, and files out of the box with optional end-to-end encryption.

### Step 1: Run the Server [FROM UPSTREAM DOCS, UNTESTED]
Run the ClipCascade Docker container on the laptop or home server. Set `CC_MAX_MESSAGE_SIZE_IN_MiB=8` (or larger) to allow standard image transfers (the default is only 1 MiB):

```bash
# [FROM UPSTREAM DOCS, UNTESTED]
docker run -d \
  --name clipcascade \
  --restart unless-stopped \
  -p 8080:8080 \
  -e CC_ADMIN_PASSWORD="<pick-a-strong-admin-password>" \
  -e CC_MAX_MESSAGE_SIZE_IN_MiB=8 \
  sathvikrao/clipcascade:latest
```
> [!CAUTION]
> You must change the default password (`admin123`) immediately upon deployment using `CC_ADMIN_PASSWORD` or the web administration panel.

### Step 2: Install Android APK [UNTESTED]
1. Download the latest APK release (`app-release.apk`) directly from the upstream GitHub repository: `https://github.com/Sathvik-Rao/ClipCascade/releases`.
2. Sideload the APK onto your Android phone (allow "Install unknown apps" for your browser or file manager when prompted).

### Step 3: Connection and Encryption Configuration [UNTESTED]
1. Open the ClipCascade Android app.
2. Enter the server endpoint: `http://<server-ip>:8080` (or `http://<tailnet-ip>:8080` if operating over Tailscale).
3. Authenticate with username `admin` and password `<pick-a-strong-admin-password>`.
4. Enable **End-to-End Encryption (E2EE)** in the app settings:
   - Check "Enable Encryption".
   - Set the Encryption Passphrase: `<pick-a-long-passphrase>`.
   - Set Salt: `<salt>`.
   - Set Key Derivation Iterations: `<iterations-e.g.-100000>`.
   > [!IMPORTANT]
   > The salt, passphrase, and iteration count MUST match identically across both the laptop client and the phone app, or decryption will fail.

### Step 4: Optional ADB Workaround for Automatic Phone → Laptop Sync [FROM UPSTREAM DOCS, UNTESTED]
By default, sending clips from Android to the laptop requires tapping a notification button. To enable fully automatic background capture, ClipCascade uses an ADB logcat monitoring and system alert overlay workaround:

1. On the phone, enable **Developer Options** (tap "Build Number" 7 times in Settings > About Phone).
2. Enable **USB Debugging** and connect the phone to the laptop via USB cable.
3. On the laptop, run the three upstream commands:
   ```bash
   # [FROM UPSTREAM DOCS, UNTESTED]
   adb shell pm grant com.sathvikrao.clipcascade android.permission.READ_LOGS
   adb shell appops set com.sathvikrao.clipcascade SYSTEM_ALERT_WINDOW allow
   adb shell am force-stop com.sathvikrao.clipcascade
   ```
4. Disconnect USB and re-open the ClipCascade app.
5. When starting the service, accept the system log-monitoring prompt.
6. **Keep "Start on device restart" OFF** in the ClipCascade app settings when using this ADB workaround, as documented by upstream to avoid boot initialization race conditions.

### Step 5: Android Battery Exemption [UNTESTED]
1. Go to phone **Settings > Apps > ClipCascade > Battery**.
2. Set battery usage to **Unrestricted** (exempt from battery optimizations).
3. In app info, verify **Allow background activity** and **Autostart** (on OEM skins like Xiaomi/Samsung) are toggled ON.

### Step 6: Test Checklist & Default Cap Warning [UNTESTED]
- [ ] **Text Bidirectional:** Copy text on laptop → pastes on phone. Copy text on phone → receives on laptop.
- [ ] **Image Bidirectional:** Copy screenshot on laptop → available on phone clipboard.
- [ ] **Image Cap Verification:** Ensure image files between 1 MiB and 8 MiB sync properly. If images above 1 MiB fail with server rejection, check that `CC_MAX_MESSAGE_SIZE_IN_MiB` was correctly exported in Docker.
- [ ] **Network Switch:** Turn off phone Wi-Fi, verify reconnection over cellular or VPN.
- [ ] **Reboot Recovery:** Reboot phone; manually restart the ClipCascade service (if "Start on device restart" is kept off for ADB mode) and confirm operation resumes.

---

## Path C — KDE Connect / GSConnect (text only, zero server) [UNTESTED]

KDE Connect / GSConnect provides zero-server peer-to-peer synchronization over your local Wi-Fi network. **It does NOT support image clipboard synchronization.**

### Step 1: Install Software [UNTESTED]
- **Laptop (Ubuntu GNOME Wayland):** Install the **GSConnect** extension from GNOME Extensions website (`extensions.gnome.org`) or via APT:
  ```bash
  # [FROM UPSTREAM DOCS, UNTESTED]
  sudo apt install gnome-shell-extension-gsconnect
  ```
  *(Note: GSConnect runs natively inside GNOME Shell's compositor, avoiding Wayland background focus restrictions).*
- **Android Phone:** Install **KDE Connect** from F-Droid or Google Play Store.

### Step 2: Network and Firewall Configuration [UNTESTED]
Both devices must be on the same local subnet. KDE Connect utilizes UDP broadcast discovery and dynamic TCP/UDP ports **1714–1764**.
If `ufw` is active on Ubuntu:
```bash
# [FROM UPSTREAM DOCS, UNTESTED]
sudo ufw allow 1714:1764/udp
sudo ufw allow 1714:1764/tcp
sudo ufw reload
```

### Step 3: Pairing and Plugin Setup [UNTESTED]
1. Open KDE Connect on Android and GSConnect settings on the laptop.
2. Select your phone from GSConnect and click **Pair** (or request pairing from phone); accept the pairing prompt on both screens.
3. In GSConnect and phone settings, ensure the **Clipboard** plugin is checked.

### Step 4: Operating Phone → Laptop Sync on Android 10+ [UNTESTED]
- **Standard Operation:** Due to Android 10+ privacy policies, copying text on the phone does not automatically push to the laptop. After copying, swipe down your notification drawer and tap **"Send clipboard"** in the persistent KDE Connect notification. On Android 14+, you can add the "Send Clipboard" Quick Settings tile to your notification panel.
- **Optional ADB Workaround [FROM UPSTREAM DOCS, UNTESTED]:** To automate phone→laptop sync without tapping the notification, grant `READ_LOGS` over ADB:
  ```bash
  # [FROM UPSTREAM DOCS, UNTESTED]
  adb shell pm grant org.kde.kdeconnect_tp android.permission.READ_LOGS
  adb shell appops set org.kde.kdeconnect_tp SYSTEM_ALERT_WINDOW allow
  ```
  *(Note: Images remain strictly unsupported even with ADB permissions).*

---

## Troubleshooting [UNTESTED]

| Symptom | Likely Cause | Exact Diagnostic Check & Resolution |
| :--- | :--- | :--- |
| **Nothing arrives on phone or laptop** | Devices not paired, service dead, or firewall blocked. | Check network reachability (`ping <tailnet-ip>` or local IP). Verify ports 1714–1764 (KDE Connect) or 8080 (ClipCascade) are open. Ensure the Android foreground service is actively running with a notification visible. |
| **Images fail to sync, but text succeeds** | Protocol/app lacks image support, or image exceeds size ceiling. | **KDE Connect:** Images are completely unsupported by design.<br>**ClipCascade:** Check if image exceeds `CC_MAX_MESSAGE_SIZE_IN_MiB` (default is 1 MiB).<br>**Path A:** Check if Android hit Binder 1 MB `TransactionTooLargeException`; use content URI or downscale. |
| **Phone → Laptop transfer is completely silent in background** | Android 10+ background read restriction blocking passive capture. | Tap the manual "Send clipboard" button in notification or QS tile. If using ADB workaround, verify permissions with `adb shell dumpsys package <package-name> \| grep READ_LOGS`. |
| **Works on home Wi-Fi, fails on mobile data** | Local LAN dependency or CGNAT mobile routing. | Local LAN tools (KDE Connect) cannot bridge cellular data without port forwarding or VPN. Switch to Tailscale mesh (Path A or Path B over Tailscale) to bridge across subnets seamlessly. |
| **Duplicate / Infinite echo loops** | Missing content-hash deduplication or byte re-encoding mismatch. | Ensure the client updates its internal `_last_local_hash` upon applying remote clips. If the receiving app re-encodes images upon pasting, verbatim byte hashes may differ; rely on sequence IDs and origin tagging. |
| **Clipboard cleared after ~1 hour** | Android 13+ automatic clipboard history purge. | This is normal OS behavior. Android 13 automatically clears `ClipboardService` history after 60 minutes for privacy. No action needed; subsequent copies will work normally. |
| **Connection drops after phone reboot** | OEM battery killer or missing autostart permissions. | Exclude app from battery optimization (`Settings > Battery > Unrestricted`). In ClipCascade with ADB workaround, note that service must be manually re-launched if "Start on device restart" was disabled. |

---

## Privacy and Safety Checklist [UNTESTED]

- [ ] **Passphrase & Key Hygiene:** Never store pairing passphrases, derived Fernet keys, or salt values in version control repositories or unencrypted configuration files. Do not screenshot passphrases on mobile.
- [ ] **Tailscale ACLs:** Restrict access within your Tailscale admin console so that only your laptop and phone nodes can access the relay port, isolating it from other machines on your tailnet.
- [ ] **Android Sensitive Content Redaction:** Android 13+ visual clipboard previews display sensitive credentials on screen. Where possible, applications copying passwords should set `ClipDescription.EXTRA_IS_SENSITIVE`. Sync tools must inspect metadata via `getPrimaryClipDescription()` and skip synchronizing clips flagged sensitive.
- [ ] **Server Exposure without E2E:** If running ClipCascade or custom relays without end-to-end encryption, the relay server processes all plaintext, session tokens, passwords, and images passing through memory and logs. Always enable client-side E2EE.
- [ ] **Device Loss Procedure:** If your Android phone is lost or stolen, immediately revoke its auth token on the laptop relay, rotate the shared encryption passphrase, and regenerate pairing keys across all remaining devices.

---

## What is Still Missing [UNTESTED]

To achieve a production-grade, two-way sync for Path A (Own Relay), the following implementation work remains:
1. **WebSocket Transport Adapter (`clipsync/transport.py`):** An asynchronous WebSocket server/client adapter conforming to the existing `Transport` protocol contract.
2. **Native Android Foreground-Service Client:** An Android application (Kotlin) implementing a persistent foreground service, WebSocket client, Keystore integration, and notification/QS-tile triggers.
3. **Android Image Downscaler & FileProvider Helper:** A dedicated utility module on Android to downscale large incoming image bitmaps or expose them via `androidx.core.content.FileProvider` to respect the ~1 MB Android Binder ceiling.
4. **Real-Device Test Matrix:** Validation on physical devices across diverse OEM Android skins (Samsung OneUI, Xiaomi HyperOS/MIUI, Google Pixel stock Android 14/15) to measure battery drain, foreground service survival, and clipboard write reliability.
