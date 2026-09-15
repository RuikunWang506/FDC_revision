import Foundation
import SwiftUI

@MainActor
final class SessionStore: ObservableObject {
    @Published var user: MobileUser?
    @Published var restoring = true
    @Published var errorMessage: String?

    func restore() async {
        restoring = true
        defer { restoring = false }
        do {
            user = try await APIClient.shared.getSession()
        } catch {
            user = nil
        }
    }

    func login(username: String, password: String) async -> Bool {
        errorMessage = nil
        do {
            user = try await APIClient.shared.login(username: username, password: password)
            return true
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
    }

    func logout() async {
        await APIClient.shared.logout()
        user = nil
    }
}
