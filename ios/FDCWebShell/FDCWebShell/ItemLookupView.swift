import SwiftUI

struct ItemLookupView: View {
    @State private var query = ""
    @State private var rows: [[String: JSONValue]] = []
    @State private var images: [ItemImage] = []
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("SKU or style", text: $query)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.search)
                    .onSubmit { Task { await search() } }
                Button("Search") { Task { await search() } }
                    .disabled(busy || query.trimmingCharacters(in: .whitespaces).isEmpty)
                    .foregroundStyle(FDCTheme.cyan)
            }
            .padding(16)

            if busy {
                ProgressView().tint(FDCTheme.cyan).padding()
            }
            if let error {
                Text(error).foregroundStyle(.orange).padding(.horizontal)
            }

            List {
                if !images.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(images) { image in
                                AsyncImage(url: URL(string: image.url)) { phase in
                                    switch phase {
                                    case .success(let img):
                                        img.resizable().scaledToFill()
                                    default:
                                        FDCTheme.card
                                    }
                                }
                                .frame(width: 88, height: 88)
                                .clipped()
                                .clipShape(RoundedRectangle(cornerRadius: 8))
                            }
                        }
                        .padding(.vertical, 4)
                    }
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 8, trailing: 16))
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(row.text("SKU").isEmpty ? row.text("PROD_CD") : row.text("SKU"))
                            .font(.headline)
                        HStack {
                            Text(row.text("Location").isEmpty ? "No location" : row.text("Location"))
                            Spacer()
                            Text(row.text("DB"))
                        }
                        .font(.subheadline)
                        .foregroundStyle(FDCTheme.muted)
                        if !row.text("DESCRIP").isEmpty {
                            Text(row.text("DESCRIP")).font(.footnote)
                        } else if !row.text("Descrip").isEmpty {
                            Text(row.text("Descrip")).font(.footnote)
                        }
                    }
                    .listRowBackground(FDCTheme.card)
                }
            }
            .scrollContentBackground(.hidden)
        }
        .fdcScreen()
        .navigationTitle("SKU Lookup")
    }

    private func search() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let result = try await APIClient.shared.itemLookup(query: q)
            rows = result.rows ?? []
            images = result.images ?? []
            if rows.isEmpty { error = "No rows for \(result.query ?? q)" }
        } catch {
            self.error = error.localizedDescription
            rows = []
            images = []
        }
    }
}
