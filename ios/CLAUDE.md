# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Aria iOS — a native SwiftUI client for the ARIA API, accessed over Tailscale. Provides live ANSI terminal streaming (via SwiftTerm), chat with streaming replies, memory search, and APNs push notifications. Targets iOS 17.0+ with Swift 6.0 strict concurrency.

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
- **Streaming** uses SSE via `SSEStream` → `AsyncThrowingStream`. Shell detail backfills 2000 events then live-streams with automatic 2s reconnect.
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

- **SwiftTerm** (1.2.0+ via SPM) — ANSI/VT100 terminal view, bridged to SwiftUI via `UIViewRepresentable` in `ShellTerminalView`.
- No other third-party dependencies. Networking is plain URLSession.

## JSON Conventions

Models use `CodingKeys` for snake_case (API) ↔ camelCase (Swift) mapping. Dates are ISO8601 with fractional seconds, decoded via custom `AriaClient.isoDecoder`.

## Key Patterns

- Shell terminal input uses tmux key notation (`C-a`, `Escape`, `Tab`, etc.) sent via `ShellsAPI.sendInput()`.
- `ShellStreamStore` feeds raw ANSI bytes (`text_raw`) to SwiftTerm, appending CRLF between events.
- APNs token is posted to `POST /api/v1/devices`; server-side sending is stubbed (needs `httpx[http2]` or `aioapns`).
- API key stored in Keychain via `KeychainStorage` (service: `dev.aria.AriaMobile`, accessible after first unlock).
- `NSAllowsArbitraryLoads: true` in Info.plist because ARIA API runs over Tailscale HTTP.
