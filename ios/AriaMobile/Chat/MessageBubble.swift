import SwiftUI
import AriaKit

struct MessageBubble: View {
    let message: Message
    var streaming: Bool = false

    var body: some View {
        HStack(alignment: .top) {
            if message.role == "user" { Spacer(minLength: 40) }
            VStack(alignment: .leading, spacing: 4) {
                RenderedMarkdown(content: message.content)
                    .textSelection(.enabled)
                    .font(.body)
                if streaming {
                    HStack(spacing: 4) {
                        Circle().fill(Neon.cyan).frame(width: 4, height: 4).opacity(0.3)
                        Circle().fill(Neon.pink).frame(width: 4, height: 4).opacity(0.5)
                        Circle().fill(Neon.cyan).frame(width: 4, height: 4).opacity(0.7)
                    }
                }
                if let calls = message.toolCalls, !calls.isEmpty {
                    ForEach(Array(calls.enumerated()), id: \.offset) { _, call in
                        VStack(alignment: .leading, spacing: 2) {
                            Label(call.name, systemImage: "wrench.and.screwdriver")
                                .font(.caption.monospaced())
                                .foregroundStyle(Neon.violet)
                            if let args = call.arguments {
                                Text(args.displayString)
                                    .font(.caption2.monospaced())
                                    .foregroundStyle(Neon.textSecondary)
                                    .lineLimit(3)
                                    .truncationMode(.tail)
                            }
                        }
                    }
                }
            }
            .padding(10)
            .foregroundStyle(Neon.textPrimary)
            .background(bubbleBackground)
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(bubbleBorder, lineWidth: 0.5)
            )
            .clipShape(RoundedRectangle(cornerRadius: 12))
            if message.role == "assistant" || message.role == "system" {
                Spacer(minLength: 40)
            }
        }
    }

    private var bubbleBackground: Color {
        switch message.role {
        case "user": return Neon.pink.opacity(0.15)
        case "assistant": return Neon.surface
        default: return Neon.neonYellow.opacity(0.1)
        }
    }

    private var bubbleBorder: Color {
        switch message.role {
        case "user": return Neon.pink.opacity(0.4)
        case "assistant": return Neon.cyan.opacity(0.2)
        default: return Neon.neonYellow.opacity(0.3)
        }
    }
}

/// Caches the parsed AttributedString so markdown isn't re-parsed on every view update.
private struct RenderedMarkdown: View, Equatable {
    let content: String

    var body: some View {
        Text(rendered)
    }

    private var rendered: AttributedString {
        if let attr = try? AttributedString(
            markdown: content,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            return attr
        }
        return AttributedString(content)
    }

    nonisolated static func == (lhs: RenderedMarkdown, rhs: RenderedMarkdown) -> Bool {
        lhs.content == rhs.content
    }
}
