import PhotosUI
import SwiftUI
import UniformTypeIdentifiers
import VisionKit

struct InvoiceLookupView: View {
    @State private var query = ""
    @State private var result: InvoiceLookupResponse?
    @State private var error: String?
    @State private var busy = false

    private let headerKeys: [(String, String)] = [
        ("Customer", "customer_name"),
        ("Customer ID", "customer_id"),
        ("Order", "order_number"),
        ("Amount", "invoice_amount"),
        ("Paid", "paid_amount"),
        ("Tax", "invoice_tax"),
        ("Date", "invoice_date"),
        ("Processed", "process_date"),
        ("Salesman", "salesman"),
        ("Terms", "terms_description"),
        ("Warehouse", "warehouse"),
        ("Type", "invoice_type"),
        ("DB", "database"),
    ]

    var body: some View {
        VStack(spacing: 12) {
            FDCSearchBar(placeholder: "Invoice number", text: $query, keyboard: .numbersAndPunctuation) {
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
                            Text(result.invoiceNumber ?? "").font(.title3.weight(.bold))
                            KVGrid(row: result.header ?? [:], keys: headerKeys)
                        }
                        .listRowBackground(FDCTheme.card)
                    }
                    Section("Lines") {
                        ForEach(Array((result.items ?? []).enumerated()), id: \.offset) { _, item in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(item.text("sku").ifEmpty(item.text("SKU"))).font(.headline)
                                Text(item.text("description").ifEmpty(item.text("description"))).font(.footnote).foregroundStyle(FDCTheme.muted)
                                KVGrid(row: item, keys: [
                                    ("Line", "line"),
                                    ("Qty", "quantity"),
                                    ("Price", "unit_price"),
                                    ("Discount", "discount"),
                                    ("Total", "line_total"),
                                ])
                            }
                            .listRowBackground(FDCTheme.card)
                        }
                    }
                }
                .scrollContentBackground(.hidden)
            } else if !busy {
                EmptyHint(text: "Look up an invoice number")
            }
            Spacer(minLength: 0)
        }
        .fdcScreen()
        .navigationTitle("Invoice Lookup")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func search() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            result = try await APIClient.shared.invoiceLookup(query: q)
        } catch {
            self.error = error.localizedDescription
            result = nil
        }
    }
}

struct CustomerView: View {
    @State private var query = ""
    @State private var matches: [[String: JSONValue]] = []
    @State private var extras: [[String: JSONValue]] = []
    @State private var details: CustomerDetailsResponse?
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: 12) {
            FDCSearchBar(placeholder: "Customer name or ID", text: $query) {
                Task { await search() }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let error { StatusBanner(text: error).padding(.horizontal, 16) }

            List {
                if let details {
                    Section("Summary") {
                        KVGrid(row: details.customerSummary ?? [:], keys: [
                            ("Name", "customer_name"),
                            ("ID", "customer_id"),
                            ("Orders", "total_orders"),
                            ("SKUs", "total_skus"),
                            ("Qty", "total_qty"),
                            ("Revenue", "total_revenue"),
                            ("AOV", "avg_order_value"),
                            ("First", "first_order_date"),
                            ("Last", "last_order_date"),
                            ("DB", "database"),
                        ])
                        .listRowBackground(FDCTheme.card)
                    }
                    Section("Profile") {
                        KVGrid(row: details.customerProfile ?? [:], keys: (details.customerProfile ?? [:]).keys.sorted().map { ($0, $0) })
                            .listRowBackground(FDCTheme.card)
                    }
                    Section("Orders") {
                        ForEach(Array((details.uniqueOrderNumbers ?? []).enumerated()), id: \.offset) { _, value in
                            if let row = value.object {
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(row.text("order_number")).font(.headline)
                                    Text("\(row.text("date"))  ·  \(row.text("po_number"))")
                                        .font(.footnote)
                                        .foregroundStyle(FDCTheme.muted)
                                    Text("Inv \(row.text("invoice_number"))  ·  $\(row.text("order_amount"))")
                                        .font(.subheadline)
                                }
                                .listRowBackground(FDCTheme.card)
                            }
                        }
                    }
                } else {
                    Section("With orders") {
                        if matches.isEmpty {
                            EmptyHint(text: "Search a customer").listRowBackground(Color.clear)
                        }
                        ForEach(Array(matches.enumerated()), id: \.offset) { _, row in
                            Button {
                                Task { await open(row) }
                            } label: {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(row.text("cus_nm")).foregroundStyle(.white)
                                    Text("\(row.text("cus_id"))  ·  \(row.text("source_label"))  ·  last \(row.text("formatted_ord_dt"))")
                                        .font(.footnote)
                                        .foregroundStyle(FDCTheme.muted)
                                }
                            }
                            .listRowBackground(FDCTheme.card)
                        }
                    }
                    if !extras.isEmpty {
                        Section("No orders") {
                            ForEach(Array(extras.enumerated()), id: \.offset) { _, row in
                                Button {
                                    Task { await open(row) }
                                } label: {
                                    Text("\(row.text("cus_nm"))  ·  \(row.text("cus_id"))")
                                        .foregroundStyle(.white)
                                }
                                .listRowBackground(FDCTheme.card)
                            }
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
        }
        .fdcScreen()
        .navigationTitle("Customers")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if details != nil {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Results") { details = nil }
                        .foregroundStyle(FDCTheme.cyan)
                }
            }
        }
    }

    private func search() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        busy = true
        error = nil
        details = nil
        defer { busy = false }
        do {
            let result = try await APIClient.shared.customerSearch(query: q)
            matches = result.customers ?? []
            extras = result.withoutOrders ?? []
            if matches.isEmpty && extras.isEmpty { error = "No customers found" }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func open(_ row: [String: JSONValue]) async {
        busy = true
        defer { busy = false }
        do {
            details = try await APIClient.shared.customerDetails(id: row.text("cus_id"), db: row.text("database_origin"))
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct BagStyleView: View {
    @State private var descriptions: [String] = []
    @State private var selected = ""
    @State private var stock = ""
    @State private var color = ""
    @State private var styles: [BagStyle] = []
    @State private var count = 0
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: 12) {
            Picker("Description", selection: $selected) {
                Text("Choose description").tag("")
                ForEach(descriptions, id: \.self) { Text($0).tag($0) }
            }
            .pickerStyle(.menu)
            .padding(.horizontal, 16)
            .padding(.top, 8)

            HStack {
                Picker("Stock", selection: $stock) {
                    Text("All stock").tag("")
                    Text("In stock").tag("instock")
                    Text("Ocean").tag("ocean")
                    Text("Either").tag("both")
                }
                TextField("Color", text: $color)
                    .textInputAutocapitalization(.characters)
                    .fdcMiniField()
            }
            .padding(.horizontal, 16)

            Button("Load styles") { Task { await load() } }
                .fontWeight(.semibold)
                .foregroundStyle(FDCTheme.cyan)
                .disabled(selected.isEmpty || busy)

            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let error { StatusBanner(text: error).padding(.horizontal, 16) }
            if count > 0 {
                Text("\(styles.count) of \(count) styles")
                    .font(.caption)
                    .foregroundStyle(FDCTheme.muted)
            }

            List(styles) { style in
                HStack(alignment: .top, spacing: 12) {
                    RemoteThumb(url: style.image ?? "")
                    VStack(alignment: .leading, spacing: 4) {
                        Text(style.style).font(.headline)
                        Text(style.description ?? "").font(.footnote).foregroundStyle(FDCTheme.muted)
                        Text("Avail \(style.totalQuantity ?? 0)  ·  Ocean \(style.totalOcean ?? 0)")
                            .font(.caption)
                            .foregroundStyle(FDCTheme.mint)
                        ForEach(Array((style.skus ?? []).prefix(6).enumerated()), id: \.offset) { _, sku in
                            Text("\(sku.text("sku").ifEmpty(sku.text("SKU")))  \(sku.text("color"))  qty \(sku.text("quantity"))")
                                .font(.caption2)
                                .foregroundStyle(FDCTheme.muted)
                        }
                    }
                }
                .listRowBackground(FDCTheme.card)
            }
            .scrollContentBackground(.hidden)
        }
        .fdcScreen()
        .navigationTitle("Bag Styles")
        .navigationBarTitleDisplayMode(.inline)
        .task { await loadMeta() }
    }

    private func loadMeta() async {
        do {
            let meta = try await APIClient.shared.bagMeta()
            descriptions = meta.descriptions ?? []
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func load() async {
        guard !selected.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let result = try await APIClient.shared.bagStyles(description: selected, stock: stock, color: color)
            styles = result.styles ?? []
            count = result.count ?? styles.count
            if styles.isEmpty { error = "No styles for that filter" }
        } catch {
            self.error = error.localizedDescription
            styles = []
        }
    }
}

struct ListCreatorView: View {
    @State private var descriptions: [String] = []
    @State private var selected = Set<String>()
    @State private var stock = ""
    @State private var jobs: [ListJob] = []
    @State private var standard: [ListJob] = []
    @State private var styles: [String] = []
    @State private var listInput = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        List {
            Section("Quick filter") {
                Picker("Stock", selection: $stock) {
                    Text("All").tag("")
                    Text("In stock").tag("instock")
                    Text("Ocean").tag("ocean")
                }
                ForEach(descriptions, id: \.self) { desc in
                    Button {
                        if selected.contains(desc) { selected.remove(desc) } else { selected.insert(desc) }
                    } label: {
                        HStack {
                            Text(desc).foregroundStyle(.white)
                            Spacer()
                            if selected.contains(desc) {
                                Image(systemName: "checkmark").foregroundStyle(FDCTheme.mint)
                            }
                        }
                    }
                    .listRowBackground(FDCTheme.card)
                }
                Button("Build style list") { Task { await filter() } }
                    .foregroundStyle(FDCTheme.cyan)
                    .disabled(selected.isEmpty || busy)
                if !listInput.isEmpty {
                    Text("\(styles.count) styles")
                        .foregroundStyle(FDCTheme.mint)
                    Text(listInput)
                        .font(.footnote)
                        .textSelection(.enabled)
                }
            }
            Section("Standard snapshots") {
                jobRows(standard)
            }
            Section("Recent jobs") {
                jobRows(jobs)
            }
        }
        .scrollContentBackground(.hidden)
        .fdcScreen()
        .navigationTitle("List Creator")
        .navigationBarTitleDisplayMode(.inline)
        .overlay { if busy { ProgressView().tint(FDCTheme.cyan) } }
        .task { await load() }
    }

    @ViewBuilder
    private func jobRows(_ items: [ListJob]) -> some View {
        if items.isEmpty {
            Text("None").foregroundStyle(FDCTheme.muted)
        }
        ForEach(items) { job in
            VStack(alignment: .leading, spacing: 4) {
                Text(job.snapshotName?.ifEmpty(job.id) ?? job.id).font(.headline)
                Text("\(job.status ?? "")  ·  \(job.ownerDisplay ?? "")  ·  \(job.createdAtPtDisplay ?? "")")
                    .font(.caption)
                    .foregroundStyle(FDCTheme.muted)
                if let preview = job.listInputPreview, !preview.isEmpty {
                    Text(preview).font(.caption2).lineLimit(2)
                }
            }
            .listRowBackground(FDCTheme.card)
        }
    }

    private func load() async {
        busy = true
        defer { busy = false }
        do {
            let result = try await APIClient.shared.listCreator()
            descriptions = result.descriptions ?? []
            jobs = result.jobs ?? []
            standard = result.standardJobs ?? []
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func filter() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            let result = try await APIClient.shared.listFilter(descriptions: Array(selected), stock: stock)
            styles = result.styles ?? []
            listInput = result.listInput ?? styles.joined(separator: ",")
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct MasterImageView: View {
    @State private var query = ""
    @State private var groups: [ImageGroup] = []
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: 12) {
            FDCSearchBar(placeholder: "SKU or style", text: $query) {
                Task { await search() }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let error { StatusBanner(text: error).padding(.horizontal, 16) }
            if groups.isEmpty && !busy {
                EmptyHint(text: "Search the DAM by SKU or style")
            }
            List {
                ForEach(groups) { group in
                    Section(group.label) {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 96), spacing: 8)], spacing: 8) {
                            ForEach(group.images) { image in
                                VStack(spacing: 4) {
                                    RemoteThumb(url: image.url, size: 96)
                                    Text(image.filename).font(.caption2).lineLimit(1)
                                }
                            }
                        }
                        .listRowBackground(Color.clear)
                    }
                }
            }
            .scrollContentBackground(.hidden)
        }
        .fdcScreen()
        .navigationTitle("Master Images")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func search() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let result = try await APIClient.shared.masterImages(query: q)
            groups = result.groups ?? []
            if groups.isEmpty { error = "No images for \(q)" }
        } catch {
            self.error = error.localizedDescription
            groups = []
        }
    }
}

struct ImageUploadView: View {
    @State private var picks: [PhotosPickerItem] = []
    @State private var preview: UploadPreviewResponse?
    @State private var images: [UploadImage] = []
    @State private var message: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: 16) {
            PhotosPicker(selection: $picks, maxSelectionCount: 20, matching: .images) {
                Label("Choose catalog photos", systemImage: "photo.on.rectangle")
                    .frame(maxWidth: .infinity)
                    .padding(14)
                    .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
            .padding(.horizontal, 16)
            .padding(.top, 12)
            .onChange(of: picks) { items in
                Task { await upload(items) }
            }

            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let message { StatusBanner(text: message, kind: .info).padding(.horizontal, 16) }

            List {
                ForEach($images) { $image in
                    HStack {
                        Text(image.filename ?? image.path).font(.footnote)
                        TextField("SKU", text: $image.sku)
                            .textInputAutocapitalization(.characters)
                            .fdcMiniField()
                    }
                    .listRowBackground(FDCTheme.card)
                }
            }
            .scrollContentBackground(.hidden)

            if let sessionId = preview?.sessionId, !images.isEmpty {
                Button("Process upload") {
                    Task { await process(sessionId) }
                }
                .fontWeight(.semibold)
                .foregroundStyle(.black)
                .frame(maxWidth: .infinity)
                .padding(14)
                .background(FDCTheme.mint, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                .padding(.horizontal, 16)
                .padding(.bottom, 16)
            }
        }
        .fdcScreen()
        .navigationTitle("Smart Upload")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func upload(_ items: [PhotosPickerItem]) async {
        guard !items.isEmpty else { return }
        busy = true
        defer { busy = false }
        do {
            var files: [(filename: String, data: Data)] = []
            for (idx, item) in items.enumerated() {
                if let picked = try await item.loadTransferable(type: PickedImage.self) {
                    files.append((filename: "photo-\(idx + 1).jpg", data: picked.data))
                }
            }
            let result = try await APIClient.shared.uploadCatalog(files: files)
            preview = result
            images = result.images ?? []
            message = result.message ?? "Review SKUs, then process."
        } catch {
            message = error.localizedDescription
        }
    }

    private func process(_ sessionId: String) async {
        busy = true
        defer { busy = false }
        do {
            let result = try await APIClient.shared.processCatalog(sessionId: sessionId, images: images)
            message = result.message ?? "Processed"
        } catch {
            message = error.localizedDescription
        }
    }
}

struct LorisView: View {
    @State private var orders: [[String: JSONValue]] = []
    @State private var items: [[String: JSONValue]] = []
    @State private var selectedPO = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        List {
            if let error { StatusBanner(text: error).listRowBackground(Color.clear) }
            Section("Orders") {
                ForEach(Array(orders.enumerated()), id: \.offset) { _, order in
                    Button {
                        Task { await open(order.text("po_number")) }
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(order.text("po_number")).font(.headline).foregroundStyle(.white)
                            Text("\(order.text("status"))  ·  qty \(order.text("total_qty"))  ·  \(order.text("batch_date"))")
                                .font(.footnote)
                                .foregroundStyle(FDCTheme.muted)
                            if !order.text("tracking").isEmpty {
                                Text(order.text("tracking")).font(.caption).foregroundStyle(FDCTheme.cyan)
                            }
                        }
                    }
                    .listRowBackground(FDCTheme.card)
                }
            }
            if !items.isEmpty {
                Section(selectedPO) {
                    ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.text("vendor_item")).font(.headline)
                            Text(item.text("description")).font(.footnote).foregroundStyle(FDCTheme.muted)
                            Text("Color \(item.text("color"))  ·  Qty \(item.text("quantity"))  ·  \(item.text("unit_price"))")
                                .font(.caption)
                        }
                        .listRowBackground(FDCTheme.card)
                    }
                }
            }
        }
        .scrollContentBackground(.hidden)
        .fdcScreen()
        .navigationTitle("Loris")
        .navigationBarTitleDisplayMode(.inline)
        .overlay { if busy { ProgressView().tint(FDCTheme.cyan) } }
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        busy = true
        defer { busy = false }
        do {
            let result = try await APIClient.shared.lorisOrders()
            orders = result.orders ?? []
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func open(_ po: String) async {
        selectedPO = po
        busy = true
        defer { busy = false }
        do {
            let result = try await APIClient.shared.lorisItems(po: po)
            items = result.items ?? []
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct ContainerHubView: View {
    @State private var query = ""
    @State private var result: ContainerResponse?
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: 12) {
            FDCSearchBar(placeholder: "Container / PO", text: $query) {
                Task { await search() }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let error { StatusBanner(text: error).padding(.horizontal, 16) }
            List {
                if let ocean = result?.ocean, !ocean.isEmpty {
                    Section("Ocean") { chipSection(ocean) }
                }
                if let arrived = result?.arrived, !arrived.isEmpty {
                    Section("Arrived") { chipSection(arrived) }
                }
                if let result {
                    Section(result.containerId?.ifEmpty("Container") ?? "Container") {
                        KVRow(label: "PO date", value: result.poDate ?? "")
                        KVRow(label: "Status", value: (result.isArrived == true) ? "Arrived" : "Ocean")
                        KVGrid(row: result.totals ?? [:], keys: [
                            ("Qty", "container_qty"),
                            ("Avail", "available"),
                            ("Warehouse", "in_warehouse"),
                            ("Sold", "sold_reserved"),
                        ])
                        KVGrid(row: result.boat ?? [:], keys: (result.boat ?? [:]).keys.sorted().prefix(8).map { ($0, $0) })
                    }
                    .listRowBackground(FDCTheme.card)
                    Section("SKUs") {
                        ForEach(Array((result.items ?? []).enumerated()), id: \.offset) { _, item in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.text("sku")).font(.headline)
                                Text("\(item.text("style"))  ·  \(item.text("color"))  ·  qty \(item.text("container_qty"))")
                                    .font(.footnote)
                                    .foregroundStyle(FDCTheme.muted)
                                Text("Avail \(item.text("available"))  ·  WH \(item.text("in_warehouse"))  ·  Sold \(item.text("sold_reserved"))")
                                    .font(.caption)
                                    .foregroundStyle(FDCTheme.mint)
                            }
                            .listRowBackground(FDCTheme.card)
                        }
                    }
                } else if !busy {
                    EmptyHint(text: "Search a container PO").listRowBackground(Color.clear)
                }
            }
            .scrollContentBackground(.hidden)
        }
        .fdcScreen()
        .navigationTitle("Container Hub")
        .navigationBarTitleDisplayMode(.inline)
        .task { await search(allowEmpty: true) }
    }

    private func chipSection(_ values: [JSONValue]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack {
                ForEach(Array(values.enumerated()), id: \.offset) { _, value in
                    let id = value.object?["id"]?.text ?? value.text
                    Button(id) {
                        query = id
                        Task { await search() }
                    }
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(FDCTheme.card, in: Capsule())
                    .foregroundStyle(FDCTheme.cyan)
                }
            }
        }
        .listRowBackground(Color.clear)
    }

    private func search(allowEmpty: Bool = false) async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if q.isEmpty && !allowEmpty { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            result = try await APIClient.shared.container(query: q)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct PrintCenterView: View {
    @State private var printers: [PrinterStatus] = []
    @State private var target = "warehouse"
    @State private var orientation = "landscape"
    @State private var copies = 1
    @State private var message: String?
    @State private var busy = false
    @State private var photoItem: PhotosPickerItem?
    @State private var pickingFile = false

    var body: some View {
        List {
            Section("Printer") {
                ForEach(printers) { printer in
                    Button {
                        target = printer.id
                    } label: {
                        HStack {
                            VStack(alignment: .leading) {
                                Text(printer.label).foregroundStyle(.white)
                                Text(printer.available == true ? (printer.resolved ?? "Ready") : (printer.error ?? "Offline"))
                                    .font(.caption)
                                    .foregroundStyle(printer.available == true ? FDCTheme.mint : .orange)
                            }
                            Spacer()
                            if target == printer.id {
                                Image(systemName: "checkmark").foregroundStyle(FDCTheme.cyan)
                            }
                        }
                    }
                    .listRowBackground(FDCTheme.card)
                }
            }
            Section("Options") {
                Picker("Orientation", selection: $orientation) {
                    Text("Landscape").tag("landscape")
                    Text("Portrait").tag("portrait")
                }
                Stepper("Copies \(copies)", value: $copies, in: 1...20)
            }
            Section("File") {
                PhotosPicker(selection: $photoItem, matching: .images) {
                    Label("Print a photo", systemImage: "photo")
                }
                Button {
                    pickingFile = true
                } label: {
                    Label("Print a PDF", systemImage: "doc")
                }
            }
            if let message {
                Section { StatusBanner(text: message, kind: .info) }
            }
        }
        .scrollContentBackground(.hidden)
        .fdcScreen()
        .navigationTitle("打印中心")
        .navigationBarTitleDisplayMode(.inline)
        .overlay { if busy { ProgressView().tint(FDCTheme.cyan) } }
        .task { await load() }
        .onChange(of: photoItem) { item in
            guard let item else { return }
            Task { await printPhoto(item) }
        }
        .fileImporter(isPresented: $pickingFile, allowedContentTypes: [.pdf]) { result in
            if case .success(let url) = result {
                Task { await printPDF(url) }
            }
        }
    }

    private func load() async {
        do {
            let result = try await APIClient.shared.printStatus()
            printers = result.printers ?? []
        } catch {
            message = error.localizedDescription
        }
    }

    private func printPhoto(_ item: PhotosPickerItem) async {
        busy = true
        defer { busy = false }
        do {
            guard let picked = try await item.loadTransferable(type: PickedImage.self) else { return }
            let result = try await APIClient.shared.printFile(
                target: target,
                orientation: orientation,
                copies: copies,
                filename: "photo.jpg",
                data: picked.data
            )
            message = result.message ?? result.error
        } catch {
            message = error.localizedDescription
        }
    }

    private func printPDF(_ url: URL) async {
        busy = true
        defer { busy = false }
        do {
            let access = url.startAccessingSecurityScopedResource()
            defer { if access { url.stopAccessingSecurityScopedResource() } }
            let data = try Data(contentsOf: url)
            let result = try await APIClient.shared.printFile(
                target: target,
                orientation: orientation,
                copies: copies,
                filename: url.lastPathComponent,
                data: data
            )
            message = result.message ?? result.error
        } catch {
            message = error.localizedDescription
        }
    }
}

struct ScannerView: View {
    @State private var query = ""
    @State private var result: ScanResponse?
    @State private var error: String?
    @State private var scanning = false
    @State private var busy = false

    var body: some View {
        VStack(spacing: 12) {
            FDCSearchBar(placeholder: "SKU or 13-digit UPC", text: $query) {
                Task { await lookup() }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)

            Button {
                scanning = true
            } label: {
                Label("Scan barcode", systemImage: "barcode.viewfinder")
                    .frame(maxWidth: .infinity)
                    .padding(14)
                    .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
            .foregroundStyle(FDCTheme.cyan)
            .padding(.horizontal, 16)

            if busy { ProgressView().tint(FDCTheme.cyan) }
            if let error { StatusBanner(text: error).padding(.horizontal, 16) }

            if let result {
                FDCCard {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(result.matchedSku ?? result.sku ?? "").font(.title3.weight(.bold))
                        KVRow(label: "UPC", value: result.upc ?? "")
                        KVRow(label: "Location", value: result.locations ?? "")
                        KVRow(label: "Available", value: result.available?.text ?? "")
                    }
                }
                .padding(.horizontal, 16)
            } else {
                EmptyHint(text: "Scan a warehouse barcode or type a SKU")
            }
            Spacer()
        }
        .fdcScreen()
        .navigationTitle("Barcode Scanner")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $scanning) {
            BarcodeScanner { code in
                scanning = false
                query = code
                Task { await lookup() }
            }
            .ignoresSafeArea()
        }
    }

    private func lookup() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let digits = q.filter(\.isNumber)
            if digits.count == 13 && q.allSatisfy(\.isNumber) {
                result = try await APIClient.shared.scan(upc: digits)
            } else {
                result = try await APIClient.shared.scan(sku: q)
            }
        } catch {
            self.error = error.localizedDescription
            result = nil
        }
    }
}

private struct BarcodeScanner: UIViewControllerRepresentable {
    let onCode: (String) -> Void

    func makeUIViewController(context: Context) -> ScannerHost {
        ScannerHost(onCode: onCode)
    }

    func updateUIViewController(_ uiViewController: ScannerHost, context: Context) {}
}

final class ScannerHost: UIViewController, DataScannerViewControllerDelegate {
    let onCode: (String) -> Void
    private var started = false

    init(onCode: @escaping (String) -> Void) {
        self.onCode = onCode
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { nil }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        guard !started else { return }
        started = true
        let scanner = DataScannerViewController(
            recognizedDataTypes: [.barcode()],
            qualityLevel: .fast,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isHighlightingEnabled: true
        )
        scanner.delegate = self
        addChild(scanner)
        scanner.view.frame = view.bounds
        scanner.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(scanner.view)
        scanner.didMove(toParent: self)
        try? scanner.startScanning()
    }

    func dataScanner(_ dataScanner: DataScannerViewController, didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]) {
        emit(addedItems, from: dataScanner)
    }

    func dataScanner(_ dataScanner: DataScannerViewController, didTapOn item: RecognizedItem) {
        emit([item], from: dataScanner)
    }

    private func emit(_ items: [RecognizedItem], from scanner: DataScannerViewController) {
        for item in items {
            if case .barcode(let barcode) = item, let value = barcode.payloadStringValue, !value.isEmpty {
                scanner.stopScanning()
                onCode(value)
                return
            }
        }
    }
}

private struct PickedImage: Transferable {
    let data: Data

    static var transferRepresentation: some TransferRepresentation {
        DataRepresentation(importedContentType: .jpeg) { PickedImage(data: $0) }
        DataRepresentation(importedContentType: .png) { PickedImage(data: $0) }
        DataRepresentation(importedContentType: .heic) { PickedImage(data: $0) }
        DataRepresentation(importedContentType: .image) { PickedImage(data: $0) }
    }
}

private extension View {
    func fdcMiniField() -> some View {
        self
            .padding(8)
            .background(FDCTheme.navy, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private extension String {
    func ifEmpty(_ other: String) -> String {
        isEmpty ? other : self
    }
}
