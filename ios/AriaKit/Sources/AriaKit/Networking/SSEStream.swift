import Foundation

public struct SSEEvent: Sendable, Hashable {
    public let id: String?
    public let event: String
    public let data: String

    public init(id: String?, event: String, data: String) {
        self.id = id
        self.event = event
        self.data = data
    }
}

public enum SSE {
    /// Opens an SSE connection at the given URL and yields events as they arrive.
    /// The stream finishes when the server closes the connection, throws on transport errors,
    /// and is cancellable via Task.cancel() (which terminates the underlying data task).
    public static func stream(
        url: URL,
        headers: [String: String] = [:],
        session: URLSession = .shared
    ) -> AsyncThrowingStream<SSEEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                var req = URLRequest(url: url)
                req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                req.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
                req.timeoutInterval = 3600
                for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }

                do {
                    let (bytes, response) = try await session.bytes(for: req)
                    guard let http = response as? HTTPURLResponse,
                          (200..<300).contains(http.statusCode) else {
                        throw AriaAPIError.http(
                            status: (response as? HTTPURLResponse)?.statusCode ?? -1,
                            body: ""
                        )
                    }

                    var currentId: String?
                    var currentEvent = "message"
                    var dataLines: [String] = []

                    for try await line in bytes.lines {
                        try Task.checkCancellation()
                        if line.isEmpty {
                            if !dataLines.isEmpty {
                                continuation.yield(
                                    SSEEvent(
                                        id: currentId,
                                        event: currentEvent,
                                        data: dataLines.joined(separator: "\n")
                                    )
                                )
                                dataLines.removeAll(keepingCapacity: true)
                                currentEvent = "message"
                            }
                            continue
                        }
                        if line.first == ":" { continue }
                        if let (field, value) = parseField(line) {
                            switch field {
                            case "id": currentId = value
                            case "event": currentEvent = value
                            case "data": dataLines.append(value)
                            default: break
                            }
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

    private static func parseField(_ line: String) -> (field: String, value: String)? {
        guard let colon = line.firstIndex(of: ":") else {
            return (line, "")
        }
        let field = String(line[..<colon])
        var valueStart = line.index(after: colon)
        if valueStart < line.endIndex, line[valueStart] == " " {
            valueStart = line.index(after: valueStart)
        }
        return (field, String(line[valueStart...]))
    }
}
