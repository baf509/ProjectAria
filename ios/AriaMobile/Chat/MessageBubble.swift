import SwiftUI
import AriaKit

struct MessageBubble: View {
    let message: Message
    var streaming: Bool = false
    /// Tool calls discovered during the live stream that have not yet been
    /// persisted to the message. Rendered alongside `message.toolCalls` for
    /// the streaming bubble only.
    var pendingToolCalls: [(name: String, arguments: String)] = []

    var body: some View {
        HStack(alignment: .top) {
            if message.role == "user" { Spacer(minLength: 40) }
            VStack(alignment: .leading, spacing: 4) {
                content
                if streaming {
                    HStack(spacing: 4) {
                        Circle().fill(Neon.cyan).frame(width: 4, height: 4).opacity(0.3)
                        Circle().fill(Neon.pink).frame(width: 4, height: 4).opacity(0.5)
                        Circle().fill(Neon.cyan).frame(width: 4, height: 4).opacity(0.7)
                    }
                }
                if let calls = message.toolCalls, !calls.isEmpty {
                    ForEach(Array(calls.enumerated()), id: \.offset) { _, call in
                        toolCallRow(name: call.name, arguments: call.arguments?.displayString)
                    }
                }
                ForEach(Array(pendingToolCalls.enumerated()), id: \.offset) { _, call in
                    toolCallRow(name: call.name, arguments: call.arguments)
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
            if message.role == "assistant" || message.role == "system" || message.role == "tool" {
                Spacer(minLength: 40)
            }
        }
    }

    /// Streaming and tool messages skip the markdown parse: streaming because
    /// the parse cost is O(n²) on a growing buffer, tool because the output is
    /// usually a code/JSON/log blob where markdown interpretation hurts more
    /// than it helps.
    @ViewBuilder
    private var content: some View {
        if streaming {
            Text(message.content)
                .font(.body)
                .textSelection(.enabled)
        } else if message.role == "tool" {
            ToolResultView(content: message.content)
        } else {
            RenderedMarkdown(content: message.content)
                .textSelection(.enabled)
                .font(.body)
        }
    }

    @ViewBuilder
    private func toolCallRow(name: String, arguments: String?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Label(name, systemImage: "wrench.and.screwdriver")
                .font(.caption.monospaced())
                .foregroundStyle(Neon.violet)
            if let args = arguments, !args.isEmpty {
                Text(args)
                    .font(.caption2.monospaced())
                    .foregroundStyle(Neon.textSecondary)
                    .lineLimit(3)
                    .truncationMode(.tail)
            }
        }
    }

    private var bubbleBackground: Color {
        switch message.role {
        case "user": return Neon.pink.opacity(0.15)
        case "assistant": return Neon.surface
        case "tool": return Neon.surface.opacity(0.7)
        default: return Neon.neonYellow.opacity(0.1)
        }
    }

    private var bubbleBorder: Color {
        switch message.role {
        case "user": return Neon.pink.opacity(0.4)
        case "assistant": return Neon.cyan.opacity(0.2)
        case "tool": return Neon.violet.opacity(0.3)
        default: return Neon.neonYellow.opacity(0.3)
        }
    }
}

/// Renders a tool result in monospaced, scrollable form. Long outputs (file
/// reads, command output, JSON) are truncated by default; user can expand.
private struct ToolResultView: View, Equatable {
    let content: String
    @State private var expanded = false

    private static let collapsedLineLimit = 6

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("tool output", systemImage: "terminal")
                .font(.caption2.monospaced())
                .foregroundStyle(Neon.violet)
            Text(displayContent)
                .font(.caption.monospaced())
                .foregroundStyle(Neon.textSecondary)
                .textSelection(.enabled)
                .lineLimit(expanded ? nil : Self.collapsedLineLimit)
            if shouldShowToggle {
                Button(expanded ? "Collapse" : "Show all") {
                    expanded.toggle()
                }
                .font(.caption2)
                .foregroundStyle(Neon.cyan)
            }
        }
    }

    /// Strip ANSI escape sequences so terminal output is readable in chat.
    private var displayContent: String {
        Self.stripAnsi(content)
    }

    private var shouldShowToggle: Bool {
        displayContent.split(separator: "\n", omittingEmptySubsequences: false).count > Self.collapsedLineLimit
    }

    nonisolated static func == (lhs: ToolResultView, rhs: ToolResultView) -> Bool {
        lhs.content == rhs.content
    }

    /// Minimal CSI / OSC escape stripper. Tool output captured from the shell
    /// tool can contain SGR color codes, cursor positioning, etc. that would
    /// render as garbage in a SwiftUI Text.
    private static func stripAnsi(_ s: String) -> String {
        var out = String()
        out.reserveCapacity(s.count)
        var iter = s.unicodeScalars.makeIterator()
        while let c = iter.next() {
            if c == "\u{1B}" {
                // ESC [ ... letter   (CSI)
                // ESC ] ... BEL/ST   (OSC)
                // ESC <single-char>  (other)
                guard let next = iter.next() else { break }
                if next == "[" {
                    while let n = iter.next() {
                        if (0x40...0x7E).contains(n.value) { break }
                    }
                } else if next == "]" {
                    while let n = iter.next() {
                        if n == "\u{07}" { break }
                        if n == "\u{1B}" { _ = iter.next(); break }
                    }
                }
                continue
            }
            out.unicodeScalars.append(c)
        }
        return out
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
