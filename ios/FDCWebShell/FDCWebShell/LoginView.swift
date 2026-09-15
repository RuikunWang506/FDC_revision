import SwiftUI

struct LoginView: View {
    @EnvironmentObject private var session: SessionStore
    @State private var username = ""
    @State private var password = ""
    @State private var busy = false

    var body: some View {
        VStack(spacing: 0) {
            Spacer()
            VStack(spacing: 10) {
                Text("FDC")
                    .font(.system(size: 52, weight: .bold, design: .rounded))
                    .foregroundStyle(
                        LinearGradient(
                            colors: [FDCTheme.cyan, FDCTheme.mint, Color(red: 54 / 255, green: 185 / 255, blue: 204 / 255)],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                    )
                Text("Fashion Distribution Center")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(FDCTheme.muted)
            }
            .padding(.bottom, 36)

            VStack(spacing: 12) {
                TextField("Username", text: $username)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .fdcField()
                SecureField("Password", text: $password)
                    .fdcField()
            }
            .padding(.horizontal, 24)

            if let message = session.errorMessage, !message.isEmpty {
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(.orange)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 28)
                    .padding(.top, 14)
            }

            Button {
                Task {
                    busy = true
                    _ = await session.login(
                        username: username.trimmingCharacters(in: .whitespacesAndNewlines),
                        password: password
                    )
                    busy = false
                }
            } label: {
                HStack {
                    if busy { ProgressView().tint(.black) }
                    Text(busy ? "Signing in…" : "Sign in")
                        .fontWeight(.semibold)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background(
                    LinearGradient(colors: [FDCTheme.cyan, FDCTheme.mint], startPoint: .leading, endPoint: .trailing),
                    in: RoundedRectangle(cornerRadius: 14, style: .continuous)
                )
                .foregroundStyle(.black)
            }
            .disabled(busy || username.isEmpty || password.isEmpty)
            .padding(.horizontal, 24)
            .padding(.top, 20)

            Spacer()
        }
        .fdcScreen()
    }
}

private extension View {
    func fdcField() -> some View {
        self
            .padding(14)
            .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(FDCTheme.line, lineWidth: 1)
            )
    }
}
