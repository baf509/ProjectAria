import SwiftUI
import AriaKit

struct SettingsView: View {
    @Environment(SettingsStore.self) private var settings
    @Environment(PushRegistrar.self) private var push
    @State private var healthText: String = "—"
    @State private var checkingHealth = false

    var body: some View {
        @Bindable var settings = settings

        Form {
            Section("Server") {
                TextField("Base URL", text: $settings.baseURLString)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                SecureField("API key (optional)", text: $settings.apiKey)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                HStack {
                    Text("Health")
                    Spacer()
                    if checkingHealth { ProgressView() }
                    Text(healthText).foregroundStyle(.secondary).font(.footnote.monospaced())
                }
                Button("Check health") { Task { await checkHealth() } }
            }

            Section("Shells") {
                Toggle("Noise filter on by default", isOn: $settings.noiseFilterDefault)
            }

            Section("Terminal") {
                Picker("Font", selection: $settings.terminalFontName) {
                    ForEach(["Menlo", "Courier", "SF Mono"], id: \.self) { Text($0).tag($0) }
                }
                Stepper(
                    value: $settings.terminalFontSize,
                    in: 8.0...20.0,
                    step: 0.5
                ) {
                    Text("Size: \(settings.terminalFontSize, specifier: "%.1f")")
                }
            }

            Section("Notifications") {
                HStack {
                    Text("APNs token")
                    Spacer()
                    Text(push.tokenShort).foregroundStyle(.secondary).font(.footnote.monospaced())
                }
                if !push.lastError.isEmpty {
                    Text(push.lastError).foregroundStyle(.red).font(.footnote)
                }
                Button("Re-register") {
                    Task { await push.requestAuthorization(forceReregister: true) }
                }
            }

            Section("About") {
                LabeledContent("Version") {
                    Text("0.1.0")
                        .font(.footnote.monospaced())
                        .foregroundStyle(Neon.cyan)
                }
            }
        }
        .scrollContentBackground(.hidden)
        .background(Neon.void)
        .navigationTitle("Settings")
    }

    private func checkHealth() async {
        guard let api = settings.makeHealth() else {
            healthText = "no baseURL"
            return
        }
        checkingHealth = true
        defer { checkingHealth = false }
        do {
            let h = try await api.ping()
            healthText = "\(h.status)\(h.version.map { " · v\($0)" } ?? "")"
        } catch {
            healthText = "\(error.localizedDescription)"
        }
    }
}
