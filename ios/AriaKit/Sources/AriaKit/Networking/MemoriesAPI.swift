import Foundation

public struct MemoriesAPI: Sendable {
    public let client: AriaClient

    public init(client: AriaClient) { self.client = client }

    public func search(query: String, limit: Int = 10) async throws -> [Memory] {
        let items = [
            URLQueryItem(name: "query", value: query),
            URLQueryItem(name: "limit", value: String(limit)),
        ]
        let req = try client.request(method: "GET", path: "/api/v1/memories", query: items)
        let (data, response) = try await client.session.data(for: req)
        try AriaClient.assertOK(response: response, data: data)
        let decoder = AriaClient.makeDecoder()
        if let wrapped = try? decoder.decode(MemorySearchResponse.self, from: data) {
            return wrapped.items
        }
        return try decoder.decode([Memory].self, from: data)
    }
}

public struct HealthAPI: Sendable {
    public let client: AriaClient

    public init(client: AriaClient) { self.client = client }

    public func ping() async throws -> HealthStatus {
        let req = try client.request(method: "GET", path: "/api/v1/health")
        return try await client.send(req)
    }
}

public struct DevicesAPI: Sendable {
    public let client: AriaClient

    public init(client: AriaClient) { self.client = client }

    public func register(_ registration: DeviceRegistration) async throws {
        let body = try AriaClient.makeEncoder().encode(registration)
        let req = try client.request(method: "POST", path: "/api/v1/devices", body: body)
        try await client.sendNoContent(req)
    }

    public func unregister(token: String) async throws {
        let req = try client.request(method: "DELETE", path: "/api/v1/devices/\(token)")
        try await client.sendNoContent(req)
    }
}
