import Foundation

public struct ConversationsAPI: Sendable {
    public let client: AriaClient

    public init(client: AriaClient) { self.client = client }

    public func list(limit: Int = 50) async throws -> [ConversationListItem] {
        let query = [URLQueryItem(name: "limit", value: String(limit))]
        let req = try client.request(method: "GET", path: "/api/v1/conversations", query: query)
        let data = try await rawSend(req)
        let decoder = AriaClient.makeDecoder()
        if let wrapper = try? decoder.decode(Wrapper.self, from: data) {
            return wrapper.conversations
        }
        return try decoder.decode([ConversationListItem].self, from: data)
    }

    public func get(id: String) async throws -> ConversationResponse {
        let req = try client.request(method: "GET", path: "/api/v1/conversations/\(id)")
        return try await client.send(req)
    }

    public func create(title: String? = nil) async throws -> ConversationResponse {
        struct Body: Encodable { let title: String? }
        let data = try AriaClient.makeEncoder().encode(Body(title: title))
        let req = try client.request(method: "POST", path: "/api/v1/conversations", body: data)
        return try await client.send(req)
    }

    public func delete(id: String) async throws {
        let req = try client.request(method: "DELETE", path: "/api/v1/conversations/\(id)")
        try await client.sendNoContent(req)
    }

    public func steer(id: String, content: String, interrupt: Bool = false) async throws {
        struct Body: Encodable { let content: String; let interrupt: Bool }
        let data = try AriaClient.makeEncoder().encode(Body(content: content, interrupt: interrupt))
        let req = try client.request(method: "POST", path: "/api/v1/conversations/\(id)/steer", body: data)
        let (_, response) = try await client.session.data(for: req)
        try AriaClient.assertOK(response: response, data: Data())
    }

    // MARK: - Streaming send

    public enum StreamChunk: Sendable {
        case text(String)
        case toolCall(name: String, arguments: String)
        case done
        case error(String)
    }

    public func sendStreaming(
        conversationId: String,
        content: String
    ) -> AsyncThrowingStream<StreamChunk, Error> {
        var components = URLComponents(
            url: client.baseURL.appendingPathComponent("/api/v1/conversations/\(conversationId)/messages"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [URLQueryItem(name: "stream", value: "true")]
        guard let url = components?.url else {
            return AsyncThrowingStream { $0.finish(throwing: AriaAPIError.badURL) }
        }
        let bodyObject = SendMessageRequest(content: content, stream: true)
        let body: Data
        do {
            body = try AriaClient.makeEncoder().encode(bodyObject)
        } catch {
            return AsyncThrowingStream { $0.finish(throwing: error) }
        }

        return AsyncThrowingStream { continuation in
            let task = Task {
                var req = URLRequest(url: url)
                req.httpMethod = "POST"
                req.httpBody = body
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                req.timeoutInterval = 3600
                if let key = client.apiKey, !key.isEmpty {
                    req.setValue(key, forHTTPHeaderField: "X-API-Key")
                }

                do {
                    let (bytes, response) = try await client.session.bytes(for: req)
                    guard let http = response as? HTTPURLResponse,
                          (200..<300).contains(http.statusCode) else {
                        throw AriaAPIError.http(
                            status: (response as? HTTPURLResponse)?.statusCode ?? -1,
                            body: ""
                        )
                    }
                    var currentEvent = "message"
                    var dataLines: [String] = []
                    for try await line in bytes.lines {
                        try Task.checkCancellation()
                        if line.isEmpty {
                            if !dataLines.isEmpty {
                                handleChunk(
                                    event: currentEvent,
                                    data: dataLines.joined(separator: "\n"),
                                    continuation: continuation
                                )
                                dataLines.removeAll(keepingCapacity: true)
                                currentEvent = "message"
                            }
                            continue
                        }
                        if line.first == ":" { continue }
                        if let colon = line.firstIndex(of: ":") {
                            let field = String(line[..<colon])
                            var valueStart = line.index(after: colon)
                            if valueStart < line.endIndex, line[valueStart] == " " {
                                valueStart = line.index(after: valueStart)
                            }
                            let value = String(line[valueStart...])
                            switch field {
                            case "event": currentEvent = value
                            case "data": dataLines.append(value)
                            default: break
                            }
                        }
                    }
                    continuation.yield(.done)
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

    private func handleChunk(
        event: String,
        data: String,
        continuation: AsyncThrowingStream<StreamChunk, Error>.Continuation
    ) {
        switch event {
        case "text":
            if let content = decodeContent(data) {
                continuation.yield(.text(content))
            } else {
                continuation.yield(.text(data))
            }
        case "tool_call":
            if let parsed = decodeToolCall(data) {
                continuation.yield(.toolCall(name: parsed.name, arguments: parsed.arguments))
            }
        case "error":
            continuation.yield(.error(data))
        case "done":
            continuation.yield(.done)
        default:
            break
        }
    }

    private func decodeContent(_ data: String) -> String? {
        guard let jsonData = data.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any]
        else { return nil }
        return obj["content"] as? String
    }

    private func decodeToolCall(_ data: String) -> (name: String, arguments: String)? {
        guard let jsonData = data.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any],
              let name = obj["name"] as? String
        else { return nil }
        let args: String
        if let a = obj["arguments"] as? String {
            args = a
        } else if let a = obj["arguments"] {
            args = String(describing: a)
        } else {
            args = ""
        }
        return (name, args)
    }

    private func rawSend(_ req: URLRequest) async throws -> Data {
        let (data, response) = try await client.session.data(for: req)
        try AriaClient.assertOK(response: response, data: data)
        return data
    }

    private struct Wrapper: Decodable {
        let conversations: [ConversationListItem]
    }
}
