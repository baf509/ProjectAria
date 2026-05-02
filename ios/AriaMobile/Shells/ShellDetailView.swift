import SwiftUI
import AriaKit

/// Read-only monitoring view for a watched shell. Intentionally NOT an
/// interactive terminal — for actual terminal work, the user opens Blink (or
/// any SSH client) and runs the `claude` wrapper, which lands them in the
/// same tmux session that this view is watching.
///
/// What this view DOES provide that a terminal app doesn't:
/// - At-a-glance status, last-activity, line-count, tags
/// - Live snapshot view that refreshes every few seconds
/// - Recent-events scrollback (ANSI-stripped, mobile-readable)
/// - Quick-action buttons for the most common one-tap keystrokes
///   (Enter, Ctrl-C, yes/no/1/2/3) so you can ack a prompt from the bus
/// - A deep-link to open the same session in Blink for full interaction
struct ShellDetailView: View {
    @Environment(SettingsStore.self) private var settings
    @Environment(\.dismiss) private var dismiss
    let shell: Shell

    @State private var store: ShellStreamStore?
    @State private var viewMode: ViewMode = .recent
    @State private var showTags = false
    @State private var showKillConfirm = false

    enum ViewMode: Hashable, CaseIterable {
        case recent      // recent-events scrollback
        case snapshot    // current pane snapshot

        var label: String {
            switch self {
            case .recent: return "Recent"
            case .snapshot: return "Snapshot"
            }
        }
        var symbol: String {
            switch self {
            case .recent: return "list.bullet.rectangle"
            case .snapshot: return "camera.viewfinder"
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
            if let store, store.status != .stopped {
                Divider()
                quickActions
            }
        }
        .background(Neon.void)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { toolbar }
        .sheet(isPresented: $showTags) {
            if let store { TagsEditorSheet(shell: store.shell) }
        }
        .confirmationDialog(
            "Kill \(shell.shortName)?",
            isPresented: $showKillConfirm,
            titleVisibility: .visible
        ) {
            Button("Kill session", role: .destructive) { Task { await killSession() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This sends tmux kill-session. Any running process is terminated.")
        }
        .task { setupStore() }
        .onDisappear { store?.stop() }
    }

    // MARK: - Header

    private var header: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                StatusBadge(status: store?.status ?? shell.status)
                Text(shell.shortName)
                    .font(.headline.monospaced())
                    .foregroundStyle(Neon.pink)
                if !shell.projectDir.isEmpty {
                    Text(shell.projectDir)
                        .font(.caption).foregroundStyle(Neon.textSecondary)
                        .lineLimit(1).truncationMode(.head)
                }
                Spacer()
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            // Picker for the two read-only modes — recent events vs snapshot.
            Picker("View", selection: $viewMode) {
                ForEach(ViewMode.allCases, id: \.self) { mode in
                    Label(mode.label, systemImage: mode.symbol).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 12)
            .padding(.bottom, 8)

            // Hint when the shell is stopped — make it clear that re-attaching
            // is a separate path (Blink + wrapper) rather than buried in this
            // view.
            if let store, store.status == .stopped {
                stoppedBanner
            } else if let store, let err = store.errorMessage, !err.isEmpty {
                errorBanner(err: err) {
                    store.errorMessage = nil
                }
            }
        }
        .background(Neon.surface)
    }

    private var stoppedBanner: some View {
        HStack(spacing: 6) {
            Image(systemName: "stop.circle.fill").font(.caption)
            Text("Session is stopped. Open a terminal (Blink) and run `claude` to start a new one.")
                .font(.caption)
                .lineLimit(3)
            Spacer()
        }
        .foregroundStyle(Neon.textSecondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Neon.neonYellow.opacity(0.12))
    }

    private func errorBanner(err: String, dismiss: @escaping () -> Void) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill").font(.caption)
            Text(err).font(.caption).lineLimit(2)
            Spacer()
            Button { dismiss() } label: {
                Image(systemName: "xmark.circle.fill").font(.caption)
            }
            .buttonStyle(.plain)
        }
        .foregroundStyle(Neon.neonRed)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Neon.neonRed.opacity(0.12))
    }

    // MARK: - Content

    @ViewBuilder
    private var content: some View {
        switch viewMode {
        case .recent: recentEventsView
        case .snapshot: SnapshotView(shellName: shell.name)
        }
    }

    private var recentEventsView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    if let store {
                        if store.recentLines.isEmpty {
                            placeholder
                        } else {
                            ForEach(store.recentLines) { line in
                                Text(line.text)
                                    .font(.system(size: 12, design: .monospaced))
                                    .foregroundStyle(Neon.termFg)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .textSelection(.enabled)
                                    .id(line.id)
                            }
                        }
                    }
                }
                .padding(8)
            }
            .background(Neon.termBg)
            .onChange(of: store?.recentLines.last?.id ?? 0) { _, newId in
                withAnimation(.easeOut(duration: 0.15)) {
                    if newId != 0 { proxy.scrollTo(newId, anchor: .bottom) }
                }
            }
        }
    }

    @ViewBuilder
    private var placeholder: some View {
        VStack(spacing: 8) {
            if let state = store?.connectionState {
                switch state {
                case .backfilling:
                    ProgressView()
                    Text("Loading recent activity…").font(.caption).foregroundStyle(Neon.textSecondary)
                case .reconnecting:
                    Image(systemName: "arrow.clockwise").foregroundStyle(Neon.neonYellow)
                    Text("Reconnecting…").font(.caption).foregroundStyle(Neon.textSecondary)
                case .stopped:
                    Image(systemName: "stop.circle").foregroundStyle(Neon.neonRed)
                    Text("Session stopped").font(.caption).foregroundStyle(Neon.textSecondary)
                default:
                    ProgressView()
                    Text("Waiting for output…").font(.caption).foregroundStyle(Neon.textSecondary)
                }
            }
        }
        .frame(maxWidth: .infinity, minHeight: 120)
    }

    // MARK: - Quick actions

    private var quickActions: some View {
        VStack(spacing: 6) {
            // Predefined one-tap responses that cover the cases where you'd
            // open a terminal from a phone "just to nudge claude".
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    quickKey("⏎ Enter") { await store?.sendKey("Enter") }
                    quickKey("Esc")     { await store?.sendKey("Escape") }
                    quickKey("⌃C")      { await store?.sendKey("C-c") }
                    Divider().frame(height: 20)
                    quickChip("yes")    { await store?.sendLine("yes") }
                    quickChip("no")     { await store?.sendLine("no") }
                    quickChip("1")      { await store?.sendLine("1") }
                    quickChip("2")      { await store?.sendLine("2") }
                    quickChip("3")      { await store?.sendLine("3") }
                    Divider().frame(height: 20)
                    blinkButton
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
            }
            .background(Neon.surface)
        }
    }

    private func quickKey(_ label: String, action: @escaping () async -> Void) -> some View {
        Button { Task { await action() } } label: {
            Text(label)
                .font(.callout.monospaced())
                .foregroundStyle(Neon.cyan)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .frame(minWidth: 44)
                .background(Neon.surface)
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .strokeBorder(Neon.cyan.opacity(0.3), lineWidth: 0.5)
                )
                .clipShape(RoundedRectangle(cornerRadius: 6))
        }
        .buttonStyle(.plain)
    }

    private func quickChip(_ label: String, action: @escaping () async -> Void) -> some View {
        Button { Task { await action() } } label: {
            Text(label)
                .font(.footnote.monospaced())
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(Neon.pink.opacity(0.2))
                .foregroundStyle(Neon.pink)
                .clipShape(Capsule())
                .overlay(Capsule().strokeBorder(Neon.pink.opacity(0.4), lineWidth: 0.5))
        }
        .buttonStyle(.plain)
    }

    /// Open this shell in Blink Shell via its custom URL scheme. Falls back
    /// to a no-op if Blink isn't installed; the user's wrapper runs `claude`
    /// which (since the session already exists) reattaches via tmux.
    private var blinkButton: some View {
        Button {
            openInBlink()
        } label: {
            Label("Open in Blink", systemImage: "rectangle.connected.to.line.below")
                .font(.footnote.monospaced())
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(Neon.violet.opacity(0.2))
                .foregroundStyle(Neon.violet)
                .clipShape(Capsule())
                .overlay(Capsule().strokeBorder(Neon.violet.opacity(0.4), lineWidth: 0.5))
        }
        .buttonStyle(.plain)
    }

    private func openInBlink() {
        // Blink supports a `blinkshell://run?cmd=...` URL scheme. The cmd
        // here SSHes to the user's configured host and attaches to this
        // shell's tmux session directly — bypassing the wrapper's
        // create-or-attach branching since we know the session exists.
        let command = "ssh corsair -t 'tmux attach -t \(shell.name)'"
        guard
            let encoded = command.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
            let url = URL(string: "blinkshell://run?cmd=\(encoded)")
        else { return }
        UIApplication.shared.open(url)
    }

    // MARK: - Toolbar

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Button { showTags = true } label: {
                    Label("Tags", systemImage: "tag")
                }
                Divider()
                Button(role: .destructive) {
                    showKillConfirm = true
                } label: {
                    Label("Kill session", systemImage: "xmark.circle")
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
        }
    }

    // MARK: - Lifecycle

    private func setupStore() {
        guard store == nil, let api = settings.makeShells() else { return }
        let s = ShellStreamStore(shell: shell, api: api)
        store = s
        s.start()
    }

    private func killSession() async {
        guard let api = settings.makeShells() else { return }
        do {
            try await api.delete(name: shell.name)
            dismiss()
        } catch {
            store?.errorMessage = "Kill failed: \(error.localizedDescription)"
        }
    }
}
