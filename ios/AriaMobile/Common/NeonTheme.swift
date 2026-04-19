import SwiftUI

// MARK: - Neon Tokyo Color Palette

enum Neon {
    // Backgrounds
    static let void        = Color(red: 0.04, green: 0.04, blue: 0.10)   // #0a0a1a — deepest bg
    static let surface     = Color(red: 0.10, green: 0.10, blue: 0.18)   // #1a1a2e — cards/surfaces
    static let surfaceAlt  = Color(red: 0.14, green: 0.12, blue: 0.22)   // #241e38 — raised surface

    // Neon accents
    static let pink        = Color(red: 1.00, green: 0.18, blue: 0.48)   // #ff2d7b — primary neon
    static let cyan        = Color(red: 0.00, green: 0.94, blue: 1.00)   // #00f0ff — secondary neon
    static let purple      = Color(red: 0.62, green: 0.16, blue: 0.87)   // #9d29dd — accent
    static let violet      = Color(red: 0.55, green: 0.36, blue: 1.00)   // #8c5cff — soft accent

    // Semantic
    static let neonGreen   = Color(red: 0.22, green: 1.00, blue: 0.08)   // #39ff14 — success/active
    static let neonYellow  = Color(red: 0.94, green: 1.00, blue: 0.00)   // #f0ff00 — warning/idle
    static let neonRed     = Color(red: 1.00, green: 0.20, blue: 0.40)   // #ff3366 — error/destructive

    // Text
    static let textPrimary   = Color(red: 0.93, green: 0.93, blue: 0.98) // slightly blue-white
    static let textSecondary = Color(red: 0.60, green: 0.56, blue: 0.72) // muted lavender
    static let textTertiary  = Color(red: 0.40, green: 0.38, blue: 0.50) // dim

    // Terminal
    static let termBg      = Color(red: 0.05, green: 0.03, blue: 0.10)   // ultra-dark purple-black
    static let termFg      = Color(red: 0.00, green: 0.94, blue: 1.00)   // cyan text

    // Gradients
    static let headerGradient = LinearGradient(
        colors: [pink.opacity(0.6), purple.opacity(0.4), cyan.opacity(0.3)],
        startPoint: .leading,
        endPoint: .trailing
    )

    static let glowGradient = LinearGradient(
        colors: [pink.opacity(0.3), purple.opacity(0.2)],
        startPoint: .top,
        endPoint: .bottom
    )

    static let surfaceGradient = LinearGradient(
        colors: [surface, void],
        startPoint: .top,
        endPoint: .bottom
    )
}

// MARK: - View Modifiers

struct NeonSurface: ViewModifier {
    var cornerRadius: CGFloat = 12

    func body(content: Content) -> some View {
        content
            .background(Neon.surface)
            .clipShape(RoundedRectangle(cornerRadius: cornerRadius))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius)
                    .strokeBorder(Neon.purple.opacity(0.25), lineWidth: 0.5)
            )
    }
}

struct NeonGlow: ViewModifier {
    var color: Color = Neon.cyan
    var radius: CGFloat = 8

    func body(content: Content) -> some View {
        content
            .shadow(color: color.opacity(0.4), radius: radius, x: 0, y: 0)
    }
}

extension View {
    func neonSurface(cornerRadius: CGFloat = 12) -> some View {
        modifier(NeonSurface(cornerRadius: cornerRadius))
    }

    func neonGlow(_ color: Color = Neon.cyan, radius: CGFloat = 8) -> some View {
        modifier(NeonGlow(color: color, radius: radius))
    }
}
