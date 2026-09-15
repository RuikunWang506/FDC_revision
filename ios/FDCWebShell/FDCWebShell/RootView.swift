import SwiftUI

struct RootView: View {
    @EnvironmentObject private var session: SessionStore

    var body: some View {
        Group {
            if session.restoring {
                ProgressView()
                    .tint(FDCTheme.cyan)
                    .fdcScreen()
            } else if session.user == nil {
                LoginView()
            } else {
                HomeView()
            }
        }
        .task { await session.restore() }
    }
}
