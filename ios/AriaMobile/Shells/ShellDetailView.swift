import SwiftUI
import AriaKit

struct ShellDetailView: View {
    @Environment(SettingsStore.self) private var settings
    @Environment(\.dismiss) private var dismiss
    let shell: Shell

    @State private var bridge = TerminalBridge()
    @State private var store: ShellStreamStore?
    @State private var inputText = ""
    @State private var viewMode: ViewMode = .terminal
    @State private var showTags = false
    @State private var showKillConfirm = false
    @FocusState private var inputFocused: Bool

    enum ViewMode: Hashable { case terminal, snapshot }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
            if viewMode == .terminal {
                Divider()
                inputBar
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

    // MARK: - UI pieces

    private var header: some View {
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
            if let store, let err = store.errorMessage, !err.isEmpty {
                Text(err).font(.caption2).foregroundStyle(Neon.neonRed).lineLimit(1)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Neon.surface)
    }

    @ViewBuilder
    private var content: some View {
        switch viewMode {
        case .terminal:
            ShellTerminalView(
                bridge: bridge,
                fontName: settings.terminalFontName,
                fontSize: CGFloat(settings.terminalFontSize),
                onInput: { data in
                    Task { await store?.sendInput(data, literal: true) }
                }
            )
            .ignoresSafeArea(edges: .bottom)
        case .snapshot:
            SnapshotView(shellName: shell.name)
        }
    }

    private var inputBar: some View {
        VStack(spacing: 0) {
            if let store {
                KeyAccessoryBar(
                    onKey: { key in await store.sendKey(key) },
                    onLine: { line in await store.sendLine(line) }
                )
            }
            HStack(spacing: 8) {
                TextField("Type a line and press send", text: $inputText, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .focused($inputFocused)
                    .submitLabel(.send)
                    .onSubmit { Task { await sendLine() } }
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button {
                    Task { await sendLine() }
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.title)
                        .foregroundStyle(inputText.isEmpty ? Neon.textTertiary : Neon.cyan)
                        .neonGlow(Neon.cyan, radius: inputText.isEmpty ? 0 : 6)
                }
                .disabled(inputText.isEmpty)
            }
            .padding(8)
            .background(Neon.surface)
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Picker("View", selection: $viewMode) {
                    Label("Terminal", systemImage: "terminal").tag(ViewMode.terminal)
                    Label("Snapshot", systemImage: "camera.viewfinder").tag(ViewMode.snapshot)
                }
                Divider()
                Toggle("Noise filter", isOn: Binding(
                    get: { store?.noiseFilter ?? false },
                    set: { store?.noiseFilter = $0 }
                ))
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

    // MARK: - Actions

    private func setupStore() {
        guard store == nil, let api = settings.makeShells() else { return }
        let s = ShellStreamStore(
            shell: shell,
            api: api,
            bridge: bridge,
            noiseFilter: settings.noiseFilterDefault
        )
        store = s
        s.start()
    }

    private func sendLine() async {
        let text = inputText
        inputText = ""
        guard !text.isEmpty else { return }
        await store?.sendLine(text)
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
