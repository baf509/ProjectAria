import Foundation

public struct ShellsAPI: Sendable {
    public let client: AriaClient

    public init(client: AriaClient) { self.client = client }

    public func list(status: [ShellStatus]? = nil) async throws -> [Shell] {
        var query: [URLQueryItem] = []
        if let status, !status.isEmpty {
            query.append(URLQueryItem(name: "status", value: status.map(\.rawValue).joined(separator: ",")))
        }
        let req = try client.request(method: "GET", path: "/api/v1/shells", query: query)
        let resp: ShellListResponse = try await client.send(req)
        return resp.shells
    }

    public func get(name: String) async throws -> Shell {
        let req = try client.request(method: "GET", path: "/api/v1/shells/\(name)")
        return try await client.send(req)
    }

    public func create(_ request: ShellCreateRequest) async throws -> Shell {
        let body = try AriaClient.makeEncoder().encode(request)
        let req = try client.request(method: "POST", path: "/api/v1/shells", body: body)
        return try await client.send(req)
    }

    public func delete(name: String) async throws {
        let req = try client.request(method: "DELETE", path: "/api/v1/shells/\(name)")
        try await client.sendNoContent(req)
    }

    public func listEvents(
        name: String,
        sinceLine: Int? = nil,
        limit: Int = 500,
        kinds: [ShellEventKind]? = nil
    ) async throws -> ShellEventsResponse {
        var query: [URLQueryItem] = [URLQueryItem(name: "limit", value: String(limit))]
        if let sinceLine { query.append(URLQueryItem(name: "since_line", value: String(sinceLine))) }
        if let kinds, !kinds.isEmpty {
            query.append(URLQueryItem(name: "kinds", value: kinds.map(\.rawValue).joined(separator: ",")))
        }
        let req = try client.request(method: "GET", path: "/api/v1/shells/\(name)/events", query: query)
        return try await client.send(req)
    }

    public func snapshot(name: String) async throws -> ShellSnapshot {
        let req = try client.request(method: "GET", path: "/api/v1/shells/\(name)/snapshot")
        return try await client.send(req)
    }

    public func sendInput(name: String, _ input: ShellInputRequest) async throws -> ShellInputResponse {
        let body = try AriaClient.makeEncoder().encode(input)
        let req = try client.request(method: "POST", path: "/api/v1/shells/\(name)/input", body: body)
        return try await client.send(req)
    }

    public func setTags(name: String, tags: [String]) async throws -> Shell {
        let body = try AriaClient.makeEncoder().encode(ShellTagsUpdate(tags: tags))
        let req = try client.request(method: "POST", path: "/api/v1/shells/\(name)/tags", body: body)
        return try await client.send(req)
    }

    public func resize(name: String, cols: Int, rows: Int) async throws {
        let body = try AriaClient.makeEncoder().encode(ShellResizeRequest(cols: cols, rows: rows))
        let req = try client.request(method: "POST", path: "/api/v1/shells/\(name)/resize", body: body)
        try await client.sendNoContent(req)
    }

    public func search(query: String, limit: Int = 50) async throws -> [ShellEvent] {
        let items = [
            URLQueryItem(name: "q", value: query),
            URLQueryItem(name: "limit", value: String(limit)),
        ]
        let req = try client.request(method: "GET", path: "/api/v1/shells/search", query: items)
        let resp: ShellSearchResponse = try await client.send(req)
        return resp.events
    }

    // MARK: - Streaming

    public enum StreamEvent: Sendable {
        case event(ShellEvent)
        case status(ShellStatusUpdate)
        case heartbeat
    }

    public func stream(
        name: String,
        sinceLine: Int? = nil,
        session: URLSession = .shared
    ) -> AsyncThrowingStream<StreamEvent, Error> {
        var components = URLComponents(
            url: client.baseURL.appendingPathComponent("/api/v1/shells/\(name)/stream"),
            resolvingAgainstBaseURL: false
        )
        if let sinceLine {
            components?.queryItems = [URLQueryItem(name: "since_line", value: String(sinceLine))]
        }
        guard let url = components?.url else {
            return AsyncThrowingStream { $0.finish(throwing: AriaAPIError.badURL) }
        }

        let headers: [String: String]
        if let key = client.apiKey, !key.isEmpty {
            headers = ["X-API-Key": key]
        } else {
            headers = [:]
        }

        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let decoder = AriaClient.makeDecoder()
                    for try await sse in SSE.stream(url: url, headers: headers, session: session) {
                        try Task.checkCancellation()
                        switch sse.event {
                        case "shell_event":
                            if let data = sse.data.data(using: .utf8) {
                                let evt = try decoder.decode(ShellEvent.self, from: data)
                                continuation.yield(.event(evt))
                            }
                        case "shell_status":
                            if let data = sse.data.data(using: .utf8) {
                                let s = try decoder.decode(ShellStatusUpdate.self, from: data)
                                continuation.yield(.status(s))
                            }
                        case "heartbeat":
                            continuation.yield(.heartbeat)
                        default:
                            break
                        }
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
