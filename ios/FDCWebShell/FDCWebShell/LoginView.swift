import SwiftUI

struct LoginView: View {
    @EnvironmentObject private var session: SessionStore
    @State private var username = ""
    @State private var password = ""
    @State private var busy = false

    var body: some View {
        VStack(spacing: 28) {
            Spacer()
            Text("FDC")
                .font(.system(size: 56, weight: .bold, design: .rounded))
                .foregroundStyle(
                    LinearGradient(colors: [FDCTheme.cyan, FDCTheme.mint], startPoint: .leading, endPoint: .trailing)
                )
            Text("Fashion Distribution Center")
                .font(.subheadline)
                .foregroundStyle(FDCTheme.muted)

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
                    .padding(.horizontal)
            }

            Button {
                Task {
                    busy = true
                    _ = await session.login(username: username.trimmingCharacters(in: .whitespacesAndNewlines), password: password)
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
                .background(FDCTheme.cyan, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .foregroundStyle(.black)
            }
            .disabled(busy || username.isEmpty || password.isEmpty)
            .padding(.horizontal, 24)

            Spacer()
        }
        .fdcScreen()
    }
}

private extension View {
    func fdcField() -> some View {
        self
            .padding(14)
            .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}
