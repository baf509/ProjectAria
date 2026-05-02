import SwiftUI
import AriaKit

@MainActor
@Observable
final class ConversationStore {
    let conversationId: String
    var messages: [Message] = []
    var streamingText: String = ""
    var streamingToolCalls: [(name: String, arguments: String)] = []
    var streamingId: String = UUID().uuidString
    var isStreaming = false
    var error: String?
    var steerDraft: String = ""

    /// nonisolated(unsafe) because deinit is implicitly nonisolated and we
    /// want to cancel the in-flight task there. This is sound: send() and
    /// cancel() are both @MainActor methods (no concurrent writers), and
    /// Task.cancel() is itself nonisolated and thread-safe. The "unsafe" is
    /// only telling the compiler we've reasoned about the actor-isolation
    /// of this storage — not about Task internals.
    nonisolated(unsafe) private var sendTask: Task<Void, Never>?
    private let api: ConversationsAPI

    init(id: String, api: ConversationsAPI) {
        self.conversationId = id
        self.api = api
    }

    deinit {
        sendTask?.cancel()
    }

    func load() async {
        do {
            let convo = try await api.get(id: conversationId)
            messages = convo.messages
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    func send(_ text: String) {
        guard !text.isEmpty, !isStreaming else { return }
        messages.append(Message(role: "user", content: text, ts: Date()))
        streamingText = ""
        streamingToolCalls.removeAll()
        streamingId = UUID().uuidString
        isStreaming = true

        sendTask = Task { [weak self] in
            guard let self else { return }
            do {
                for try await chunk in api.sendStreaming(conversationId: conversationId, content: text) {
                    try Task.checkCancellation()
                    switch chunk {
                    case .text(let piece): streamingText += piece
                    case .toolCall(let name, let args):
                        streamingToolCalls.append((name, args))
                    case .done:
                        await finalize()
                        return
                    case .error(let msg):
                        error = msg
                    }
                }
                // Stream ended without an explicit .done — finalize anyway so
                // any persisted tool/assistant messages on the server side
                // become visible (the streaming-only buffer drops them).
                await finalize()
            } catch is CancellationError {
                isStreaming = false
            } catch {
                self.error = error.localizedDescription
                await finalize()
            }
        }
    }

    func cancel() {
        sendTask?.cancel()
        sendTask = nil
        // Don't re-enter finalize here; the task's catch CancellationError
        // path resets isStreaming. Calling finalize() would hit the API and
        // could race with reload.
        isStreaming = false
        streamingText = ""
        streamingToolCalls.removeAll()
    }

    func steer(interrupt: Bool = false) async {
        let text = steerDraft.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        steerDraft = ""
        do {
            try await api.steer(id: conversationId, content: text, interrupt: interrupt)
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// On stream completion, reload the conversation from the API. The
    /// streaming buffer only sees text and tool-call chunks, but the server
    /// persists `role="tool"` result messages separately — without a reload,
    /// those would not appear in the UI until the user navigated away and
    /// back.
    private func finalize() async {
        defer {
            streamingText = ""
            streamingToolCalls.removeAll()
            isStreaming = false
        }
        do {
            let convo = try await api.get(id: conversationId)
            messages = convo.messages
        } catch {
            // Reload failed — fall back to whatever streaming captured so the
            // user at least sees the assistant text.
            if !streamingText.isEmpty {
                messages.append(Message(role: "assistant", content: streamingText, ts: Date()))
            }
            self.error = error.localizedDescription
        }
    }
}

struct ConversationDetailView: View {
    @Environment(SettingsStore.self) private var settings
    let conversationId: String
    let title: String

    @State private var store: ConversationStore?
    @State private var draft: String = ""
    @FocusState private var inputFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            messageList
            Divider()
            inputBar
        }
        .background(Neon.void)
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .task { setup() }
    }

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if let store {
                        ForEach(store.messages) { msg in
                            MessageBubble(message: msg).id(msg.id)
                        }
                        if store.isStreaming {
                            MessageBubble(
                                message: Message(
                                    id: store.streamingId,
                                    role: "assistant",
                                    content: store.streamingText
                                ),
                                streaming: true,
                                pendingToolCalls: store.streamingToolCalls
                            )
                            .id(store.streamingId)
                        }
                        if let error = store.error, !error.isEmpty {
                            Text(error).foregroundStyle(Neon.neonRed).font(.footnote).padding(.horizontal)
                        }
                    }
                }
                .padding()
            }
            .onChange(of: store?.messages.count ?? 0) { _, _ in
                withAnimation(.easeOut(duration: 0.2)) {
                    proxy.scrollTo(store?.messages.last?.id, anchor: .bottom)
                }
            }
            .onChange(of: store?.streamingText.count ?? 0) { _, _ in
                if let id = store?.streamingId {
                    proxy.scrollTo(id, anchor: .bottom)
                }
            }
        }
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            if let store, store.isStreaming {
                Button(role: .destructive) { store.cancel() } label: {
                    Image(systemName: "stop.circle.fill")
                        .font(.title2)
                        .foregroundStyle(Neon.neonRed)
                }
                TextField("Steer (optional)…", text: Bindable(store).steerDraft)
                    .textFieldStyle(.roundedBorder)
                    .focused($inputFocused)
                Button("Steer") { Task { await store.steer() } }
                    .disabled(store.steerDraft.isEmpty)
                    .foregroundStyle(Neon.cyan)
            } else {
                TextField("Message ARIA…", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .focused($inputFocused)
                    .submitLabel(.send)
                    .onSubmit(send)
                Button {
                    send()
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.title)
                        .foregroundStyle(draft.isEmpty ? Neon.textTertiary : Neon.cyan)
                        .neonGlow(Neon.cyan, radius: draft.isEmpty ? 0 : 6)
                }
                .disabled(draft.isEmpty)
            }
        }
        .padding(8)
        .background(Neon.surface)
    }

    private func setup() {
        guard store == nil, let api = settings.makeConversations() else { return }
        let s = ConversationStore(id: conversationId, api: api)
        store = s
        Task { await s.load() }
    }

    private func send() {
        let text = draft
        draft = ""
        store?.send(text)
    }
}
