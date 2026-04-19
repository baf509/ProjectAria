import Foundation
import Observation
import AriaKit

@MainActor
@Observable
final class SettingsStore {
    static let shared = SettingsStore()

    private let defaults = UserDefaults.standard
    private let keychain = KeychainStorage()

    private enum Keys {
        static let baseURL = "aria.baseURL"
        static let apiKey = "aria.apiKey"
        static let noiseFilterDefault = "aria.shells.noiseFilterDefault"
        static let terminalFont = "aria.terminal.font"
        static let terminalFontSize = "aria.terminal.fontSize"
    }

    static let defaultBaseURL = "http://corsair-ai.tailb286a5.ts.net:8000"

    var baseURLString: String {
        didSet { defaults.set(baseURLString, forKey: Keys.baseURL) }
    }

    var apiKey: String {
        didSet { keychain.setString(apiKey, for: Keys.apiKey) }
    }

    var noiseFilterDefault: Bool {
        didSet { defaults.set(noiseFilterDefault, forKey: Keys.noiseFilterDefault) }
    }

    var terminalFontName: String {
        didSet { defaults.set(terminalFontName, forKey: Keys.terminalFont) }
    }

    var terminalFontSize: Double {
        didSet { defaults.set(terminalFontSize, forKey: Keys.terminalFontSize) }
    }

    init() {
        self.baseURLString = UserDefaults.standard.string(forKey: Keys.baseURL) ?? Self.defaultBaseURL
        self.apiKey = KeychainStorage().string(for: Keys.apiKey) ?? ""
        self.noiseFilterDefault = UserDefaults.standard.bool(forKey: Keys.noiseFilterDefault)
        self.terminalFontName = UserDefaults.standard.string(forKey: Keys.terminalFont) ?? "Menlo"
        let size = UserDefaults.standard.double(forKey: Keys.terminalFontSize)
        self.terminalFontSize = size > 0 ? size : 12.0
    }

    var hasBaseURL: Bool { URL(string: baseURLString)?.scheme != nil }

    var baseURL: URL? { URL(string: baseURLString) }

    func setBaseURL(_ url: String) {
        baseURLString = url.trimmingCharacters(in: .whitespaces)
    }

    // Fresh clients per call — AriaClient is a cheap Sendable value type.
    func makeClient() -> AriaClient? {
        guard let url = baseURL else { return nil }
        return AriaClient(baseURL: url, apiKey: apiKey.isEmpty ? nil : apiKey)
    }

    func makeShells() -> ShellsAPI? {
        guard let client = makeClient() else { return nil }
        return ShellsAPI(client: client)
    }

    func makeConversations() -> ConversationsAPI? {
        guard let client = makeClient() else { return nil }
        return ConversationsAPI(client: client)
    }

    func makeMemories() -> MemoriesAPI? {
        guard let client = makeClient() else { return nil }
        return MemoriesAPI(client: client)
    }

    func makeHealth() -> HealthAPI? {
        guard let client = makeClient() else { return nil }
        return HealthAPI(client: client)
    }

    func makeDevices() -> DevicesAPI? {
        guard let client = makeClient() else { return nil }
        return DevicesAPI(client: client)
    }
}
