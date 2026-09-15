import SwiftUI

struct FDCSearchBar: View {
    let placeholder: String
    @Binding var text: String
    var keyboard: UIKeyboardType = .default
    var submit: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(FDCTheme.muted)
            TextField(placeholder, text: $text)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(keyboard)
                .submitLabel(.search)
                .onSubmit(submit)
            if !text.isEmpty {
                Button {
                    text = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(FDCTheme.muted)
                }
            }
            Button("Search", action: submit)
                .fontWeight(.semibold)
                .foregroundStyle(FDCTheme.cyan)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(FDCTheme.line, lineWidth: 1)
        )
    }
}

struct FDCCard<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(FDCTheme.line, lineWidth: 1)
            )
    }
}

struct KVRow: View {
    let label: String
    let value: String

    var body: some View {
        if !value.isEmpty, value != "N/A" {
            HStack(alignment: .top, spacing: 10) {
                Text(label)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(FDCTheme.muted)
                    .frame(width: 92, alignment: .leading)
                Text(value)
                    .font(.subheadline)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

struct KVGrid: View {
    let row: [String: JSONValue]
    let keys: [(String, String)]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(keys, id: \.0) { label, key in
                KVRow(label: label, value: row.text(key))
            }
        }
    }
}

struct RemoteThumb: View {
    let url: String
    var size: CGFloat = 88

    var body: some View {
        AsyncImage(url: URL(string: url)) { phase in
            switch phase {
            case .success(let img):
                img.resizable().scaledToFill()
            default:
                FDCTheme.card
                    .overlay {
                        Image(systemName: "photo")
                            .foregroundStyle(FDCTheme.muted)
                    }
            }
        }
        .frame(width: size, height: size)
        .clipped()
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

struct StatusBanner: View {
    let text: String
    var kind: Kind = .error

    enum Kind { case error, info }

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(kind == .error ? Color.orange : FDCTheme.muted)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct EmptyHint: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.subheadline)
            .foregroundStyle(FDCTheme.muted)
            .multilineTextAlignment(.center)
            .padding(.top, 36)
            .frame(maxWidth: .infinity)
    }
}
