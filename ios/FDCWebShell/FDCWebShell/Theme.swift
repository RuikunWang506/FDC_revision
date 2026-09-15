import SwiftUI

enum FDCTheme {
    static let navy = Color(red: 1 / 255, green: 11 / 255, blue: 29 / 255)
    static let card = Color(red: 8 / 255, green: 28 / 255, blue: 52 / 255)
    static let cyan = Color(red: 0.15, green: 0.78, blue: 0.85)
    static let mint = Color(red: 0.20, green: 0.85, blue: 0.62)
    static let muted = Color.white.opacity(0.55)
}

extension View {
    func fdcScreen() -> some View {
        self
            .foregroundStyle(.white)
            .background(FDCTheme.navy.ignoresSafeArea())
    }
}
