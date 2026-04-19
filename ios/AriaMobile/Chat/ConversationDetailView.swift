import SwiftUI
import AriaKit

@MainActor
@Observable
final class ConversationStore {
    let conversationId: String
    var messages: [Message] = []
    var streamingText: String = ""
    var streamingToolCalls: [(name: String, arguments: String)] = []
    var isStreaming = false
    var error: String?
    var steerDraft: String = ""

    nonisolated(unsafe) private var sendTask: Task<Void, Never>?
    private let api: ConversationsAPI

    init(id: String, api: ConversationsAPI) {
        self.conversationId = id
        self.api = api
    }

    deinit { sendTask?.cancel() }

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
                        finalize()
                        return
                    case .error(let msg):
                        error = msg
                    }
                }
                finalize()
            } catch {
                self.error = error.localizedDescription
                finalize()
            }
        }
    }

    func cancel() {
        sendTask?.cancel()
        sendTask = nil
        finalize()
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

    private func finalize() {
        if !streamingText.isEmpty {
            messages.append(Message(role: "assistant", content: streamingText, ts: Date()))
        }
        streamingText = ""
        streamingToolCalls.removeAll()
        isStreaming = false
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
                                    id: "streaming",
                                    role: "assistant",
                                    content: store.streamingText
                                ),
                                streaming: true
                            )
                            .id("streaming")
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
                proxy.scrollTo("streaming", anchor: .bottom)
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
