import Foundation

struct MobileUser: Codable, Equatable {
    let username: String
    let csrf: String
    let tools: [MobileTool]
}

struct MobileTool: Codable, Identifiable, Equatable, Hashable {
    let id: String
    let title: String
    let category: String
    let native: Bool
}

struct SessionResponse: Codable {
    let ok: Bool
    let user: String?
    let username: String?
    let csrf: String?
    let tools: [MobileTool]?
    let error: String?
}

struct LoginResponse: Codable {
    let ok: Bool
    let username: String?
    let csrf: String?
    let tools: [MobileTool]?
    let error: String?
}

struct ItemLookupResponse: Codable {
    let ok: Bool
    let query: String?
    let rows: [[String: JSONValue]]?
    let history: [[String: JSONValue]]?
    let purchases: [[String: JSONValue]]?
    let adjustments: [[String: JSONValue]]?
    let images: [ItemImage]?
    let totals: ItemTotals?
    let error: String?
}

struct ItemTotals: Codable {
    let stock: Double?
    let sold: Double?
    let avail: Double?
    let ocean: Double?
}

struct ItemImage: Codable, Identifiable {
    var id: String { url + sku + filename }
    let sku: String
    let filename: String
    let url: String
}

struct OrderLookupResponse: Codable {
    let ok: Bool
    let orderNumber: String?
    let header: [String: JSONValue]?
    let items: [[String: JSONValue]]?
    let totals: OrderTotals?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, header, items, totals, error
        case orderNumber = "order_number"
    }
}

struct OrderTotals: Codable {
    let qty: Int?
    let boxes: Int?
    let weight: Double?
}

struct InvoiceLookupResponse: Codable {
    let ok: Bool
    let invoiceNumber: String?
    let header: [String: JSONValue]?
    let items: [[String: JSONValue]]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, header, items, error
        case invoiceNumber = "invoice_number"
    }
}

struct CustomerSearchResponse: Codable {
    let ok: Bool
    let customers: [[String: JSONValue]]?
    let withoutOrders: [[String: JSONValue]]?
    let errors: [String]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, customers, errors, error
        case withoutOrders = "without_orders"
    }
}

struct CustomerDetailsResponse: Codable {
    let ok: Bool?
    let success: Bool?
    let customerSummary: [String: JSONValue]?
    let customerProfile: [String: JSONValue]?
    let uniqueOrderNumbers: [JSONValue]?
    let productSummaryRows: [JSONValue]?
    let message: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, success, message, error
        case customerSummary = "customer_summary"
        case customerProfile = "customer_profile"
        case uniqueOrderNumbers = "unique_order_numbers"
        case productSummaryRows = "product_summary_rows"
    }
}

struct BagMetaResponse: Codable {
    let ok: Bool
    let descriptions: [String]?
    let error: String?
}

struct BagStyleResponse: Codable {
    let ok: Bool
    let count: Int?
    let styles: [BagStyle]?
    let error: String?
}

struct BagStyle: Codable, Identifiable {
    var id: String { style }
    let style: String
    let description: String?
    let image: String?
    let totalQuantity: Int?
    let totalOcean: Int?
    let total: Int?
    let skus: [[String: JSONValue]]?

    enum CodingKeys: String, CodingKey {
        case style, description, image, total, skus
        case totalQuantity = "total_quantity"
        case totalOcean = "total_ocean"
    }
}

struct ListCreatorResponse: Codable {
    let ok: Bool?
    let jobs: [ListJob]?
    let standardJobs: [ListJob]?
    let descriptions: [String]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, jobs, descriptions, error
        case standardJobs = "standard_jobs"
    }
}

struct ListJob: Codable, Identifiable {
    let id: String
    let status: String?
    let snapshotName: String?
    let ownerDisplay: String?
    let createdAtPtDisplay: String?
    let listInputPreview: String?
    let resolvedStyles: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case id, status, error
        case snapshotName = "snapshot_name"
        case ownerDisplay = "owner_display"
        case createdAtPtDisplay = "created_at_pt_display"
        case listInputPreview = "list_input_preview"
        case resolvedStyles = "resolved_styles"
    }
}

struct ListFilterResponse: Codable {
    let success: Bool?
    let ok: Bool?
    let styles: [String]?
    let count: Int?
    let listInput: String?
    let message: String?

    enum CodingKeys: String, CodingKey {
        case success, ok, styles, count, message
        case listInput = "list_input"
    }
}

struct MasterImageResponse: Codable {
    let ok: Bool
    let sku: String?
    let total: Int?
    let groups: [ImageGroup]?
    let error: String?
}

struct ImageGroup: Codable, Identifiable {
    let id: String
    let label: String
    let images: [MasterImage]
}

struct MasterImage: Codable, Identifiable {
    var id: String { "\(url)-\(filename)" }
    let sku: String?
    let filename: String
    let source: String?
    let url: String
    let fullURL: String?

    enum CodingKeys: String, CodingKey {
        case sku, filename, source, url
        case fullURL = "full_url"
    }
}

struct UploadPreviewResponse: Codable {
    let ok: Bool?
    let success: Bool?
    let sessionId: String?
    let images: [UploadImage]?
    let duplicates: [UploadImage]?
    let message: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, success, images, duplicates, message, error
        case sessionId = "session_id"
    }
}

struct UploadImage: Codable, Identifiable {
    var id: String { path }
    let filename: String?
    let path: String
    var sku: String
}

struct LorisOrdersResponse: Codable {
    let ok: Bool?
    let success: Bool?
    let orders: [[String: JSONValue]]?
    let totalOrders: Int?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, success, orders, error
        case totalOrders = "total_orders"
    }
}

struct LorisItemsResponse: Codable {
    let ok: Bool?
    let success: Bool?
    let items: [[String: JSONValue]]?
    let error: String?
}

struct ContainerResponse: Codable {
    let ok: Bool
    let containerId: String?
    let poDate: String?
    let isArrived: Bool?
    let items: [[String: JSONValue]]?
    let styleGroups: [JSONValue]?
    let totals: [String: JSONValue]?
    let boat: [String: JSONValue]?
    let ocean: [JSONValue]?
    let arrived: [JSONValue]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, items, totals, boat, ocean, arrived, error
        case containerId = "container_id"
        case poDate = "po_date"
        case isArrived = "is_arrived"
        case styleGroups = "style_groups"
    }
}

struct PrintStatusResponse: Codable {
    let ok: Bool?
    let printers: [PrinterStatus]?
    let error: String?
}

struct PrinterStatus: Codable, Identifiable {
    let id: String
    let label: String
    let hint: String?
    let available: Bool?
    let resolved: String?
    let error: String?
}

struct PrintResultResponse: Codable {
    let ok: Bool?
    let message: String?
    let error: String?
}

struct ScanResponse: Codable {
    let ok: Bool?
    let success: Bool?
    let sku: String?
    let matchedSku: String?
    let upc: String?
    let locations: String?
    let available: JSONValue?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, success, sku, upc, locations, available, error
        case matchedSku = "matched_sku"
    }
}

enum JSONValue: Codable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let b = try? container.decode(Bool.self) {
            self = .bool(b)
        } else if let i = try? container.decode(Int.self) {
            self = .number(Double(i))
        } else if let d = try? container.decode(Double.self) {
            self = .number(d)
        } else if let arr = try? container.decode([JSONValue].self) {
            self = .array(arr)
        } else if let obj = try? container.decode([String: JSONValue].self) {
            self = .object(obj)
        } else {
            self = .string((try? container.decode(String.self)) ?? "")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let s): try container.encode(s)
        case .number(let n): try container.encode(n)
        case .bool(let b): try container.encode(b)
        case .null: try container.encodeNil()
        case .array(let a): try container.encode(a)
        case .object(let o): try container.encode(o)
        }
    }

    var text: String {
        switch self {
        case .string(let s): return s
        case .number(let n):
            return n == n.rounded() ? String(Int(n)) : String(format: "%g", n)
        case .bool(let b): return b ? "true" : "false"
        case .null: return ""
        case .array(let a): return a.map(\.text).joined(separator: ", ")
        case .object: return ""
        }
    }

    var object: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }

    var array: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    func text(_ key: String) -> String {
        self[key]?.text ?? ""
    }
}
