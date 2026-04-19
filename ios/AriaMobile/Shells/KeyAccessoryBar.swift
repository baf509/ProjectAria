import SwiftUI

struct KeyAccessoryBar: View {
    let onKey: (String) async -> Void
    let onLine: (String) async -> Void
    @State private var showingCtrl = false

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                key("Esc") { await onKey("Escape") }
                key("Tab") { await onKey("Tab") }
                key("↑")   { await onKey("Up") }
                key("↓")   { await onKey("Down") }
                key("←")   { await onKey("Left") }
                key("→")   { await onKey("Right") }
                key("Ctrl", systemImage: "control") { showingCtrl = true }
                key("⏎", systemImage: "return") { await onKey("Enter") }
                Divider().frame(height: 20)
                chip("yes")  { await onLine("yes") }
                chip("no")   { await onLine("no") }
                chip("1")    { await onLine("1") }
                chip("2")    { await onLine("2") }
                chip("3")    { await onLine("3") }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
        }
        .background(Neon.void)
        .sheet(isPresented: $showingCtrl) {
            CtrlPickerSheet { letter in
                showingCtrl = false
                Task { await onKey("C-\(letter)") }
            }
            .presentationDetents([.medium])
        }
    }

    private func key(_ label: String, systemImage: String? = nil, action: @escaping () async -> Void) -> some View {
        Button {
            Task { await action() }
        } label: {
            Group {
                if let systemImage { Image(systemName: systemImage) } else { Text(label) }
            }
            .font(.callout.monospaced())
            .foregroundStyle(Neon.cyan)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .frame(minWidth: 40)
            .background(Neon.surface)
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(Neon.cyan.opacity(0.3), lineWidth: 0.5)
            )
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
        .buttonStyle(.plain)
    }

    private func chip(_ label: String, action: @escaping () async -> Void) -> some View {
        Button {
            Task { await action() }
        } label: {
            Text(label)
                .font(.footnote.monospaced())
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(Neon.pink.opacity(0.2))
                .foregroundStyle(Neon.pink)
                .clipShape(Capsule())
                .overlay(Capsule().strokeBorder(Neon.pink.opacity(0.4), lineWidth: 0.5))
        }
        .buttonStyle(.plain)
    }
}

struct CtrlPickerSheet: View {
    let onSelect: (String) -> Void
    private let letters = ["A","B","C","D","E","F","G","H","I","J","K","L","M",
                           "N","O","P","Q","R","S","T","U","V","W","X","Y","Z",
                           "[","\\","]","^","_"]

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 6), spacing: 8) {
                    ForEach(letters, id: \.self) { letter in
                        Button(letter) { onSelect(letter) }
                            .font(.body.monospaced())
                            .foregroundStyle(Neon.cyan)
                            .padding(.vertical, 12)
                            .frame(maxWidth: .infinity)
                            .background(Neon.surface)
                            .overlay(
                                RoundedRectangle(cornerRadius: 8)
                                    .strokeBorder(Neon.purple.opacity(0.3), lineWidth: 0.5)
                            )
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                }
                .padding()
            }
            .navigationTitle("Ctrl-?")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
