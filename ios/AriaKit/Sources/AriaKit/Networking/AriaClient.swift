import Foundation

public enum AriaAPIError: Error, Sendable, LocalizedError {
    case badURL
    case transport(String)
    case http(status: Int, body: String)
    case decoding(String)
    case notFound
    case conflict
    case unauthorized
    case rateLimited

    public var errorDescription: String? {
        switch self {
        case .badURL: return "Invalid URL"
        case .transport(let msg): return "Network error: \(msg)"
        case .http(let status, let body): return "HTTP \(status): \(body)"
        case .decoding(let msg): return "Decode error: \(msg)"
        case .notFound: return "Not found"
        case .conflict: return "Already exists"
        case .unauthorized: return "Unauthorized"
        case .rateLimited: return "Rate limited"
        }
    }
}

public struct AriaClient: Sendable {
    public let baseURL: URL
    public let apiKey: String?
    public let session: URLSession

    public init(baseURL: URL, apiKey: String? = nil, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.session = session
    }

    private static let isoWithFraction = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
    private static let isoPlain = Date.ISO8601FormatStyle(includingFractionalSeconds: false)

    @Sendable
    private static func decodeISODate(_ decoder: Decoder) throws -> Date {
        let container = try decoder.singleValueContainer()
        var raw = try container.decode(String.self)
        // API may return UTC dates without a timezone suffix — assume UTC.
        if !raw.hasSuffix("Z") && !raw.contains("+") && !raw.suffix(6).contains("-") {
            raw += "Z"
        }
        if let date = try? Date(raw, strategy: isoWithFraction) { return date }
        if let date = try? Date(raw, strategy: isoPlain) { return date }
        throw DecodingError.dataCorruptedError(
            in: container,
            debugDescription: "Unrecognized ISO8601 date: \(raw)"
        )
    }

    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom(Self.decodeISODate)
        return decoder
    }

    public static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }


    // MARK: - Request building

    public func request(
        method: String,
        path: String,
        query: [URLQueryItem] = [],
        body: Data? = nil,
        accept: String = "application/json"
    ) throws -> URLRequest {
        var components = URLComponents(
            url: baseURL.appendingPathComponent(path),
            resolvingAgainstBaseURL: false
        )
        if !query.isEmpty { components?.queryItems = query }
        guard let url = components?.url else { throw AriaAPIError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue(accept, forHTTPHeaderField: "Accept")
        if let body {
            req.httpBody = body
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let apiKey, !apiKey.isEmpty {
            req.setValue(apiKey, forHTTPHeaderField: "X-API-Key")
        }
        return req
    }

    public func send<T: Decodable & Sendable>(
        _ req: URLRequest,
        as type: T.Type = T.self
    ) async throws -> T {
        let (data, response) = try await session.data(for: req)
        try Self.assertOK(response: response, data: data)
        if data.isEmpty, let empty = EmptyResponse() as? T { return empty }
        do {
            return try Self.makeDecoder().decode(T.self, from: data)
        } catch {
            throw AriaAPIError.decoding("\(error) — body: \(String(data: data, encoding: .utf8) ?? "<binary>")")
        }
    }

    public func sendNoContent(_ req: URLRequest) async throws {
        let (data, response) = try await session.data(for: req)
        try Self.assertOK(response: response, data: data)
    }

    public static func assertOK(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw AriaAPIError.transport("Non-HTTP response")
        }
        switch http.statusCode {
        case 200..<300: return
        case 401, 403: throw AriaAPIError.unauthorized
        case 404: throw AriaAPIError.notFound
        case 409: throw AriaAPIError.conflict
        case 429: throw AriaAPIError.rateLimited
        default:
            let body = String(data: data, encoding: .utf8) ?? ""
            throw AriaAPIError.http(status: http.statusCode, body: body)
        }
    }
}

public struct EmptyResponse: Decodable, Sendable {
    public init() {}
}
