import SwiftUI

struct OrderLookupView: View {
    @State private var query = ""
    @State private var result: OrderLookupResponse?
    @State private var error: String?
    @State private var busy = false

    private let headerKeys: [(String, String)] = [
        ("Customer", "customer"),
        ("Customer ID", "customer_id"),
        ("Invoice", "invoice_number"),
        ("PO", "po_number"),
        ("Amount", "order_amount"),
        ("Paid", "paid_amount"),
        ("Discount", "discount"),
        ("Shipping", "shipping_charge"),
        ("Handling", "handling_fee"),
        ("Tax", "tax_amount"),
        ("Salesman", "salesman"),
        ("Terms", "terms"),
        ("Status", "order_status"),
        ("Type", "order_type"),
        ("Priority", "priority"),
        ("Order date", "order_date"),
        ("Ship date", "ship_date"),
        ("Cancel", "cancel_date"),
        ("Ship via", "ship_method"),
        ("Tracking", "tracking_number"),
        ("Ship name", "ship_customer_name"),
        ("Address", "ship_address_1"),
        ("City", "ship_to_city"),
        ("State", "ship_to_state"),
        ("ZIP", "ship_to_zip"),
        ("Phone", "phone"),
        ("Email", "email"),
        ("Notes", "notes"),
        ("DB", "database"),
    ]

    var body: some View {
        VStack(spacing: 12) {
            FDCSearchBar(placeholder: "Order or PO", text: $query, keyboard: .numbersAndPunctuation) {
                Task { await search() }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)

            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let error { StatusBanner(text: error).padding(.horizontal, 16) }

            if let result {
                List {
                    Section {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(result.orderNumber ?? "").font(.title3.weight(.bold))
                            if let totals = result.totals {
                                Text("Qty \(totals.qty ?? 0)  ·  Boxes \(totals.boxes ?? 0)  ·  Wt \(totals.weight.map { String(format: "%g", $0) } ?? "—")")
                                    .font(.footnote)
                                    .foregroundStyle(FDCTheme.mint)
                            }
                            KVGrid(row: result.header ?? [:], keys: headerKeys)
                        }
                        .listRowBackground(FDCTheme.card)
                    }
                    Section("Lines") {
                        ForEach(Array((result.items ?? []).enumerated()), id: \.offset) { _, item in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(item.text("SKU")).font(.headline)
                                Text(item.text("Descrip")).font(.footnote).foregroundStyle(FDCTheme.muted)
                                KVGrid(row: item, keys: [
                                    ("Line", "line"),
                                    ("Qty", "Qty"),
                                    ("Price", "price"),
                                    ("Per box", "per_box"),
                                    ("Boxes", "Boxes"),
                                    ("Location", "Location"),
                                    ("Stock", "Stock"),
                                    ("Weight", "Weight"),
                                ])
                            }
                            .listRowBackground(FDCTheme.card)
                        }
                    }
                }
                .scrollContentBackground(.hidden)
            } else if !busy {
                EmptyHint(text: "Look up an order number or PO")
            }
            Spacer(minLength: 0)
        }
        .fdcScreen()
        .navigationTitle("Sales Orders")
        .navigationBarTitleDisplayMode(.inline)
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
