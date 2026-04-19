import SwiftUI
import AriaKit

struct ShellEventSearchView: View {
    @Environment(SettingsStore.self) private var settings
    @Environment(\.dismiss) private var dismiss
    @State private var query: String = ""
    @State private var results: [ShellEvent] = []
    @State private var error: String?
    @State private var searching = false

    var body: some View {
        NavigationStack {
            List {
                if searching { ProgressView() }
                if let error {
                    Section { Text(error).foregroundStyle(.red).font(.footnote) }
                }
                ForEach(results) { event in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(event.shellName)
                                .font(.caption.monospaced())
                                .foregroundStyle(Neon.pink)
                            Spacer()
                            RelativeTimeText(date: event.ts)
                                .font(.caption2).foregroundStyle(Neon.textTertiary)
                        }
                        Text(event.textClean)
                            .font(.footnote.monospaced())
                            .foregroundStyle(Neon.textPrimary)
                            .lineLimit(3)
                    }
                    .listRowBackground(Neon.void)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Neon.void)
            .navigationTitle("Search")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Close") { dismiss() }
                }
            }
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always))
            .onSubmit(of: .search) { Task { await runSearch() } }
        }
    }

    private func runSearch() async {
        guard let api = settings.makeShells() else { return }
        guard !query.isEmpty else {
            results = []
            return
        }
        searching = true
        defer { searching = false }
        do {
            results = try await api.search(query: query)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
