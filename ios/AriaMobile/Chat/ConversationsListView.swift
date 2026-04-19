import SwiftUI
import AriaKit

@MainActor
@Observable
final class ConversationsListStore {
    var items: [ConversationListItem] = []
    var error: String?
    var loading = false

    func load(using api: ConversationsAPI) async {
        loading = true
        defer { loading = false }
        do {
            items = try await api.list()
            error = nil
        } catch let DecodingError.keyNotFound(key, context) {
            self.error = "Missing key \"\(key.stringValue)\" at \(context.codingPath.map(\.stringValue).joined(separator: "."))"
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct ConversationsListView: View {
    @Environment(SettingsStore.self) private var settings
    @State private var store = ConversationsListStore()
    @State private var creating = false

    var body: some View {
        List {
            if let error = store.error {
                Section { Text(error).foregroundStyle(.red).font(.footnote) }
            }
            ForEach(store.items) { item in
                NavigationLink(value: item) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item.title ?? "Untitled")
                            .font(.body)
                            .foregroundStyle(Neon.textPrimary)
                            .lineLimit(1)
                        HStack {
                            if let summary = item.summary, !summary.isEmpty {
                                Text(summary)
                                    .font(.caption).foregroundStyle(Neon.textSecondary)
                                    .lineLimit(1)
                            }
                            Spacer()
                            RelativeTimeText(date: item.updatedAt)
                                .font(.caption2).foregroundStyle(Neon.textTertiary)
                        }
                    }
                    .padding(.vertical, 4)
                    .listRowBackground(Neon.void)
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Neon.void)
        .refreshable { await reload() }
        .overlay {
            if store.items.isEmpty && !store.loading && store.error == nil {
                ContentUnavailableView(
                    "No conversations",
                    systemImage: "bubble.left.and.bubble.right",
                    description: Text("Start a new chat with ARIA.")
                )
            }
        }
        .navigationTitle("Chat")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Task { await newConversation() }
                } label: {
                    Image(systemName: creating ? "hourglass" : "plus")
                }
                .disabled(creating)
            }
        }
        .navigationDestination(for: ConversationListItem.self) { item in
            ConversationDetailView(conversationId: item.id, title: item.title ?? "Chat")
        }
        .task { await reload() }
    }

    private func reload() async {
        guard let api = settings.makeConversations() else {
            store.error = "Base URL not set"
            return
        }
        await store.load(using: api)
    }

    private func newConversation() async {
        guard let api = settings.makeConversations() else { return }
        creating = true
        defer { creating = false }
        do {
            let convo = try await api.create()
            await reload()
            // Optimistic nav handled by user tap on new row in list.
            _ = convo
        } catch {
            store.error = error.localizedDescription
        }
    }
}
