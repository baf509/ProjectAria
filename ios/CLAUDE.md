# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Aria iOS — a native SwiftUI **dashboard** for the ARIA API, accessed over Tailscale. It is intentionally NOT a terminal emulator: for interactive shell work, the user opens a real SSH/terminal app (Blink, Termius, etc.) and runs the `claude` wrapper, which lands them in the same ARIA shell that this app is monitoring.

What the app provides:
- Shell list with status / last-activity / line-count / tags
- Read-only shell detail: snapshot view + recent-events scrollback (ANSI-stripped)
- Quick-action buttons (Enter, Ctrl-C, yes/no, Open in Blink) for one-tap nudges from the phone
- Chat with ARIA (streaming replies, tool-call display, tool-output rendering)
- Memory search
- APNs push notifications

What the app explicitly does NOT provide:
- Live interactive terminal rendering. Earlier versions used SwiftTerm; that was ripped out because building a competitive mobile terminal emulator is out of scope and the rendering bugs (cursor positioning, ANSI escape handling, keyboard ergonomics) were a tar pit. Real terminal apps do this better.

Targets iOS 17.0+ with Swift 6.0 strict concurrency.

## Build & Run

The Xcode project is generated from `project.yml` and is git-ignored. Regenerate after any change to project.yml:

```sh
brew install xcodegen        # one-time
xcodegen generate
open AriaMobile.xcodeproj
```

Set `DEVELOPMENT_TEAM` in project.yml or Xcode Signing & Capabilities before building to a device.

## Run Tests

```sh
# AriaKit package tests (model decoding)
cd AriaKit && swift test

# Or from Xcode: Product > Test (Cmd+U) on the AriaMobile scheme
```

Test coverage is currently limited to `AriaKitTests/ModelDecodingTests.swift` (JSON encode/decode of Shell, ShellEvent, ShellCreateRequest).

## Architecture

**MVVM with @Observable** — no Combine, no third-party state management.

Data flow: **View → @Observable Store → API client (Sendable struct) → URLSession async/await**

- **Stores** (`ShellStreamStore`, `ConversationStore`, `ShellsListStore`, `ConversationsListStore`, `SettingsStore`) are `@Observable @MainActor` classes that own state and async task lifecycle.
- **API clients** (`ShellsAPI`, `ConversationsAPI`, `MemoriesAPI`, `HealthAPI`, `DevicesAPI`) are cheap `Sendable` structs created on demand from `AriaClient`.
- **Streaming** uses SSE via `SSEStream` → `AsyncThrowingStream`. Shell detail subscribes to the same SSE stream the (now-removed) terminal viewer used, but feeds events into a capped recent-lines buffer rather than a virtual terminal.
- **Dependency injection** via `@Environment` — `SettingsStore.shared` is passed through the environment from the app root.

**Responsive layout**: `PhoneRootView` (TabView) for compact size class, `PadRootView` (NavigationSplitView) for regular.

## Two Targets

| Module | Type | Role |
|--------|------|------|
| **AriaMobile** | App target | SwiftUI views, stores, app lifecycle |
| **AriaKit** | Local SPM package | Models (Codable/Sendable), networking, keychain |

## Concurrency Rules (Swift 6 strict)

`SWIFT_STRICT_CONCURRENCY: complete` is enforced. All public types in AriaKit must be `Sendable`. UI-facing stores must be `@MainActor`. Use `Task {}` for background work with explicit actor boundaries.

## Dependencies

- **No third-party Swift packages.** Networking is plain URLSession.
- Earlier versions depended on SwiftTerm; do **not** reintroduce it. The mobile app delegates terminal interaction to external SSH apps via the `blinkshell://run?cmd=...` deep link.

## JSON Conventions

Models use `CodingKeys` for snake_case (API) ↔ camelCase (Swift) mapping. Dates are ISO8601 with fractional seconds, decoded via `AriaClient.decodeISODate` which falls back through 3-digit-fraction and no-fraction forms to handle Python's microsecond-precision `isoformat()` output.

## Key Patterns

- Quick-action buttons in `ShellDetailView` use tmux key notation (`Enter`, `Escape`, `C-c`) sent via `ShellsAPI.sendInput(literal: false)`. Free-text input goes via `sendLine()` which uses `literal: true` + a separate Enter keypress so user input isn't mangled by tmux's key parser.
- `ShellStreamStore` keeps a capped (~400) buffer of recent ANSI-stripped lines for the monitoring scrollback. The full terminal byte stream stays on the server.
- APNs token is posted to `POST /api/v1/devices`; server-side sending is stubbed (needs `httpx[http2]` or `aioapns`).
- API key stored in Keychain via `KeychainStorage` (service: `dev.aria.AriaMobile`, accessible after first unlock).
- `NSAllowsArbitraryLoads: true` in Info.plist because ARIA API runs over Tailscale HTTP.
- "Open in Blink" deep link in `ShellDetailView` constructs `blinkshell://run?cmd=ssh corsair -t 'tmux attach -t <session>'`. The user must have a Blink host alias named `corsair` configured.
