import SwiftUI

@MainActor
@Observable
final class TimeTickPublisher {
    static let shared = TimeTickPublisher()
    var tick = Date()
    private var task: Task<Void, Never>?

    private init() {
        task = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(30))
                self?.tick = Date()
            }
        }
    }
}

struct RelativeTimeText: View {
    let date: Date
    @State private var publisher = TimeTickPublisher.shared

    private static let formatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f
    }()

    var body: some View {
        Text(Self.formatter.localizedString(for: date, relativeTo: publisher.tick))
    }
}
