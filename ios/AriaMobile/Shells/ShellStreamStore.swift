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
    /// True once we've fed at least one byte to the terminal. Drives the
    /// "waiting for output" overlay so the user can tell a quiet pane from
    /// a broken connection.
    var hasReceivedOutput: Bool = false

    enum ConnectionState: Equatable {
        case idle
        case backfilling
        case streaming
        case reconnecting
        case stopped
        case error(String)
    }

    private var streamTask: Task<Void, Never>?
    private var resizeTask: Task<Void, Never>?
    private var lastSentGeometry: (cols: Int, rows: Int)?
    private var firstResizeAttempted: Bool = false
    /// Highest line_number seen so far. Mutated only on @MainActor (this class
    /// is @MainActor-isolated), so the max() guard is also a monotonicity
    /// guard against out-of-order delivery from backfill+stream.
    private var maxAppliedLine: Int = 0
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
        resizeTask?.cancel()
        resizeTask = nil
        connectionState = .idle
    }

    private func runLoop() async {
        // Wait briefly for the SwiftTerm view to report its geometry so we
        // can resize the server-side tmux pane BEFORE backfill replays. Without
        // this, the first events the user sees are rendered at the server's
        // wide default (120×40) and look broken on phone screens. Cap the wait
        // at 500ms so an undersized/headless test path doesn't hang forever.
        for _ in 0..<10 {
            if lastSentGeometry != nil { break }
            try? await Task.sleep(for: .milliseconds(50))
        }

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
            // Successful (re)connect: clear stale error banner so the user
            // knows the issue resolved. Also drop the cached geometry so the
            // server gets a fresh resize after reconnect (otherwise a rotation
            // that happened during the disconnect can be skipped because
            // lastSentGeometry still matches).
            errorMessage = nil
            lastSentGeometry = nil
            firstResizeAttempted = false
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
            // Stream ended cleanly — small backoff then reconnect.
            try? await Task.sleep(for: .seconds(1))
        }
    }

    private func apply(event: ShellEvent) {
        // Out-of-order delivery from backfill+stream can re-deliver lines we
        // already saw. max() makes this idempotent.
        guard event.lineNumber > maxAppliedLine else {
            // Still bump activity even on duplicates so UI doesn't think the
            // shell is dead.
            lastActivityAt = event.ts
            return
        }
        maxAppliedLine = event.lineNumber
        lineCount = max(lineCount, event.lineNumber)
        lastActivityAt = event.ts
        hasReceivedOutput = true
        guard !shouldFilter(event) else { return }
        // SwiftTerm expects byte-level ANSI; feed text_raw unless it's an input
        // echo (tmux already echoes inputs to the pane, so we skip input events
        // to avoid double-printing).
        if event.kind == .input { return }
        let payload = terminalPayload(for: event)
        if payload.isEmpty { return }
        // Feed the bytes verbatim. The server preserves the original byte
        // stream (including any trailing \n that pipe-pane delivered), so
        // SwiftTerm sees what the underlying TUI actually wrote — no synthetic
        // newlines that would clobber Claude Code's cursor-positioned redraws.
        bridge.feed(bytes: Array(payload.utf8))
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

    /// Send a text line as keystrokes. Uses literal=true so tmux send-keys
    /// passes the bytes through as characters rather than interpreting them
    /// as key names — without `-l`, characters like `;` `~` `#` get treated
    /// as tmux key syntax which mangles user-typed code/JSON. We append a
    /// separate Enter keypress because `-l` mode does not interpret the
    /// "Enter" name.
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

    /// Tell the server about the client's terminal size so the running TUI
    /// repaints at this geometry. Debounced (200ms) to absorb rapid SwiftTerm
    /// callbacks during rotation / keyboard show-hide animations, and skipped
    /// when geometry hasn't actually changed.
    ///
    /// The first resize on a new view skips the debounce — without that, the
    /// first ~200ms of TUI output renders at the server's wide default
    /// (120×40) and looks broken on phone-sized screens. `firstResizeAttempted`
    /// (rather than a `lastSentGeometry == nil` check) prevents a flurry of
    /// in-flight resize calls from each thinking they're "the first" while
    /// the network round-trip is still pending.
    func notifyResize(cols: Int, rows: Int) {
        guard cols >= 20, rows >= 10 else { return }
        if let last = lastSentGeometry, last.cols == cols, last.rows == rows {
            return
        }
        let isFirstResize = !firstResizeAttempted
        firstResizeAttempted = true
        resizeTask?.cancel()
        resizeTask = Task { [weak self, shellName = shell.name, api] in
            if !isFirstResize {
                try? await Task.sleep(for: .milliseconds(200))
                if Task.isCancelled { return }
            }
            do {
                try await api.resize(name: shellName, cols: cols, rows: rows)
                self?.lastSentGeometry = (cols, rows)
            } catch {
                self?.errorMessage = "Resize failed: \(error.localizedDescription)"
            }
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
