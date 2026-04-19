import SwiftUI
import AriaKit

struct ShellCreateSheet: View {
    @Environment(SettingsStore.self) private var settings
    @Environment(\.dismiss) private var dismiss
    var onCreated: (Shell) -> Void

    @State private var name: String = ""
    @State private var workdir: String = ""
    @State private var launchClaude: Bool = true
    @State private var error: String?
    @State private var submitting = false

    var body: some View {
        NavigationStack {
            Form {
                Section("New shell") {
                    TextField("Name (e.g. myproj)", text: $name)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Working directory (optional)", text: $workdir)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Toggle("Launch Claude Code", isOn: $launchClaude)
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red).font(.footnote) }
                }
                Section {
                    Text("Session name will be prefixed with `claude-` if not already.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Neon.void)
            .navigationTitle("New shell")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Create") { Task { await create() } }
                        .disabled(name.isEmpty || submitting)
                }
            }
            .overlay {
                if submitting { ProgressView().controlSize(.large) }
            }
        }
    }

    private func create() async {
        guard let api = settings.makeShells() else {
            error = "Base URL not set"
            return
        }
        submitting = true
        defer { submitting = false }
        do {
            let req = ShellCreateRequest(
                name: name.trimmingCharacters(in: .whitespaces),
                workdir: workdir.isEmpty ? nil : workdir,
                launchClaude: launchClaude
            )
            let shell = try await api.create(req)
            onCreated(shell)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
