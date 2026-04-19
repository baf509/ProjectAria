import SwiftUI
import AriaKit

struct SnapshotView: View {
    @Environment(SettingsStore.self) private var settings
    let shellName: String
    @State private var content: String = ""
    @State private var updatedAt: Date?
    @State private var error: String?

    var body: some View {
        ScrollView {
            if !content.isEmpty {
                Text(content)
                    .font(.system(size: 12, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(8)
            } else if let error {
                Text(error).foregroundStyle(.red).padding()
            } else {
                ProgressView().padding()
            }
        }
        .background(Neon.termBg)
        .foregroundStyle(Neon.termFg)
        .task {
            await refresh()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                guard !Task.isCancelled else { break }
                await refresh()
            }
        }
    }

    private func refresh() async {
        guard let api = settings.makeShells() else {
            error = "Base URL not set"
            return
        }
        do {
            let snap = try await api.snapshot(name: shellName)
            content = snap.content
            updatedAt = snap.ts
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
