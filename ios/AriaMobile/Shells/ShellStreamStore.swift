import Foundation
import Observation
import AriaKit

/// Read-only live event store for a watched shell. Drives the monitoring view
/// in the iOS app — for interactive use, SSH in via Blink/Termius and run the
/// `claude` wrapper. The mobile app intentionally stops here at "watch the
/// session" instead of trying to be a terminal emulator.
@MainActor
@Observable
final class ShellStreamStore {
    let shell: Shell
    var status: ShellStatus
    var lastActivityAt: Date
    var lineCount: Int = 0
    var connectionState: ConnectionState = .idle
    var errorMessage: String?
    /// Rolling buffer of the most recent ANSI-stripped output lines, oldest
    /// first. Capped at recentBufferLimit so memory doesn't grow unbounded
    /// for chatty shells.
    var recentLines: [RecentLine] = []
    /// True once we've seen at least one event. Drives the "waiting for
    /// output" placeholder in the UI.
    var hasReceivedOutput: Bool = false

    enum ConnectionState: Equatable {
        case idle
        case backfilling
        case streaming
        case reconnecting
        case stopped
        case error(String)
    }

    /// One captured shell line, ANSI-stripped for legible monitoring display.
    struct RecentLine: Identifiable, Equatable {
        let id: Int          // line_number from the server
        let ts: Date
        let text: String     // text_clean
        let kind: ShellEventKind
    }

    private static let recentBufferLimit = 400

    private var streamTask: Task<Void, Never>?
    private var maxAppliedLine: Int = 0
    private let api: ShellsAPI

    init(shell: Shell, api: ShellsAPI) {
        self.shell = shell
        self.status = shell.status
        self.lastActivityAt = shell.lastActivityAt
        self.lineCount = shell.lineCount
        self.api = api
    }

    func start() {
        streamTask?.cancel()
        streamTask = Task { await runLoop() }
    }

    func stop() {
        streamTask?.cancel()
        streamTask = nil
        connectionState = .idle
    }

    private func runLoop() async {
        // Backfill the tail (last N events) so the user sees recent context
        // immediately, then transition to live streaming. The buffer cap
        // limits how much of that we keep in memory.
        connectionState = .backfilling
        let backfillCount = Self.recentBufferLimit
        let startLine = max(0, shell.lineCount - backfillCount)
        do {
            let backfill = try await api.listEvents(
                name: shell.name, sinceLine: startLine, limit: backfillCount
            )
            for evt in backfill.events {
                apply(event: evt)
            }
        } catch {
            errorMessage = "Backfill failed: \(error.localizedDescription)"
        }

        while !Task.isCancelled {
            connectionState = .streaming
            errorMessage = nil
            do {
                for try await update in api.stream(name: shell.name, sinceLine: maxAppliedLine) {
                    try Task.checkCancellation()
                    switch update {
                    case .event(let evt):
                        apply(event: evt)
                    case .status(let s):
                        status = s.status
                        lastActivityAt = s.lastActivityAt
                        if s.status == .stopped {
                            connectionState = .stopped
                            return
                        }
                    case .heartbeat:
                        break
                    }
                }
            } catch is CancellationError {
                return
            } catch {
                errorMessage = error.localizedDescription
                connectionState = .reconnecting
                try? await Task.sleep(for: .seconds(2))
                continue
            }
            try? await Task.sleep(for: .seconds(1))
        }
    }

    private func apply(event: ShellEvent) {
        guard event.lineNumber > maxAppliedLine else {
            lastActivityAt = event.ts
            return
        }
        maxAppliedLine = event.lineNumber
        lineCount = max(lineCount, event.lineNumber)
        lastActivityAt = event.ts
        hasReceivedOutput = true
        // Skip input-echo events — they're the user's own keystrokes and
        // would clutter the monitoring scrollback.
        if event.kind == .input { return }
        let text = event.textClean
        if text.isEmpty { return }
        recentLines.append(RecentLine(
            id: event.lineNumber, ts: event.ts, text: text, kind: event.kind
        ))
        if recentLines.count > Self.recentBufferLimit {
            recentLines.removeFirst(recentLines.count - Self.recentBufferLimit)
        }
    }

    // MARK: - Quick actions
    //
    // Even though the mobile app doesn't render an interactive terminal, it's
    // useful to be able to send a small set of common keystrokes — answer a
    // y/n prompt, send Enter to dismiss a modal, send Ctrl-C to interrupt a
    // hung process. For anything more complex, the user opens Blink.

    /// Send a one-line text input followed by Enter. Uses literal mode so
    /// special characters (`;` `~` `#`) are sent as keystrokes, not parsed
    /// as tmux key syntax.
    func sendLine(_ text: String) async {
        guard !text.isEmpty else { return }
        do {
            _ = try await api.sendInput(
                name: shell.name,
                ShellInputRequest(text: text, appendEnter: false, literal: true)
            )
            _ = try await api.sendInput(
                name: shell.name,
                ShellInputRequest(text: "Enter", appendEnter: false, literal: false)
            )
        } catch {
            errorMessage = "Send failed: \(error.localizedDescription)"
        }
    }

    /// Send a named tmux key (e.g. "Enter", "Escape", "C-c", "Tab").
    func sendKey(_ key: String) async {
        do {
            _ = try await api.sendInput(
                name: shell.name,
                ShellInputRequest(text: key, appendEnter: false, literal: false)
            )
        } catch {
            errorMessage = "Send failed: \(error.localizedDescription)"
        }
    }
}
