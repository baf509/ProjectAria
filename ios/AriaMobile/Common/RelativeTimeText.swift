import SwiftUI

/// Renders a relative timestamp like "5m" or "2h ago" that updates as time
/// passes. Uses TimelineView so the tick driver stops automatically when the
/// view leaves the hierarchy — no app-wide singleton timer that would burn
/// battery for a view nobody is looking at.
struct RelativeTimeText: View {
    let date: Date

    private static let formatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f
    }()

    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            Text(Self.formatter.localizedString(for: date, relativeTo: context.date))
        }
    }
}
