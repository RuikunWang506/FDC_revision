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
    let images: [ItemImage]?
    let error: String?
}

struct ItemImage: Codable, Identifiable {
    var id: String { url }
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

enum JSONValue: Codable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

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
        }
    }

    var text: String {
        switch self {
        case .string(let s): return s
        case .number(let n):
            return n == n.rounded() ? String(Int(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        case .null: return ""
        }
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    func text(_ key: String) -> String {
        self[key]?.text ?? ""
    }
}
