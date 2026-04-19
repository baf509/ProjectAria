import SwiftUI
import AriaKit

struct StatusBadge: View {
    let status: ShellStatus

    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 8, height: 8)
                .neonGlow(color, radius: 4)
            Text(status.rawValue).font(.caption).foregroundStyle(Neon.textSecondary)
        }
    }

    private var color: Color {
        switch status {
        case .active: return Neon.neonGreen
        case .idle: return Neon.neonYellow
        case .stopped: return Neon.textTertiary
        case .unknown: return Neon.violet
        }
    }
}
