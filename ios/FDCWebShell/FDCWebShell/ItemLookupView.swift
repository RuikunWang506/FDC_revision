import SwiftUI

struct ItemLookupView: View {
    @State private var query = ""
    @State private var rows: [[String: JSONValue]] = []
    @State private var history: [[String: JSONValue]] = []
    @State private var purchases: [[String: JSONValue]] = []
    @State private var adjustments: [[String: JSONValue]] = []
    @State private var images: [ItemImage] = []
    @State private var totals: ItemTotals?
    @State private var error: String?
    @State private var busy = false
    @State private var tab = 0

    var body: some View {
        VStack(spacing: 12) {
            FDCSearchBar(placeholder: "SKU or style", text: $query) {
                Task { await search() }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)

            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let error { StatusBanner(text: error).padding(.horizontal, 16) }

            if let totals {
                HStack(spacing: 8) {
                    stat("Stock", totals.stock)
                    stat("Sold", totals.sold)
                    stat("Avail", totals.avail)
                    stat("Ocean", totals.ocean)
                }
                .padding(.horizontal, 16)
            }

            if !images.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(images) { image in
                            VStack(spacing: 4) {
                                RemoteThumb(url: image.url, size: 92)
                                Text(image.sku)
                                    .font(.caption2)
                                    .foregroundStyle(FDCTheme.muted)
                                    .lineLimit(1)
                                    .frame(width: 92)
                            }
                        }
                    }
                    .padding(.horizontal, 16)
                }
            }

            Picker("Section", selection: $tab) {
                Text("Inventory").tag(0)
                Text("History").tag(1)
                Text("Purchase").tag(2)
                Text("Adjust").tag(3)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)

            List {
                switch tab {
                case 1:
                    rowsSection(history, empty: "No sales history") { row in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(row.text("Product Code").ifEmpty(row.text("SKU"))).font(.headline)
                            KVRow(label: "Order", value: row.text("Order Number"))
                            KVRow(label: "Date", value: row.text("Order Date"))
                            KVRow(label: "Customer", value: row.text("Customer ID"))
                            KVRow(label: "Qty", value: row.text("Order Quantity"))
                            KVRow(label: "Price", value: row.text("Unit Price"))
                            KVRow(label: "DB", value: row.text("Database"))
                        }
                    }
                case 2:
                    rowsSection(purchases, empty: "No purchase history") { row in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(row.text("SKU")).font(.headline)
                            KVRow(label: "PO", value: row.text("PO/Container"))
                            KVRow(label: "Date", value: row.text("PO DATE"))
                            KVRow(label: "Qty", value: row.text("Quantity"))
                            KVRow(label: "DB", value: row.text("Database"))
                        }
                    }
                case 3:
                    rowsSection(adjustments, empty: "No adjustments") { row in
                        KVGrid(row: row, keys: row.keys.sorted().map { ($0, $0) })
                    }
                default:
                    rowsSection(rows, empty: "Search a SKU or style") { row in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(row.text("SKU")).font(.headline)
                            Text(row.text("Descrip"))
                                .font(.subheadline)
                                .foregroundStyle(FDCTheme.muted)
                            KVGrid(row: row, keys: [
                                ("Location", "Location"),
                                ("DB", "DB"),
                                ("Style", "Style"),
                                ("Color", "color"),
                                ("Stock", "Stock"),
                                ("Sold", "Sold"),
                                ("Avail", "Avail"),
                                ("Ocean", "ocean"),
                                ("Cost", "cost"),
                                ("MSRP", "MSRP"),
                                ("Case qty", "CaseQty"),
                                ("Box wt", "box_wt"),
                                ("Pc wt", "pc_wt"),
                                ("L × W × H", "len"),
                            ])
                            if !row.text("len").isEmpty {
                                Text("\(row.text("len")) × \(row.text("wid")) × \(row.text("hei"))")
                                    .font(.footnote)
                                    .foregroundStyle(FDCTheme.muted)
                            }
                            KVRow(label: "Updated", value: row.text("update dt"))
                            KVRow(label: "Created", value: row.text("create dt"))
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .listStyle(.plain)
        }
        .fdcScreen()
        .navigationTitle("SKU / Style")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func stat(_ label: String, _ value: Double?) -> some View {
        VStack(spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(FDCTheme.muted)
            Text(value.map { $0 == $0.rounded() ? String(Int($0)) : String(format: "%g", $0) } ?? "—")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(FDCTheme.mint)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    @ViewBuilder
    private func rowsSection(
        _ items: [[String: JSONValue]],
        empty: String,
        @ViewBuilder content: @escaping ([String: JSONValue]) -> some View
    ) -> some View {
        if items.isEmpty {
            EmptyHint(text: empty).listRowBackground(Color.clear)
        } else {
            ForEach(Array(items.enumerated()), id: \.offset) { _, row in
                content(row).listRowBackground(FDCTheme.card)
            }
        }
    }

    private func search() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let result = try await APIClient.shared.itemLookup(query: q)
            guard result.ok else { throw APIError.message(result.error ?? "Lookup failed") }
            rows = result.rows ?? []
            history = result.history ?? []
            purchases = result.purchases ?? []
            adjustments = result.adjustments ?? []
            images = result.images ?? []
            totals = result.totals
            if rows.isEmpty { error = "No inventory rows for \(result.query ?? q)" }
        } catch {
            self.error = error.localizedDescription
            rows = []
            history = []
            purchases = []
            adjustments = []
            images = []
            totals = nil
        }
    }
}

private extension String {
    func ifEmpty(_ other: String) -> String {
        isEmpty ? other : self
    }
}
