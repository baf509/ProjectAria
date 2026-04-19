import SwiftUI
import AriaKit

struct TagsEditorSheet: View {
    @Environment(SettingsStore.self) private var settings
    @Environment(\.dismiss) private var dismiss
    let shell: Shell

    @State private var tags: [String] = []
    @State private var draft: String = ""
    @State private var submitting = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                Section("Tags") {
                    ForEach(tags, id: \.self) { tag in
                        HStack {
                            Text(tag)
                            Spacer()
                            Button(role: .destructive) {
                                tags.removeAll { $0 == tag }
                            } label: {
                                Image(systemName: "minus.circle")
                            }
                            .buttonStyle(.plain)
                            .foregroundStyle(.red)
                        }
                    }
                    HStack {
                        TextField("Add tag", text: $draft)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        Button("Add") {
                            let t = draft.trimmingCharacters(in: .whitespaces)
                            if !t.isEmpty, !tags.contains(t) { tags.append(t) }
                            draft = ""
                        }
                        .disabled(draft.isEmpty)
                    }
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red).font(.footnote) }
                }
            }
            .scrollContentBackground(.hidden)
            .background(Neon.void)
            .navigationTitle("Tags — \(shell.shortName)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Save") { Task { await save() } }
                        .disabled(submitting)
                }
            }
            .onAppear { tags = shell.tags }
        }
    }

    private func save() async {
        guard let api = settings.makeShells() else { return }
        submitting = true
        defer { submitting = false }
        do {
            _ = try await api.setTags(name: shell.name, tags: tags)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
