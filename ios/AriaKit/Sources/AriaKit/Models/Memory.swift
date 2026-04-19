import Foundation

public struct MemorySource: Codable, Sendable, Hashable {
    public let type: String?
    public let project: String?
}

public struct Memory: Sendable, Identifiable, Hashable {
    public let id: String
    public let content: String
    public let contentType: String?
    public let createdAt: Date?
    public let confidence: Double?
    public let source: MemorySource?
    public let importance: Double?
}

extension Memory: Codable {
    enum CodingKeys: String, CodingKey {
        case _id, id
        case content
        case contentType = "content_type"
        case createdAt = "created_at"
        case confidence, source, importance
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try (try? c.decode(String.self, forKey: ._id))
            ?? c.decode(String.self, forKey: .id)
        self.content = try c.decode(String.self, forKey: .content)
        self.contentType = try c.decodeIfPresent(String.self, forKey: .contentType)
        self.createdAt = try c.decodeIfPresent(Date.self, forKey: .createdAt)
        self.confidence = try c.decodeIfPresent(Double.self, forKey: .confidence)
        // source can be a string or an object
        if let obj = try? c.decodeIfPresent(MemorySource.self, forKey: .source) {
            self.source = obj
        } else if let str = try? c.decodeIfPresent(String.self, forKey: .source) {
            self.source = MemorySource(type: str, project: nil)
        } else {
            self.source = nil
        }
        self.importance = try c.decodeIfPresent(Double.self, forKey: .importance)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: ._id)
        try c.encode(content, forKey: .content)
        try c.encodeIfPresent(contentType, forKey: .contentType)
        try c.encodeIfPresent(createdAt, forKey: .createdAt)
        try c.encodeIfPresent(confidence, forKey: .confidence)
        try c.encodeIfPresent(source, forKey: .source)
        try c.encodeIfPresent(importance, forKey: .importance)
    }
}

public struct MemorySearchResponse: Codable, Sendable {
    public let results: [Memory]?
    public let memories: [Memory]?

    public var items: [Memory] { results ?? memories ?? [] }
}

public struct HealthStatus: Codable, Sendable {
    public let status: String
    public let version: String?
    public let database: Component?
    public let llm: Component?
    public let embeddings: Component?

    public struct Component: Codable, Sendable {
        public let status: String?
        public let detail: String?
    }
}

public struct DeviceRegistration: Codable, Sendable {
    public let token: String
    public let platform: String
    public let deviceName: String?
    public let appVersion: String?

    public init(token: String, platform: String = "ios", deviceName: String? = nil, appVersion: String? = nil) {
        self.token = token
        self.platform = platform
        self.deviceName = deviceName
        self.appVersion = appVersion
    }

    enum CodingKeys: String, CodingKey {
        case token, platform
        case deviceName = "device_name"
        case appVersion = "app_version"
    }
}
