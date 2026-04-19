import Foundation
import Observation
import UIKit
import UserNotifications
import AriaKit

@MainActor
@Observable
final class PushRegistrar {
    var token: String = ""
    var authorized: Bool = false
    var lastError: String = ""

    weak var settings: SettingsStore?

    var tokenShort: String {
        token.isEmpty ? "—" : String(token.prefix(8)) + "…"
    }

    func requestAuthorization(forceReregister: Bool = false) async {
        let center = UNUserNotificationCenter.current()
        do {
            let granted = try await center.requestAuthorization(options: [.alert, .sound, .badge])
            authorized = granted
            if granted {
                if forceReregister { UIApplication.shared.unregisterForRemoteNotifications() }
                UIApplication.shared.registerForRemoteNotifications()
            }
        } catch {
            lastError = error.localizedDescription
        }
    }

    func didRegister(token: String) async {
        self.token = token
        lastError = ""
        guard let api = settings?.makeDevices() else { return }
        let registration = DeviceRegistration(
            token: token,
            platform: "ios",
            deviceName: UIDevice.current.name,
            appVersion: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
        )
        do {
            try await api.register(registration)
        } catch {
            lastError = "Register failed: \(error.localizedDescription)"
        }
    }
}
