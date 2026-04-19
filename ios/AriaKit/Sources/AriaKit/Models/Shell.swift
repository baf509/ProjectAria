import Foundation

public enum ShellStatus: String, Codable, Sendable, CaseIterable {
    case active, idle, stopped, unknown
}

public struct Shell: Codable, Sendable, Identifiable, Hashable {
    public var id: String { name }

    public let name: String
    public let shortName: String
    public let projectDir: String
    public let host: String
    public let status: ShellStatus
    public let createdAt: Date
    public let lastActivityAt: Date
    public let lastOutputAt: Date?
    public let lastInputAt: Date?
    public let lineCount: Int
    public let tags: [String]

    enum CodingKeys: String, CodingKey {
        case name
        case shortName = "short_name"
        case projectDir = "project_dir"
        case host
        case status
        case createdAt = "created_at"
        case lastActivityAt = "last_activity_at"
        case lastOutputAt = "last_output_at"
        case lastInputAt = "last_input_at"
        case lineCount = "line_count"
        case tags
    }
}

public struct ShellListResponse: Codable, Sendable {
    public let shells: [Shell]
}

public struct ShellCreateRequest: Codable, Sendable {
    public let name: String
    public let workdir: String?
    public let launchClaude: Bool

    public init(name: String, workdir: String? = nil, launchClaude: Bool = true) {
        self.name = name
        self.workdir = workdir
        self.launchClaude = launchClaude
    }

    enum CodingKeys: String, CodingKey {
        case name, workdir
        case launchClaude = "launch_claude"
    }
}

public struct ShellInputRequest: Codable, Sendable {
    public let text: String
    public let appendEnter: Bool
    public let literal: Bool

    public init(text: String, appendEnter: Bool = true, literal: Bool = false) {
        self.text = text
        self.appendEnter = appendEnter
        self.literal = literal
    }

    enum CodingKeys: String, CodingKey {
        case text, literal
        case appendEnter = "append_enter"
    }
}

public struct ShellInputResponse: Codable, Sendable {
    public let ok: Bool
    public let lineNumber: Int?

    enum CodingKeys: String, CodingKey {
        case ok
        case lineNumber = "line_number"
    }
}

public struct ShellTagsUpdate: Codable, Sendable {
    public let tags: [String]
    public init(tags: [String]) { self.tags = tags }
}
