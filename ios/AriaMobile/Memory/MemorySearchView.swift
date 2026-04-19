import SwiftUI
import AriaKit

struct MemorySearchView: View {
    @Environment(SettingsStore.self) private var settings
    @State private var query: String = ""
    @State private var results: [Memory] = []
    @State private var error: String?
    @State private var searching = false
    @State private var debounce: Task<Void, Never>?

    var body: some View {
        List {
            if searching { ProgressView() }
            if let error {
                Section { Text(error).foregroundStyle(.red).font(.footnote) }
            }
            ForEach(results) { memory in
                VStack(alignment: .leading, spacing: 4) {
                    Text(memory.content)
                        .font(.body)
                        .foregroundStyle(Neon.textPrimary)
                        .lineLimit(6)
                    HStack(spacing: 6) {
                        if let source = memory.source?.type {
                            Text(source).font(.caption).foregroundStyle(Neon.violet)
                        }
                        if let confidence = memory.confidence {
                            Text("\(Int(confidence * 100))%")
                                .font(.caption2)
                                .foregroundStyle(Neon.cyan.opacity(0.7))
                        }
                        Spacer()
                        if let date = memory.createdAt {
                            RelativeTimeText(date: date).font(.caption2).foregroundStyle(Neon.textTertiary)
                        }
                    }
                }
                .padding(.vertical, 4)
                .listRowBackground(Neon.void)
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Neon.void)
        .navigationTitle("Memory")
        .searchable(text: $query, prompt: "Search memories")
        .onChange(of: query) { _, newValue in
            debounce?.cancel()
            debounce = Task {
                try? await Task.sleep(for: .milliseconds(300))
                if !Task.isCancelled { await runSearch(query: newValue) }
            }
        }
        .overlay {
            if query.isEmpty && results.isEmpty && error == nil {
                ContentUnavailableView(
                    "Search ARIA's memory",
                    systemImage: "brain",
                    description: Text("Hybrid vector + BM25 search over long-term memory.")
                )
            }
        }
    }

    private func runSearch(query: String) async {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else {
            results = []
            return
        }
        guard let api = settings.makeMemories() else {
            error = "Base URL not set"
            return
        }
        searching = true
        defer { searching = false }
        do {
            results = try await api.search(query: q, limit: 25)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
