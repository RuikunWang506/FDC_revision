import SwiftUI
import UIKit

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
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
            } else {
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
        .task(id: url) { await load() }
    }

    private func load() async {
        if let cached = RemoteImageCache.store.object(forKey: url as NSString) {
            image = cached
            return
        }
        guard let data = await APIClient.shared.imageData(from: url),
              let loaded = UIImage(data: data)
        else {
            image = nil
            return
        }
        RemoteImageCache.store.setObject(loaded, forKey: url as NSString)
        image = loaded
    }
}

private enum RemoteImageCache {
    static let store = NSCache<NSString, UIImage>()
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

struct CompactTable: View {
    let headers: [String]
    let rows: [[String]]
    var skuWidth: CGFloat = 118
    var numWidth: CGFloat = 52

    var body: some View {
        ScrollView(.horizontal, showsIndicators: true) {
            VStack(alignment: .leading, spacing: 0) {
                headerRow
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    HStack(spacing: 0) {
                        ForEach(Array(row.enumerated()), id: \.offset) { col, value in
                            Text(value.isEmpty ? "—" : value)
                                .font(.caption)
                                .foregroundStyle(FDCTheme.ink)
                                .frame(width: col == 0 ? skuWidth : numWidth, alignment: col == 0 ? .leading : .trailing)
                                .lineLimit(1)
                                .minimumScaleFactor(0.7)
                        }
                    }
                    .padding(.vertical, 5)
                    .padding(.horizontal, 4)
                    .background(index % 2 == 0 ? Color.white.opacity(0.03) : Color.clear)
                }
            }
        }
    }

    private var headerRow: some View {
        HStack(spacing: 0) {
            ForEach(Array(headers.enumerated()), id: \.offset) { col, title in
                Text(title)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(FDCTheme.muted)
                    .frame(width: col == 0 ? skuWidth : numWidth, alignment: col == 0 ? .leading : .trailing)
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 4)
        .overlay(alignment: .bottom) {
            Rectangle().fill(FDCTheme.line).frame(height: 1)
        }
    }
}
