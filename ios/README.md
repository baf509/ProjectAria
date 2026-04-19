# Aria — iOS / iPadOS client

Native SwiftUI app for interacting with the rest of ARIA from your iPhone or iPad
over Tailscale. Primary use case is full control of **Aria Shells** (watched tmux
sessions): list, create, attach (live ANSI terminal via SwiftTerm), kill, tag,
search. Also includes chat, memory search, and push-notification-driven idle
alerts.

Inspired by Prompt 3 / Shellfish for terminal ergonomics.

## Requirements

- macOS with **Xcode 26.0+** (Swift 6)
- [XcodeGen](https://github.com/yonaskolb/XcodeGen) — `brew install xcodegen`
- Apple Developer account (for on-device install + APNs)
- Your iOS device and Mac both on the same **Tailscale** tailnet as the ARIA API

## First-time setup

```sh
cd ios
xcodegen generate          # produces AriaMobile.xcodeproj
open AriaMobile.xcodeproj
```

In Xcode:

1. Select the **AriaMobile** target → **Signing & Capabilities** → set your
   **Team**. (Or edit `project.yml` to set `DEVELOPMENT_TEAM` once and
   regenerate.)
2. Optional: change `PRODUCT_BUNDLE_IDENTIFIER` from `dev.aria.AriaMobile` to
   something in your own team.
3. Build & Run on an attached device (simulator works for everything except
   APNs).

First launch asks for a **Base URL** — this is your ARIA API reachable over
Tailscale, e.g. `http://<tailscale-hostname>:8000`. No API key is required
unless you've enabled `settings.api_auth_enabled` server-side.

## Project layout

```
ios/
├── project.yml               — XcodeGen spec (edit + regenerate)
├── AriaMobile/
│   ├── App/                  — entry point, root navigation, push registrar
│   ├── Settings/             — SettingsStore (@Observable) + SettingsView
│   ├── Shells/               — list, detail, SwiftTerm bridge, input, create, tags, snapshot, search
│   ├── Chat/                 — conversation list, streaming detail, message bubbles
│   ├── Memory/               — memory search
│   ├── Common/               — RelativeTimeText, StatusBadge
│   └── Resources/            — Info.plist, entitlements
└── AriaKit/                  — shared Swift package (models + networking)
    ├── Package.swift
    └── Sources/AriaKit/
        ├── Models/           — Sendable Codable types
        ├── Networking/       — AriaClient, SSEStream, Shells/Conversations/Memories/Health/Devices APIs
        └── Keychain/         — small wrapper around the Security framework
```

## Dependencies

Pulled in via SPM (resolved on first build):

- [SwiftTerm](https://github.com/migueldeicaza/SwiftTerm) — the ANSI / VT100
  terminal view backing the shell detail screen.

Zero third-party deps beyond SwiftTerm. Networking uses `URLSession` + async/await.

## What the app can do

### Shells (full parity with the CLI)

| Capability                       | Where                                      |
|----------------------------------|--------------------------------------------|
| List watched shells              | Shells tab                                 |
| Filter by status, search by name | Shells tab toolbar                         |
| Create new session               | `+` in toolbar → sheet                     |
| Attach (live ANSI terminal)      | Tap a row → detail view                    |
| Backfill 2000 events on open     | Automatic on detail appear                 |
| Live SSE stream                  | Automatic; reconnects on drop              |
| Send input (free-form)           | Input bar                                  |
| Special keys (Esc/Tab/arrows/⏎)  | Key accessory bar                          |
| Ctrl-?                           | Ctrl button → letter picker sheet          |
| Quick replies (yes/no/1/2/3)     | Key accessory bar                          |
| Kill session                     | ⋯ menu → Kill (confirmed)                  |
| Edit tags                        | ⋯ menu → Tags                              |
| Snapshot view (3s refresh)       | ⋯ menu → View: Snapshot                    |
| Noise filter toggle              | ⋯ menu → Noise filter                      |
| Full-text search across shells   | Toolbar 🔍                                  |

### Chat

- Conversation list
- Streaming replies (SSE) with tool-call markers
- Steer / cancel in-flight responses

### Memory

- Hybrid-search across long-term memory (debounced 300ms)

### Settings

- Base URL, optional API key (stored in Keychain)
- Health check
- Terminal font + size
- Default noise filter on/off
- Re-register for APNs

## Push notifications (Phase 6)

The app registers for APNs and posts the device token to `POST /api/v1/devices`.
Server-side sending is **stubbed by default**: configure these env vars on the
API host to enable real delivery.

```sh
SHELLS_APNS_ENABLED=true
APNS_TEAM_ID=...           # Apple Developer Team ID
APNS_KEY_ID=...            # .p8 key ID
APNS_BUNDLE_ID=dev.aria.AriaMobile
APNS_AUTH_KEY_PATH=/path/to/AuthKey_XXXXXX.p8
APNS_USE_SANDBOX=true      # set false for TestFlight/App Store builds
```

See `api/aria/shells/apns.py` for where to plug in the HTTP/2 client (deliberately
left as a stub — needs `httpx[http2]` or `aioapns` plus ES256-signed JWTs).

Alerts fire via the existing `IdleNotifier` when a watched shell idles at a prompt
— the same trigger that sends Signal/Telegram notifications today.

## Regenerating the project

Edit `project.yml`, then:

```sh
xcodegen generate
```

`AriaMobile.xcodeproj` is **git-ignored** — the generated project should never
be committed. Everyone regenerates from `project.yml`.

## Known first-build things

Fresh checkouts sometimes need:

1. **File > Packages > Reset Package Caches** (if SwiftTerm doesn't resolve)
2. A clean build after setting your Team (Product > Clean Build Folder)
3. On a device, trust the developer profile in Settings > General > VPN & Device
   Management

## Troubleshooting

- **"The resource could not be loaded"** → your phone can't reach the API host.
  Check Tailscale is up on both devices, and try `http://<tailscale-ip>:8000/api/v1/health`
  in Safari first.
- **Terminal shows garbled text** → confirm the API version includes the raw
  ANSI change; `text_raw` must be present in SSE payloads from `/shells/{name}/stream`.
- **Kill doesn't actually kill** → check `journalctl --user -u aria-api` for
  a `tmux kill-session failed` entry.
