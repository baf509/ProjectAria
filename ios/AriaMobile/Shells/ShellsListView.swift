import SwiftUI
import AriaKit

@MainActor
@Observable
final class ShellsListStore {
    var shells: [Shell] = []
    var error: String?
    var loading = false
    var query: String = ""
    var statusFilter: ShellStatus? = nil

    func load(using api: ShellsAPI) async {
        loading = true
        defer { loading = false }
        do {
            let result = try await api.list()
            shells = result
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    var filtered: [Shell] {
        shells.filter { shell in
            (statusFilter == nil || shell.status == statusFilter)
            && (query.isEmpty
                || shell.shortName.localizedCaseInsensitiveContains(query)
                || shell.name.localizedCaseInsensitiveContains(query)
                || shell.projectDir.localizedCaseInsensitiveContains(query)
                || shell.tags.contains(where: { $0.localizedCaseInsensitiveContains(query) }))
        }
    }
}

struct ShellsListView: View {
    @Environment(SettingsStore.self) private var settings
    @State private var store = ShellsListStore()
    @State private var showCreate = false
    @State private var showSearch = false

    var body: some View {
        List {
            if let error = store.error {
                Section {
                    Text(error).foregroundStyle(.red).font(.footnote)
                }
            }

            ForEach(store.filtered) { shell in
                NavigationLink(value: shell) {
                    ShellRow(shell: shell)
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Neon.void)
        .searchable(text: Bindable(store).query, prompt: "Filter by name, project, tag")
        .refreshable { await reload() }
        .navigationTitle("Shells")
        .overlay {
            if store.shells.isEmpty && !store.loading && store.error == nil {
                ContentUnavailableView(
                    "No shells",
                    systemImage: "terminal",
                    description: Text("Tap + to spin up a new watched tmux session.")
                )
            }
        }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Menu {
                    Picker("Status", selection: Bindable(store).statusFilter) {
                        Text("All").tag(ShellStatus?.none)
                        ForEach(ShellStatus.allCases, id: \.self) {
                            Text($0.rawValue).tag(Optional($0))
                        }
                    }
                } label: {
                    Label("Filter", systemImage: "line.3.horizontal.decrease.circle")
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { showSearch = true } label: {
                    Image(systemName: "magnifyingglass")
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { showCreate = true } label: {
                    Image(systemName: "plus")
                }
            }
        }
        .sheet(isPresented: $showCreate) {
            ShellCreateSheet { created in
                store.shells.insert(created, at: 0)
            }
        }
        .sheet(isPresented: $showSearch) {
            ShellEventSearchView()
        }
        .navigationDestination(for: Shell.self) { shell in
            ShellDetailView(shell: shell)
        }
        .task { await reload() }
    }

    private func reload() async {
        guard let api = settings.makeShells() else {
            store.error = "Base URL not set"
            return
        }
        await store.load(using: api)
    }
}

struct ShellRow: View {
    let shell: Shell

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(shell.shortName)
                    .font(.body.monospaced())
                    .foregroundStyle(Neon.textPrimary)
                HStack(spacing: 6) {
                    StatusBadge(status: shell.status)
                    if !shell.projectDir.isEmpty {
                        Text(shortPath(shell.projectDir))
                            .font(.caption)
                            .foregroundStyle(Neon.textSecondary)
                            .lineLimit(1)
                            .truncationMode(.head)
                    }
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                RelativeTimeText(date: shell.lastActivityAt)
                    .font(.caption).foregroundStyle(Neon.textSecondary)
                Text("\(shell.lineCount) lines")
                    .font(.caption2).foregroundStyle(Neon.textTertiary)
            }
        }
        .padding(.vertical, 4)
        .listRowBackground(Neon.void)
    }

    private func shortPath(_ path: String) -> String {
        let parts = path.split(separator: "/")
        return parts.suffix(2).joined(separator: "/")
    }
}
