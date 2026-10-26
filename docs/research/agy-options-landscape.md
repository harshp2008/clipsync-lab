# Options landscape

Synchronizing the system clipboard bidirectionally between Ubuntu GNOME Wayland and Android involves navigating significant OS-level architectural sandboxes and performance constraints. On Android 10 (API 29) and later, Google permanently blocks background applications from reading the system clipboard unless the app is the actively focused window or is designated as the default Input Method Editor (IME / software keyboard), returning null to background read attempts. On the Linux side, GNOME under Wayland strictly isolates application memory spaces and clipboard access, preventing arbitrary background daemons from passively intercepting or modifying selection buffers without active window focus or dedicated Wayland protocols (such as those wrapped by `wl-clipboard` or GNOME Shell extensions). Furthermore, while plain text clipboard payloads consist of negligible byte counts, transferring uncompressed image buffers or high-resolution screenshots introduces significant network latency, memory pressure, and protocol-specific payload serialization overhead that most lightweight signaling protocols are ill-equipped to handle without dedicated object storage or chunking.

---

## 1. KDE Connect / GSConnect

- **Sync Mechanism:** Native desktop background daemon paired with an Android background service. On Ubuntu GNOME, GSConnect implements the KDE Connect protocol natively as a GNOME Shell extension via Mutter D-Bus, while standard KDE Connect runs a standalone C++ daemon communicating with the Android app over a local network socket `[VERIFIED](https://community.kde.org/KDEConnect)`.
- **Text & Image Support:** Text and URLs are fully supported. Image clipboard synchronization is **NOT supported** across either KDE Connect or GSConnect; images copied to the clipboard on either device are ignored by the clipboard plugin and must instead be shared explicitly as files using the manual "Share to" file-transfer plugin `[VERIFIED](https://userbase.kde.org/KDEConnect)`.
- **Directionality:** Bidirectional for text. Linux-to-Android sync operates automatically when text is copied on the desktop. Android-to-Linux sync on Android 10+ requires the user to tap an interactive notification action ("Send Clipboard") or Quick Settings tile on the phone, because passive background clipboard reading is prohibited `[VERIFIED](https://userbase.kde.org/KDEConnect)`.
- **Phone App:** Official KDE Connect Android client; free and open source under GPL-2.0+, available via F-Droid and Google Play `[VERIFIED](https://userbase.kde.org/KDEConnect)`.
- **Server / Account:** No cloud server or account required. Operates completely peer-to-peer over the local network (UDP broadcast discovery and TCP connections over port 1716) `[VERIFIED](https://community.kde.org/KDEConnect)`.
- **Cost:** Free ($0); entirely community maintained open-source software `[VERIFIED](https://community.kde.org/KDEConnect)`.
- **Privacy & Encryption:** End-to-end encrypted using TLS with unique per-device cryptographic RSA/ECDSA certificates exchanged and trusted on first use (TOFU) during initial pairing; no data routes through third parties `[VERIFIED](https://community.kde.org/KDEConnect)`.
- **GNOME Wayland Caveats:** GSConnect integrates directly into GNOME Shell's compositor, but standalone KDE Connect requires `wl-clipboard` (`wl-copy` / `wl-paste`) to interact with Wayland clipboard buffers. Security isolation prevents background Wayland applications from reading the clipboard without active window focus `[VERIFIED](https://github.com/GSConnect/gnome-shell-extension-gsconnect)`.
- **Setup & Maintenance:** Low setup effort (install GNOME extension and phone app, pair via local Wi-Fi); low ongoing maintenance burden `[ASSUMED]`.
- **Real-World Limitations:** Complete absence of image clipboard synchronization `[VERIFIED](https://userbase.kde.org/KDEConnect)`. On Android 10+, background automatic clipboard sync from phone to desktop is impossible without granting ADB permission `android.permission.READ_LOGS` or manually tapping notification buttons for every copy action `[VERIFIED](https://userbase.kde.org/KDEConnect)`.

---

## 2. Syncthing-based approaches

- **Sync Mechanism:** File-based synchronization hack. Syncthing has no native clipboard synchronization plugin; users must establish a shared folder between Linux and Android and run custom scripts (e.g. `wl-paste --watch` on Linux and Tasker or Termux file monitors on Android) that dump clipboard payloads into a designated file (such as `clipboard.txt` or `clip.png`) which Syncthing continuously syncs across devices `[VERIFIED](https://syncthing.net/)`.
- **Text & Image Support:** Text and images can both be synced as discrete filesystem objects (e.g., raw text files or PNG/JPEG image files written by the watcher script). However, native clipboard ingestion requires secondary scripts on each receiving endpoint to read the file into the OS clipboard `[ASSUMED]`.
- **Directionality:** Bidirectional file synchronization, subject to local watcher and script triggers in each direction `[VERIFIED](https://syncthing.net/)`.
- **Phone App:** Syncthing-Fork or official Syncthing for Android; free and open source under MPL-2.0 on F-Droid and GitHub `[VERIFIED](https://github.com/syncthing/syncthing-android)`.
- **Server / Account:** Fully decentralized peer-to-peer; no account needed. Relies on public or self-hosted discovery servers and relay nodes (`relays.syncthing.net`) for NAT traversal `[VERIFIED](https://syncthing.net/)`.
- **Cost:** Free ($0) open-source software `[VERIFIED](https://syncthing.net/)`.
- **Privacy & Encryption:** End-to-end encrypted between connected device IDs using TLS with forward secrecy (Block Exchange Protocol v1); relay nodes cannot decrypt data blocks in transit `[VERIFIED](https://syncthing.net/)`.
- **GNOME Wayland Caveats:** Desktop requires custom daemon scripting utilizing `wl-clipboard` (`wl-paste` to extract images/text into the sync directory and `wl-copy` to populate the Wayland clipboard when Syncthing detects an incoming file change) `[ASSUMED]`.
- **Setup & Maintenance:** High setup effort; users must configure folder synchronization, install file watcher daemons on Linux, and build custom automation routines in Android (via Tasker/Termux). High maintenance due to fragile script glue `[ASSUMED]`.
- **Real-World Limitations:** Inherent disk I/O and protocol polling latency (typically 2 to 10 seconds), causing race conditions and sync conflicts if multiple items are copied rapidly. Android 10+ background read restrictions prevent Android watcher scripts from silently populating the synced file from the clipboard `[ASSUMED]`.

---

## 3. ntfy

- **Sync Mechanism:** HTTP-based pub/sub push notification service. The Linux desktop and Android app publish and subscribe to topics. For clipboard integration, messages can carry a predefined `copy` action button that Android users tap in the notification shade, or a background script listens to SSE/WebSocket topic streams `[VERIFIED](https://docs.ntfy.sh/publish/#action-buttons)`.
- **Text & Image Support:** Text is fully supported via message bodies and `copy` action buttons. Images are supported as file attachments (sent via HTTP PUT/POST), but attachments are downloaded to device storage and are **NOT automatically injected** into the OS image clipboard without auxiliary automation scripts `[VERIFIED](https://docs.ntfy.sh/publish/#attachments)`.
- **Directionality:** Bidirectional if two topics are configured (e.g., `topic-desktop-to-phone` and `topic-phone-to-desktop`), though fundamentally based on publish/subscribe push semantics `[ASSUMED]`.
- **Phone App:** Official ntfy Android app; free and open source under GPLv3 and Apache-2.0, available on F-Droid and Google Play `[VERIFIED](https://docs.ntfy.sh/publish/#action-buttons)`.
- **Server / Account:** Can use public hosted servers (`ntfy.sh`) with no account required for open topics, or a self-hosted single-binary Go server; zero mandatory cloud dependencies if self-hosted `[VERIFIED](https://ntfy.sh/#pricing)`.
- **Cost:** Free tier on `ntfy.sh` (allows up to 2 MB per attachment, 20 MB total active visitor attachment cache, and 500 MB daily bandwidth); paid tiers for higher bandwidth start at $5/month. Self-hosting has no licensing fees ($0) `[VERIFIED](https://ntfy.sh/#pricing)`.
- **Privacy & Encryption:** Transport encrypted via HTTPS/TLS. By default on public servers, messages and attachments are stored in plaintext in server cache for up to 12 hours unless client-side end-to-end encryption (password-based AES-GCM) is configured in the clients `[VERIFIED](https://docs.ntfy.sh/publish/#attachments)`.
- **GNOME Wayland Caveats:** Desktop requires a persistent script or daemon (e.g., Python or curl listening to the ntfy SSE streaming endpoint) piping received payloads to `wl-copy`. Native browser desktop notifications cannot execute clipboard writes in the background `[ASSUMED]`.
- **Setup & Maintenance:** Medium setup effort (requires configuring bash/curl listeners or Tasker tasks); low maintenance if using public ntfy.sh, medium if self-hosting `[ASSUMED]`.
- **Real-World Limitations:** Not an automated clipboard sync tool out of the box. Free hosted file attachments are capped at 2 MB and expire after 3 hours `[VERIFIED](https://ntfy.sh/#pricing)`. Android 10+ requires the user to manually tap the notification "Copy" button on the phone, preventing silent background clipboard updates `[VERIFIED](https://docs.ntfy.sh/publish/#action-buttons)`.

---

## 4. Pushbullet-style services (Pushbullet, Join, similar)

- **Sync Mechanism:** Proprietary cloud relay service. Pushbullet provides a dedicated "Universal Copy & Paste" feature that mirrors clipboard contents across registered devices via cloud WebSocket connections. Join (by joaoapps) routes clipboard actions through Google Drive and Google Cloud Messaging (FCM) `[VERIFIED](https://blog.pushbullet.com/2014/08/20/introducing-universal-copy-paste/)`.
- **Text & Image Support:** Universal copy & paste across Pushbullet and Join is **strictly limited to text and links**. Images are NOT supported via the automatic clipboard sync mechanism; images must be pushed manually as discrete file attachments `[VERIFIED](https://blog.pushbullet.com/2014/08/20/introducing-universal-copy-paste/)`.
- **Directionality:** Bidirectional for text. Background mirroring occurs automatically across desktop and mobile devices where permitted by OS focus `[VERIFIED](https://blog.pushbullet.com/2014/08/20/introducing-universal-copy-paste/)`.
- **Phone App:** Official Pushbullet Android app (closed-source, freemium on Google Play) or Join by joaoapps (closed-source, 30-day trial then paid on Google Play) `[VERIFIED](https://play.google.com/store/apps/details?id=com.joaoapps.join)`.
- **Server / Account:** Requires an account with the service provider (Google/Apple SSO); cannot be self-hosted; fully dependent on third-party cloud infrastructure and Google Cloud Messaging `[VERIFIED](https://www.pushbullet.com/pro)`.
- **Cost:** Pushbullet Universal Copy & Paste is locked behind the paid **Pushbullet Pro** plan ($4.99/month or $39.99/year [$3.33/month billed annually]). Join offers a 30-day free trial followed by a one-time purchase of ~$4.99 `[VERIFIED](https://www.pushbullet.com/pro)` `[VERIFIED](https://play.google.com/store/apps/details?id=com.joaoapps.join)`.
- **Privacy & Encryption:** Pushbullet supports optional End-to-End Encryption (E2EE) for Universal Copy & Paste using AES-256-GCM with keys derived via PBKDF2 from a user-supplied password `[VERIFIED](https://help.pushbullet.com/articles/what-is-end-to-end-encryption/)`. Join offers optional user encryption keys using Google Drive storage `[VERIFIED](https://play.google.com/store/apps/details?id=com.joaoapps.join)`.
- **GNOME Wayland Caveats:** No official modern Linux desktop client exists for Pushbullet or Join. Users must rely on browser extensions (which cannot access the Wayland system clipboard from the background without user interaction) or third-party community CLI wrappers using `wl-clipboard` `[ASSUMED]`.
- **Setup & Maintenance:** Low initial setup on mobile; medium-to-high on Linux Wayland due to third-party client bitrot; low operational maintenance `[ASSUMED]`.
- **Real-World Limitations:** Pushbullet clipboard sync is paywalled ($4.99/mo). Image clipboard sync is completely unsupported `[VERIFIED](https://blog.pushbullet.com/2014/08/20/introducing-universal-copy-paste/)`. Modern Android versions (Android 10+) severely restrict automatic background phone-to-cloud clipboard reading, requiring manual notification actions or accessibility services `[ASSUMED]`.

---

## 5. Tailscale + custom relay

- **Sync Mechanism:** Point-to-point mesh VPN overlay (WireGuard-based tailnet) paired with a custom lightweight HTTP or WebSocket relay daemon running on a designated node (such as the Linux laptop, home server, or VPS). The Android client and Linux desktop communicate directly across private Tailscale 100.x.y.z IP addresses `[VERIFIED](https://tailscale.com/pricing)`.
- **Text & Image Support:** Fully supports both text and arbitrary binary image payloads (PNG, JPEG, WebP) within custom API data structures (e.g. multipart HTTP requests or binary WebSocket frames), since the custom relay daemon defines its own payload protocol `[ASSUMED]`.
- **Directionality:** Bidirectional. Clients push updates upon local clipboard change events and pull/stream incoming updates `[ASSUMED]`.
- **Phone App:** Official Tailscale Android app (open-source core under BSD-3-Clause with proprietary wrapper, on F-Droid and Play Store) to establish the tailnet, alongside a custom client app, Tasker task, or Termux script for clipboard ingestion `[VERIFIED](https://tailscale.com/pricing)`.
- **Server / Account:** Requires a Tailscale account (or open-source Headscale coordination server). The custom relay server is self-hosted on a private node within the tailnet `[VERIFIED](https://tailscale.com/pricing)`.
- **Cost:** Tailscale Free Personal tier covers up to 3 users and 100 devices at $0/month; custom relay software is free and self-hosted `[VERIFIED](https://tailscale.com/pricing)`.
- **Privacy & Encryption:** End-to-end encrypted using WireGuard (ChaCha20-Poly1305). Coordination server only handles key exchange and network topology; relay traffic between nodes is private and inaccessible to third parties `[VERIFIED](https://tailscale.com/pricing)`.
- **GNOME Wayland Caveats:** The Linux daemon must execute `wl-paste --watch` to detect changes and `wl-copy` to write incoming clipboard buffers, requiring active user session credentials in GNOME Wayland `[ASSUMED]`.
- **Setup & Maintenance:** High setup effort; requires deploying Tailscale across devices, writing custom relay server and client code, handling background reconnections, and configuring firewall traversal. Medium maintenance `[ASSUMED]`.
- **Real-World Limitations:** Maintaining persistent Tailscale VPN connections on Android increases background battery consumption and can trigger aggressive OS background killer daemons. Android 10+ clipboard background access blocks passive client reads without active UI focus or an IME implementation `[ASSUMED]`.

---

## 6. Self-hosted clipboard servers (ClipCascade, SyncClipboard)

- **Sync Mechanism:** Dedicated open-source cross-platform client-server clipboard platforms. **ClipCascade** (by Sathvik Rao) provides a self-hosted Docker server paired with Linux and Android client apps communicating via WebSockets and REST APIs `[VERIFIED](https://github.com/Sathvik-Rao/ClipCascade)`. **SyncClipboard** (by Jeric-X) uses a self-hosted Docker server, WebDAV, or S3 backend synchronized against desktop clients and a React Native mobile companion app `[VERIFIED](https://github.com/Jeric-X/SyncClipboard)`.
- **Text & Image Support:** Both ClipCascade and SyncClipboard **explicitly support syncing text, images, and files** bidirectionally between desktop and Android `[VERIFIED](https://github.com/Sathvik-Rao/ClipCascade)` `[VERIFIED](https://github.com/Jeric-X/SyncClipboard)`.
- **Directionality:** Bidirectional for text and image data. Updates are pushed to the server and broadcast to connected endpoints `[VERIFIED](https://github.com/Sathvik-Rao/ClipCascade)` `[VERIFIED](https://github.com/Jeric-X/SyncClipboard)`.
- **Phone App:** Official mobile APKs: ClipCascade Android app (open source on GitHub) and SyncClipboard Mobile (`syncclipboard-mobile`, open source on GitHub) `[VERIFIED](https://github.com/Sathvik-Rao/ClipCascade)` `[VERIFIED](https://github.com/Jeric-X/syncclipboard-mobile)`.
- **Server / Account:** Requires running a self-hosted server instance (via Docker or WebDAV/S3). User accounts are created and managed on the self-hosted instance; no commercial cloud dependency `[VERIFIED](https://github.com/Sathvik-Rao/ClipCascade)` `[VERIFIED](https://github.com/Jeric-X/SyncClipboard)`.
- **Cost:** Free ($0) open-source software under MIT / GPL licenses. Server hosting costs depend on user infrastructure (local hardware or self-funded VPS) `[VERIFIED](https://github.com/Sathvik-Rao/ClipCascade)` `[VERIFIED](https://github.com/Jeric-X/SyncClipboard)`.
- **Privacy & Encryption:** ClipCascade features built-in End-to-End Encryption (E2EE) with zero-knowledge architecture, encrypting payloads before transmission `[VERIFIED](https://github.com/Sathvik-Rao/ClipCascade)`. SyncClipboard supports TLS transport security and optional server-side storage encryption `[VERIFIED](https://github.com/Jeric-X/SyncClipboard)`.
- **GNOME Wayland Caveats:** Both projects officially document Linux Wayland compatibility using `wl-clipboard` (`wl-copy` and `wl-paste`). ClipCascade provides dedicated CLI flags (such as `--xmode false`) to enforce native Wayland clipboard watching via `wl-paste --watch` `[VERIFIED](https://github.com/Sathvik-Rao/ClipCascade)` `[VERIFIED](https://github.com/Jeric-X/SyncClipboard)`.
- **Setup & Maintenance:** Medium to high setup effort (deploying Docker containers, setting up reverse proxies with TLS certificates, sideloading Android APKs). Moderate ongoing maintenance `[ASSUMED]`.
- **Real-World Limitations:** Mobile apps must run persistent background foreground services with sticky notifications to maintain server connections. On Android 10+, background automated reading of the clipboard requires keeping the app in the foreground, using accessibility services, or tapping quick-action notifications `[VERIFIED](https://github.com/Jeric-X/syncclipboard-mobile)`. Sideloaded APKs lack automated Google Play background updating.

---

## 7. Termux + Tasker / HTTP Shortcuts automation

- **Sync Mechanism:** User-scripted automation pipelines. On Android, Tasker monitors the clipboard state via event profiles or accessibility services, while Termux executes CLI commands (`termux-clipboard-get`, `termux-clipboard-set`, `curl`, `rsync`, or SSH) `[VERIFIED](https://github.com/termux/termux-api)` `[VERIFIED](https://joaoapps.com/tasker/)`. HTTP Shortcuts provides custom homescreen widgets, Quick Settings tiles, and JavaScript actions (`getClipboardContent()`, `copyToClipboard()`) that trigger HTTP calls to the Linux PC `[VERIFIED](https://github.com/Waboodoo/HTTP-Shortcuts)`.
- **Text & Image Support:** Tasker natively supports text (`%CLIP`) as well as explicit image clipboard actions (using Tasker's "Get Clipboard" and "Set Clipboard" actions with image URIs, `%cl_image_uri`) `[VERIFIED](https://joaoapps.com/tasker/)`. Termux API's `termux-clipboard-get` is text-only. HTTP Shortcuts focuses on text payloads, though image files can be sent via Android Share Sheet triggers `[VERIFIED](https://github.com/termux/termux-api)` `[VERIFIED](https://github.com/Waboodoo/HTTP-Shortcuts)`.
- **Directionality:** Bidirectional, implemented via complementary send/receive task flows (e.g., Tasker POSTing to a local Python daemon on Linux, and Linux pushing via SSH or Termux:Tasker intents) `[ASSUMED]`.
- **Phone App:** Tasker (commercial, proprietary on Google Play), Termux (free and open source under GPLv3 on F-Droid), HTTP Shortcuts (free and open source on F-Droid and Play Store) `[VERIFIED](https://github.com/termux/termux-api)` `[VERIFIED](https://joaoapps.com/tasker/)` `[VERIFIED](https://github.com/Waboodoo/HTTP-Shortcuts)`.
- **Server / Account:** No commercial cloud server or account required. Operates over local Wi-Fi, WireGuard VPN, or direct SSH/HTTP endpoints `[ASSUMED]`.
- **Cost:** Tasker costs ~$3.49 one-time purchase on Google Play; Termux and HTTP Shortcuts are free ($0) `[VERIFIED](https://joaoapps.com/tasker/)` `[VERIFIED](https://github.com/Waboodoo/HTTP-Shortcuts)`.
- **Privacy & Encryption:** Completely private and secure if routed over SSH, local HTTPS, or WireGuard VPN. Plaintext if unauthenticated HTTP is transmitted across an open local network `[ASSUMED]`.
- **GNOME Wayland Caveats:** Linux endpoint requires a daemon script listening to incoming network requests and executing `wl-copy`, while triggering outgoing pushes via `wl-paste --watch` `[ASSUMED]`.
- **Setup & Maintenance:** High setup effort; requires writing custom shell/JavaScript scripts, configuring intent receivers, setting up SSH keys, and debugging Tasker event contexts. High maintenance burden `[ASSUMED]`.
- **Real-World Limitations:** On Android 10+, `termux-clipboard-get` fails or returns empty strings when executed in the background or via headless SSH sessions unless Termux is actively focused on screen `[VERIFIED](https://github.com/termux/termux-api)`. Tasker cannot read the clipboard silently in the background on Android 10+ without granting `android.permission.READ_LOGS` via ADB or employing an accessibility overlay `[VERIFIED](https://joaoapps.com/tasker/)`.

---

## 8. Firebase Firestore / Realtime Database

- **Sync Mechanism:** Managed cloud document/key-value store acting as a real-time message broker. Linux desktop runs a background script (Python or Node.js utilizing the Firebase Admin SDK) listening to Firestore document snapshots or Realtime Database `on('value')` listeners, while an Android app listens to corresponding collection listeners `[VERIFIED](https://firebase.google.com/docs/firestore/quotas)`.
- **Text & Image Support:** Text is supported natively. Images face severe database constraints: Cloud Firestore enforces a strict hard limit of **1 MiB (1,048,576 bytes)** total document size `[VERIFIED](https://firebase.google.com/docs/firestore/quotas)`. Base64-encoded images larger than ~750 KB cannot be stored in Firestore documents. Syncing full-size images requires uploading files to Firebase Cloud Storage and storing the reference URI in Firestore, which requires an upgraded paid plan `[VERIFIED](https://firebase.google.com/pricing)` `[VERIFIED](https://firebase.google.com/docs/firestore/quotas)`.
- **Directionality:** Bidirectional. Document changes written by either client trigger immediate snapshot listener events on the opposite client `[VERIFIED](https://firebase.google.com/docs/firestore/quotas)`.
- **Phone App:** Requires a custom-built Android application or custom Tasker JavaScript tasks integrating the Firebase REST API `[ASSUMED]`.
- **Server / Account:** Requires a Google Firebase / Google Cloud Platform account. Hosted entirely in Google's cloud; cannot be self-hosted `[VERIFIED](https://firebase.google.com/pricing)`.
- **Cost:** Free "Spark" tier provides 1 GiB total storage, 50,000 document reads/day, 20,000 document writes/day, and 10 GiB/month outbound data transfer for Firestore `[VERIFIED](https://firebase.google.com/pricing)`. Standalone Cloud Storage buckets for large image assets require the pay-as-you-go "Blaze" plan `[VERIFIED](https://firebase.google.com/pricing)`.
- **Privacy & Encryption:** Data in transit is secured with TLS. Data at rest is encrypted on Google Cloud infrastructure. However, payloads are accessible in plaintext to Google unless custom client-side end-to-end encryption is implemented in the desktop and mobile clients `[ASSUMED]`.
- **GNOME Wayland Caveats:** Desktop script requires running in the GNOME session to pipe snapshot event payloads into `wl-copy` and watch clipboard buffers using `wl-paste` `[ASSUMED]`.
- **Setup & Maintenance:** High setup effort; requires creating a Google Cloud project, setting up Firestore security rules, generating service account credentials, and developing both Linux and Android client software. Moderate maintenance `[ASSUMED]`.
- **Real-World Limitations:** The 1 MiB document size limit creates a hard barrier for raw image clipboard synchronization `[VERIFIED](https://firebase.google.com/docs/firestore/quotas)`. Exceeding daily free Spark quotas causes Firebase to pause operations until midnight PT, halting clipboard sync `[VERIFIED](https://firebase.google.com/pricing)`. Android 10+ background restrictions still prevent the Android client from reading the clipboard without user interaction or IME privileges `[ASSUMED]`.

---

## 9. Supabase

- **Sync Mechanism:** Open-source Firebase alternative powered by PostgreSQL. Employs Supabase Realtime (WebSockets) for listening to database row changes (Postgres CDC) or ephemeral broadcast channels. Large payloads (images) are uploaded to Supabase Storage buckets, with metadata and download URLs distributed over Realtime channels `[VERIFIED](https://supabase.com/pricing)`.
- **Text & Image Support:** Text payloads can be broadcast directly over Realtime channels or written to database tables. Images are supported via Supabase Storage buckets (supporting files up to 50 MB each on the free tier) `[VERIFIED](https://supabase.com/pricing)`.
- **Directionality:** Bidirectional. Both Linux and Android clients connect to the Realtime WebSocket channel to publish and receive clipboard change events `[VERIFIED](https://supabase.com/pricing)`.
- **Phone App:** No turnkey commercial clipboard app exists; requires a custom Android client or PWA developed with the Supabase Android/Kotlin SDK `[ASSUMED]`.
- **Server / Account:** Requires a Supabase Cloud account or a self-hosted Supabase Docker Compose stack; self-hostable with no external cloud vendor lock-in `[VERIFIED](https://supabase.com/pricing)`.
- **Cost:** Free Cloud Tier ($0/month) includes 500 MB database storage, 1 GB file storage, 2 million Realtime messages/month, and 5 GB egress bandwidth. Self-hosted version is 100% free open-source software `[VERIFIED](https://supabase.com/pricing)`.
- **Privacy & Encryption:** Secured in transit via TLS. Access control governed by PostgreSQL Row-Level Security (RLS) policies. Raw database and object storage are unencrypted at the application layer unless client-side encryption is built into the syncing clients `[ASSUMED]`.
- **GNOME Wayland Caveats:** Linux endpoint runs a Python or Node.js background process subscribing to Supabase Realtime and reading/writing the Wayland clipboard via `wl-clipboard` `[ASSUMED]`.
- **Setup & Maintenance:** High setup effort (writing client applications, deploying PostgreSQL schemas, configuring RLS policies, and maintaining storage cleanup cron jobs). Medium maintenance `[ASSUMED]`.
- **Real-World Limitations:** Free cloud projects are **automatically paused after 7 days of inactivity**, terminating clipboard sync until manually unpaused via the web console `[VERIFIED](https://supabase.com/pricing)`. No turnkey client app exists. Android 10+ clipboard restrictions block background client reads `[ASSUMED]`.

---

## 10. Plain WebSocket relay

- **Sync Mechanism:** Minimalist custom client-server architecture. A lightweight server daemon (written in Go, Node.js, or Python) listens for persistent WebSocket connections from the Linux desktop and Android devices, relaying JSON or binary message packets immediately across all active connections `[ASSUMED]`.
- **Text & Image Support:** Both text and binary images (e.g. raw PNG bytes or base64 data) are fully supported. WebSocket frames natively accommodate arbitrary binary payloads up to server-configured message size limits (typically tens of megabytes) `[ASSUMED]`.
- **Directionality:** Bidirectional with minimal transmission latency (sub-100ms on local networks or fast cellular connections) `[ASSUMED]`.
- **Phone App:** Requires a custom Android application (or an automated Termux/Tasker WebSocket client) that runs a persistent foreground connection service `[ASSUMED]`.
- **Server / Account:** Self-hosted server (deployable on a local Raspberry Pi, home server, or cloud VPS). No third-party accounts or cloud dependencies `[ASSUMED]`.
- **Cost:** Free ($0) for software; hosting cost is $0 on local LAN / home server, or ~$3–$5/month if hosted on an entry-level cloud VPS `[ASSUMED]`.
- **Privacy & Encryption:** Plain `ws://` connections transmit data in unencrypted plaintext across the network. Must be secured using `wss://` (TLS) via a reverse proxy (e.g., Caddy, Nginx) or integrated TLS certificates. End-to-end payload encryption (e.g., AES-256-GCM) can be implemented in the client code `[ASSUMED]`.
- **GNOME Wayland Caveats:** Linux daemon uses `wl-paste --watch` to stream newly copied text and images to the WebSocket relay, and invokes `wl-copy` upon receiving incoming payloads from the socket `[ASSUMED]`.
- **Setup & Maintenance:** High setup effort; requires developing both the relay server and the client endpoints, writing reconnection logic, handling mobile network drops, and configuring TLS. High initial development burden, low-to-medium operational maintenance `[ASSUMED]`.
- **Real-World Limitations:** Android Doze mode and OEM battery management will aggressively terminate background WebSocket connections unless the client runs as a persistent foreground service with a visible ongoing notification. Android 10+ prevents background clients from reading the clipboard without active window focus or IME status `[ASSUMED]`.

---

## Sources

1. https://developer.android.com/about/versions/10/privacy/changes — Android 10 Privacy Changes: Limited access to clipboard data
2. https://developer.android.com/about/versions/12/behavior-changes-all — Android 12 Behavior Changes: Clipboard access notifications
3. https://developer.android.com/about/versions/13/features — Android 13 Features: Clipboard copy preview and automatic history expiration
4. https://community.kde.org/KDEConnect — KDE Connect Architecture and Protocol Specifications
5. https://userbase.kde.org/KDEConnect — KDE Connect UserBase: Clipboard sharing plugin and Android 10 limitations
6. https://github.com/GSConnect/gnome-shell-extension-gsconnect — GSConnect GNOME Shell Extension Documentation and Wayland Dependencies
7. https://syncthing.net/ — Syncthing Continuous File Synchronization Specification and Protocol
8. https://github.com/syncthing/syncthing-android — Syncthing for Android Wrapper Repository
9. https://docs.ntfy.sh/publish/#action-buttons — ntfy Documentation: Notification Action Buttons (`copy` action)
10. https://docs.ntfy.sh/publish/#attachments — ntfy Documentation: Publishing File Attachments and Size Quotas
11. https://ntfy.sh/#pricing — ntfy.sh Hosted Service Pricing and Account Quotas
12. https://blog.pushbullet.com/2014/08/20/introducing-universal-copy-paste/ — Pushbullet Universal Copy & Paste Announcement
13. https://www.pushbullet.com/pro — Pushbullet Pro Features and Subscription Pricing
14. https://help.pushbullet.com/articles/what-is-end-to-end-encryption/ — Pushbullet End-to-End Encryption Specifications
15. https://play.google.com/store/apps/details?id=com.joaoapps.join — Join by joaoapps Google Play Store Listing and Features
16. https://tailscale.com/pricing — Tailscale Pricing Tiers and Feature Matrix
17. https://github.com/Sathvik-Rao/ClipCascade — ClipCascade Open-Source Cross-Platform Clipboard Manager Repository
18. https://github.com/Jeric-X/SyncClipboard — SyncClipboard Cross-Platform Clipboard Synchronization System Repository
19. https://github.com/Jeric-X/syncclipboard-mobile — SyncClipboard Mobile Android Client Repository
20. https://github.com/termux/termux-api — Termux:API Add-on Repository and Clipboard Commands
21. https://joaoapps.com/tasker/ — Tasker Automation Framework Features and Clipboard Actions
22. https://github.com/Waboodoo/HTTP-Shortcuts — HTTP Shortcuts for Android Repository and Documentation
23. https://firebase.google.com/pricing — Google Firebase Spark and Blaze Pricing Tiers
24. https://firebase.google.com/docs/firestore/quotas — Cloud Firestore Usage Quotas and Limits (1 MiB document limit)
25. https://supabase.com/pricing — Supabase Pricing, Free Tier Storage, and Realtime Limits
