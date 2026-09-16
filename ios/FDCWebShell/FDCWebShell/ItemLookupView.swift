import SwiftUI

struct ItemLookupView: View {
    @State private var query = ""
    @State private var styles: [ItemStyleGroup] = []
    @State private var history: [[String: JSONValue]] = []
    @State private var purchases: [[String: JSONValue]] = []
    @State private var adjustments: [[String: JSONValue]] = []
    @State private var error: String?
    @State private var busy = false
    @State private var pageTab = 0

    var body: some View {
        VStack(spacing: 10) {
            FDCSearchBar(placeholder: "SKU or style", text: $query) {
                Task { await search() }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)

            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let error { StatusBanner(text: error).padding(.horizontal, 16) }

            Picker("Section", selection: $pageTab) {
                Text("Inventory").tag(0)
                Text("History").tag(1)
                Text("Purchase").tag(2)
                Text("Adjust").tag(3)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)

            if pageTab == 0 {
                if styles.isEmpty, !busy {
                    EmptyHint(text: "Search a SKU or style")
                } else {
                    ScrollView {
                        LazyVStack(spacing: 14) {
                            ForEach(styles) { style in
                                StyleLookupCard(group: style)
                            }
                        }
                        .padding(.horizontal, 12)
                        .padding(.bottom, 20)
                    }
                }
            } else {
                ScrollView {
                    moreTables
                        .padding(12)
                }
            }
        }
        .fdcScreen()
        .navigationTitle("SKU / Style")
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder
    private var moreTables: some View {
        let records: [[String: JSONValue]] = {
            switch pageTab {
            case 1: return history
            case 2: return purchases
            default: return adjustments
            }
        }()
        let headers: [String] = {
            switch pageTab {
            case 1: return ["SKU", "Order", "Date", "Customer", "Qty", "Price"]
            case 2: return ["SKU", "PO", "Date", "Qty", "DB"]
            default: return Array(records.first?.keys.sorted().prefix(6) ?? ["SKU"])
            }
        }()
        let keys: [[String]] = {
            switch pageTab {
            case 1: return [["Product Code", "SKU"], ["Order Number"], ["Order Date"], ["Customer ID"], ["Order Quantity"], ["Unit Price"]]
            case 2: return [["SKU"], ["PO/Container"], ["PO DATE"], ["Quantity"], ["Database"]]
            default: return headers.map { [$0] }
            }
        }()
        if records.isEmpty {
            EmptyHint(text: pageTab == 1 ? "No sales history" : pageTab == 2 ? "No purchase history" : "No adjustments")
        } else {
            CompactTable(
                headers: headers,
                rows: records.map { row in
                    keys.map { candidates in
                        candidates.map { row.text($0) }.first { !$0.isEmpty } ?? ""
                    }
                },
                skuWidth: 110,
                numWidth: 72
            )
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
            styles = result.styles ?? []
            history = result.history ?? []
            purchases = result.purchases ?? []
            adjustments = result.adjustments ?? []
            if styles.isEmpty { error = "No rows for \(result.query ?? q)" }
        } catch {
            self.error = error.localizedDescription
            styles = []
            history = []
            purchases = []
            adjustments = []
        }
    }
}

private struct StyleLookupCard: View {
    let group: ItemStyleGroup
    @State private var tab = 0
    @State private var warehouse = 0
    @State private var activeColors: Set<String>
    @State private var hideZero = true

    init(group: ItemStyleGroup) {
        self.group = group
        _activeColors = State(initialValue: Set((group.colors ?? []).map(\.code)))
    }

    private var products: [ItemProductRow] { group.products ?? [] }

    private var colorFiltered: [ItemProductRow] {
        products.filter { product in
            activeColors.isEmpty || activeColors.contains(product.color)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                RemoteThumb(url: group.imageURL ?? "", size: 92)
                VStack(alignment: .leading, spacing: 8) {
                    Text(group.style)
                        .font(.headline)
                    colorChips
                }
                Spacer(minLength: 0)
            }

            Picker("Pane", selection: $tab) {
                Text("Inventory").tag(0)
                Text("Location").tag(1)
                Text("Info").tag(2)
                Text("Package").tag(3)
            }
            .pickerStyle(.segmented)

            if tab == 0 {
                HStack {
                    Picker("Warehouse", selection: $warehouse) {
                        Text("Combined").tag(0)
                        Text("FDC").tag(1)
                        Text("E_F").tag(2)
                    }
                    .pickerStyle(.segmented)
                    Button(hideZero ? "Show all" : "Hide 0") { hideZero.toggle() }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(FDCTheme.cyan)
                }
                inventoryTable
            } else if tab == 1 {
                locationTable
            } else if tab == 2 {
                infoTable
            } else {
                packageBlock
            }
        }
        .padding(12)
        .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(FDCTheme.line, lineWidth: 1)
        )
    }

    private var colorChips: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 44), spacing: 4)], alignment: .leading, spacing: 4) {
            ForEach(group.colors ?? []) { chip in
                let on = activeColors.contains(chip.code)
                Button {
                    if on { activeColors.remove(chip.code) } else { activeColors.insert(chip.code) }
                } label: {
                    Text(chip.code)
                        .font(.caption2.weight(.bold))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .foregroundStyle((chip.light == true) ? Color.black : Color.white)
                        .background(Color(hex: chip.hex ?? "#888888"), in: Capsule())
                        .opacity(on ? 1 : 0.35)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var inventoryTable: some View {
        let source: [ItemProductRow] = {
            switch warehouse {
            case 1: return colorFiltered.filter { $0.isFDC && (!hideZero || $0.invInt("available") > 0) }
            case 2: return colorFiltered.filter { $0.isEF && (!hideZero || $0.invInt("available") > 0) }
            default: return []
            }
        }()
        let rows: [[String]] = {
            if warehouse == 0 {
                return combinedRows()
            }
            var lines = source.map { product in
                [
                    product.sku,
                    product.inv("stock"),
                    product.inv("sold"),
                    product.inv("available"),
                    product.inv("incoming"),
                    product.inv("case_qty"),
                    product.inv("update_date"),
                ]
            }
            let stock = source.reduce(0) { $0 + $1.invInt("stock") }
            let sold = source.reduce(0) { $0 + $1.invInt("sold") }
            let avail = source.reduce(0) { $0 + $1.invInt("available") }
            let ocean = source.reduce(0) { $0 + $1.invInt("incoming") }
            if !lines.isEmpty {
                lines.append(["Σ", "\(stock)", "\(sold)", "\(avail)", "\(ocean)", "", ""])
            }
            return lines
        }()
        let headers = warehouse == 0
            ? ["SKU", "Stock", "Sold", "Avail", "ocean"]
            : ["SKU", "Stock", "Sold", "Avail", "ocean", "Case", "Updated"]
        return CompactTable(headers: headers, rows: rows, skuWidth: 124, numWidth: warehouse == 0 ? 54 : 58)
    }

    private func combinedRows() -> [[String]] {
        var merged: [String: (stock: Int, sold: Int, avail: Int, ocean: Int)] = [:]
        var order: [String] = []
        for product in colorFiltered {
            if merged[product.sku] == nil { order.append(product.sku) }
            var cur = merged[product.sku] ?? (0, 0, 0, 0)
            cur.stock += product.invInt("stock")
            cur.sold += product.invInt("sold")
            cur.avail += product.invInt("available")
            cur.ocean += product.invInt("incoming")
            merged[product.sku] = cur
        }
        var rows = order.compactMap { sku -> [String]? in
            let v = merged[sku]!
            if hideZero && v.avail <= 0 { return nil }
            return [sku, "\(v.stock)", "\(v.sold)", "\(v.avail)", "\(v.ocean)"]
        }
        if !rows.isEmpty {
            let stock = rows.compactMap { Int($0[1]) }.reduce(0, +)
            let sold = rows.compactMap { Int($0[2]) }.reduce(0, +)
            let avail = rows.compactMap { Int($0[3]) }.reduce(0, +)
            let ocean = rows.compactMap { Int($0[4]) }.reduce(0, +)
            rows.append(["Σ", "\(stock)", "\(sold)", "\(avail)", "\(ocean)"])
        }
        return rows
    }

    private var locationTable: some View {
        var seen = Set<String>()
        let rows = colorFiltered.compactMap { product -> [String]? in
            let sku = product.sku.uppercased()
            if seen.contains(sku) { return nil }
            seen.insert(sku)
            return [
                sku,
                (product.location?.text("current_location")).ifBlank("No location"),
                (product.location?.text("update_date")).ifBlank("Never"),
            ]
        }
        return CompactTable(headers: ["SKU", "Loc", "Last"], rows: rows, skuWidth: 124, numWidth: 90)
    }

    private var infoTable: some View {
        var seen = Set<String>()
        let rows = colorFiltered.compactMap { product -> [String]? in
            let boat = (product.info?.text("description") ?? "").trimmingCharacters(in: .whitespaces)
            if boat.isEmpty || seen.contains(boat) { return nil }
            seen.insert(boat)
            let cost = product.info?.text("cost") ?? ""
            let price = product.info?.text("price") ?? ""
            return [boat, money(cost), money(price)]
        }
        return CompactTable(headers: ["Boat", "Cost", "Price"], rows: rows, skuWidth: 140, numWidth: 70)
    }

    private var packageBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            KVRow(label: "Unit wt", value: group.measurements?.pcWeights ?? "")
            KVRow(label: "Unit dim", value: group.measurements?.dimensions ?? "")
            KVRow(label: "Pkg wt", value: group.measurements?.pkgWeights ?? "")
            KVRow(label: "Pkg dim", value: group.measurements?.pkgDimensions ?? "")
        }
    }

    private func money(_ raw: String) -> String {
        guard let value = Double(raw), value != 0 else { return raw }
        return String(format: "$%.2f", value)
    }
}

private extension Optional where Wrapped == String {
    func ifBlank(_ fallback: String) -> String {
        switch self {
        case .none: return fallback
        case .some(let value): return value.trimmingCharacters(in: .whitespaces).isEmpty ? fallback : value
        }
    }
}

private extension String {
    func ifBlank(_ fallback: String) -> String {
        trimmingCharacters(in: .whitespaces).isEmpty ? fallback : self
    }
}

private extension Color {
    init(hex: String) {
        var cleaned = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        if cleaned.count == 3 {
            cleaned = cleaned.map { "\($0)\($0)" }.joined()
        }
        var value: UInt64 = 0
        Scanner(string: cleaned).scanHexInt64(&value)
        self.init(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}
