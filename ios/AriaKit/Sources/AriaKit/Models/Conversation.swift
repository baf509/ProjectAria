import Foundation

public struct ConversationListItem: Sendable, Identifiable, Hashable {
    public let id: String
    public let title: String?
    public let summary: String?
    public let createdAt: Date
    public let updatedAt: Date
    public let messageCount: Int?
    public let pinned: Bool?
    public let tags: [String]?
}

extension ConversationListItem: Codable {
    enum CodingKeys: String, CodingKey {
        case _id, id
        case title, summary
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case messageCount = "message_count"
        case pinned, tags
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try (try? c.decode(String.self, forKey: ._id))
            ?? c.decode(String.self, forKey: .id)
        self.title = try c.decodeIfPresent(String.self, forKey: .title)
        self.summary = try c.decodeIfPresent(String.self, forKey: .summary)
        self.createdAt = try c.decode(Date.self, forKey: .createdAt)
        self.updatedAt = try c.decodeIfPresent(Date.self, forKey: .updatedAt) ?? self.createdAt
        self.messageCount = try c.decodeIfPresent(Int.self, forKey: .messageCount)
        self.pinned = try c.decodeIfPresent(Bool.self, forKey: .pinned)
        self.tags = try c.decodeIfPresent([String].self, forKey: .tags)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: ._id)
        try c.encodeIfPresent(title, forKey: .title)
        try c.encodeIfPresent(summary, forKey: .summary)
        try c.encode(createdAt, forKey: .createdAt)
        try c.encode(updatedAt, forKey: .updatedAt)
        try c.encodeIfPresent(messageCount, forKey: .messageCount)
        try c.encodeIfPresent(pinned, forKey: .pinned)
        try c.encodeIfPresent(tags, forKey: .tags)
    }
}

public struct Message: Sendable, Identifiable, Hashable {
    public let id: String
    public let role: String
    public let content: String
    public let ts: Date?
    public let toolCalls: [ToolCall]?

    public init(
        id: String = UUID().uuidString,
        role: String,
        content: String,
        ts: Date? = nil,
        toolCalls: [ToolCall]? = nil
    ) {
        self.id = id
        self.role = role
        self.content = content
        self.ts = ts
        self.toolCalls = toolCalls
    }
}

extension Message: Codable {
    enum CodingKeys: String, CodingKey {
        case _id, id
        case role, content, ts
        case toolCalls = "tool_calls"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try (try? c.decode(String.self, forKey: ._id))
            ?? c.decodeIfPresent(String.self, forKey: .id)
            ?? UUID().uuidString
        self.role = try c.decode(String.self, forKey: .role)
        self.content = try c.decodeIfPresent(String.self, forKey: .content) ?? ""
        self.ts = try c.decodeIfPresent(Date.self, forKey: .ts)
        self.toolCalls = try c.decodeIfPresent([ToolCall].self, forKey: .toolCalls)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: ._id)
        try c.encode(role, forKey: .role)
        try c.encode(content, forKey: .content)
        try c.encodeIfPresent(ts, forKey: .ts)
        try c.encodeIfPresent(toolCalls, forKey: .toolCalls)
    }
}

public struct ToolCall: Codable, Sendable, Hashable {
    public let id: String?
    public let name: String
    /// Tool arguments as sent by the server. Anthropic-style backends send a JSON
    /// object; OpenAI-style backends send a JSON-encoded string. Accept either.
    public let arguments: JSONValue?
    /// Tool result. Free-form — strings, dicts, lists, numbers all show up.
    public let result: JSONValue?
}

public struct ConversationResponse: Sendable {
    public let id: String
    public let title: String?
    public let summary: String?
    public let messages: [Message]
    public let createdAt: Date
    public let updatedAt: Date
}

extension ConversationResponse: Codable {
    enum CodingKeys: String, CodingKey {
        case _id, id
        case title, summary, messages
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try (try? c.decode(String.self, forKey: ._id))
            ?? c.decode(String.self, forKey: .id)
        self.title = try c.decodeIfPresent(String.self, forKey: .title)
        self.summary = try c.decodeIfPresent(String.self, forKey: .summary)
        self.messages = try c.decodeIfPresent([Message].self, forKey: .messages) ?? []
        self.createdAt = try c.decode(Date.self, forKey: .createdAt)
        self.updatedAt = try c.decodeIfPresent(Date.self, forKey: .updatedAt) ?? self.createdAt
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: ._id)
        try c.encodeIfPresent(title, forKey: .title)
        try c.encodeIfPresent(summary, forKey: .summary)
        try c.encode(messages, forKey: .messages)
        try c.encode(createdAt, forKey: .createdAt)
        try c.encode(updatedAt, forKey: .updatedAt)
    }
}

public struct SendMessageRequest: Codable, Sendable {
    public let content: String
    public let stream: Bool

    public init(content: String, stream: Bool = true) {
        self.content = content
        self.stream = stream
    }
}
