import Foundation
import Observation
import AriaKit

@MainActor
@Observable
final class ShellStreamStore {
    let shell: Shell
    var status: ShellStatus
    var lastActivityAt: Date
    var lineCount: Int = 0
    var connectionState: ConnectionState = .idle
    var errorMessage: String?
    var noiseFilter: Bool = false

    enum ConnectionState: Equatable {
        case idle
        case backfilling
        case streaming
        case reconnecting
        case stopped
        case error(String)
    }

    private var streamTask: Task<Void, Never>?
    private var lastLine: Int = 0
    private let bridge: TerminalBridge
    private let api: ShellsAPI

    init(shell: Shell, api: ShellsAPI, bridge: TerminalBridge, noiseFilter: Bool) {
        self.shell = shell
        self.status = shell.status
        self.lastActivityAt = shell.lastActivityAt
        self.lineCount = shell.lineCount
        self.api = api
        self.bridge = bridge
        self.noiseFilter = noiseFilter
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
        // Backfill the last 2000 events (tail), not the first 2000. For
        // long-lived shells (tens of thousands of lines) starting from 0
        // would pin the viewport to session startup and force the SSE
        // stream to replay the entire history before reaching live.
        connectionState = .backfilling
        let backfillWindow = 2000
        let startLine = max(0, shell.lineCount - backfillWindow)
        do {
            let backfill = try await api.listEvents(
                name: shell.name, sinceLine: startLine, limit: backfillWindow
            )
            for evt in backfill.events {
                apply(event: evt)
            }
        } catch {
            errorMessage = "Backfill failed: \(error.localizedDescription)"
        }

        while !Task.isCancelled {
            connectionState = .streaming
            do {
                for try await update in api.stream(name: shell.name, sinceLine: lastLine) {
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
            // Stream ended cleanly — small backoff then reconnect.
            try? await Task.sleep(for: .seconds(1))
        }
    }

    private func apply(event: ShellEvent) {
        lastLine = max(lastLine, event.lineNumber)
        lineCount = max(lineCount, event.lineNumber)
        lastActivityAt = event.ts
        guard !shouldFilter(event) else { return }
        // SwiftTerm expects byte-level ANSI; feed text_raw unless it's an input
        // echo (tmux already echoes inputs to the pane, so we skip input events
        // to avoid double-printing).
        if event.kind == .input { return }
        let payload = terminalPayload(for: event)
        if payload.isEmpty { return }
        let bytes = Array(payload.utf8)
        // Append CRLF between line-oriented events — tmux pipe-pane emits
        // lines without trailing newlines.
        bridge.feed(bytes: bytes)
        bridge.feed(bytes: [0x0D, 0x0A])
    }

    private func terminalPayload(for event: ShellEvent) -> String {
        // Prefer raw ANSI. If absent (older server), fall back to clean text.
        event.textRaw.isEmpty ? event.textClean : event.textRaw
    }

    private func shouldFilter(_ event: ShellEvent) -> Bool {
        guard noiseFilter else { return false }
        let noise = ["Checking for updates", "Updating dependencies", "npm WARN"]
        return noise.contains(where: { event.textClean.contains($0) })
    }

    // MARK: - Input

    func sendInput(_ data: Data, appendEnter: Bool = false, literal: Bool = true) async {
        guard let text = String(data: data, encoding: .utf8) else { return }
        do {
            _ = try await api.sendInput(
                name: shell.name,
                ShellInputRequest(text: text, appendEnter: appendEnter, literal: literal)
            )
        } catch {
            errorMessage = "Send failed: \(error.localizedDescription)"
        }
    }

    func sendLine(_ text: String) async {
        do {
            _ = try await api.sendInput(
                name: shell.name,
                ShellInputRequest(text: text, appendEnter: true, literal: false)
            )
        } catch {
            errorMessage = "Send failed: \(error.localizedDescription)"
        }
    }

    func sendKey(_ key: String) async {
        // Named keys use tmux notation without -l.
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
