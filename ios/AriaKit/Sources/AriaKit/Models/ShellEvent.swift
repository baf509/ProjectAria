import Foundation

public enum ShellEventKind: String, Codable, Sendable {
    case output, input, system
}

public enum ShellEventSource: String, Codable, Sendable {
    case pipePane = "pipe-pane"
    case sendKeys = "send-keys"
    case hook
    case reconciler
    case backfill
}

public struct ShellEvent: Codable, Sendable, Identifiable, Hashable {
    public var id: String { "\(shellName):\(lineNumber)" }

    public let shellName: String
    public let ts: Date
    public let lineNumber: Int
    public let kind: ShellEventKind
    public let textRaw: String
    public let textClean: String
    public let source: ShellEventSource
    public let byteOffset: Int?

    enum CodingKeys: String, CodingKey {
        case shellName = "shell_name"
        case ts
        case lineNumber = "line_number"
        case kind
        case textRaw = "text_raw"
        case textClean = "text_clean"
        case source
        case byteOffset = "byte_offset"
    }
}

public struct ShellEventsResponse: Codable, Sendable {
    public let events: [ShellEvent]
    public let hasMore: Bool

    enum CodingKeys: String, CodingKey {
        case events
        case hasMore = "has_more"
    }
}

public struct ShellSearchResponse: Codable, Sendable {
    public let events: [ShellEvent]
}

public struct ShellSnapshot: Codable, Sendable {
    public let shellName: String
    public let ts: Date
    public let content: String
    public let contentHash: String
    public let lineCountAtSnapshot: Int

    enum CodingKeys: String, CodingKey {
        case shellName = "shell_name"
        case ts, content
        case contentHash = "content_hash"
        case lineCountAtSnapshot = "line_count_at_snapshot"
    }
}

public struct ShellStatusUpdate: Codable, Sendable {
    public let status: ShellStatus
    public let lastActivityAt: Date

    enum CodingKeys: String, CodingKey {
        case status
        case lastActivityAt = "last_activity_at"
    }
}
