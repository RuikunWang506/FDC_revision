import SwiftUI

struct OrderLookupView: View {
    @State private var query = ""
    @State private var result: OrderLookupResponse?
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("Order or PO", text: $query)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.numbersAndPunctuation)
                    .submitLabel(.search)
                    .onSubmit { Task { await search() } }
                Button("Search") { Task { await search() } }
                    .disabled(busy || query.trimmingCharacters(in: .whitespaces).isEmpty)
                    .foregroundStyle(FDCTheme.cyan)
            }
            .padding(16)

            if busy { ProgressView().tint(FDCTheme.cyan).padding() }
            if let error { Text(error).foregroundStyle(.orange).padding(.horizontal) }

            if let result {
                List {
                    Section {
                        headerBlock(result)
                    }
                    Section("Lines") {
                        ForEach(Array((result.items ?? []).enumerated()), id: \.offset) { _, item in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.text("SKU")).font(.headline)
                                Text(item.text("Descrip")).font(.footnote).foregroundStyle(FDCTheme.muted)
                                HStack {
                                    Text("Qty \(item.text("Qty"))")
                                    Spacer()
                                    Text(item.text("Location").isEmpty ? "—" : item.text("Location"))
                                }
                                .font(.subheadline)
                            }
                            .listRowBackground(FDCTheme.card)
                        }
                    }
                }
                .scrollContentBackground(.hidden)
            } else {
                Spacer()
            }
        }
        .fdcScreen()
        .navigationTitle("Sales Orders")
    }

    @ViewBuilder
    private func headerBlock(_ result: OrderLookupResponse) -> some View {
        let header = result.header ?? [:]
        VStack(alignment: .leading, spacing: 8) {
            Text(result.orderNumber ?? "").font(.title3.weight(.bold))
            Text(header.text("customer_name").isEmpty ? header.text("Customer") : header.text("customer_name"))
            Text([
                header.text("ship_to_city"),
                header.text("ship_to_state"),
                header.text("ship_to_zip"),
            ].filter { !$0.isEmpty && $0 != "N/A" }.joined(separator: ", "))
                .foregroundStyle(FDCTheme.muted)
            if let totals = result.totals {
                Text("Qty \(totals.qty ?? 0)  ·  Boxes \(totals.boxes ?? 0)  ·  Wt \(totals.weight.map { String($0) } ?? "—")")
                    .font(.footnote)
                    .foregroundStyle(FDCTheme.mint)
            }
        }
        .listRowBackground(FDCTheme.card)
    }

    private func search() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            result = try await APIClient.shared.orderLookup(query: q)
        } catch {
            self.error = error.localizedDescription
            result = nil
        }
    }
}
