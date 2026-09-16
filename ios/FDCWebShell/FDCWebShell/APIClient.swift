import Foundation

enum APIError: LocalizedError {
    case message(String)
    case http(Int)
    case notJSON(Int)

    var errorDescription: String? {
        switch self {
        case .message(let text): return text
        case .http(let code): return "Server error (\(code))"
        case .notJSON(let code):
            if code == 404 {
                return "API not on fdcef.com yet. Pull git on the office PC and restart Flask."
            }
            return "Server did not return JSON (HTTP \(code))."
        }
    }
}

final class APIClient {
    static let shared = APIClient()

    private let session: URLSession
    private(set) var csrf: String = ""

    private init() {
        let config = URLSessionConfiguration.default
        config.httpCookieStorage = .shared
        config.httpCookieAcceptPolicy = .always
        config.httpShouldSetCookies = true
        config.timeoutIntervalForRequest = 90
        session = URLSession(configuration: config)
    }

    func getSession() async throws -> MobileUser? {
        let data = try await request(path: "/api/mobile/session", method: "GET")
        let decoded = try JSONDecoder().decode(SessionResponse.self, from: data)
        if let csrf = decoded.csrf { self.csrf = csrf }
        guard decoded.ok else { throw APIError.message(decoded.error ?? "Session failed") }
        if let username = decoded.username, let tools = decoded.tools, let csrf = decoded.csrf {
            return MobileUser(username: username, csrf: csrf, tools: tools)
        }
        return nil
    }

    func login(username: String, password: String) async throws -> MobileUser {
        let body = try JSONSerialization.data(withJSONObject: [
            "username": username,
            "password": password,
            "remember": true,
        ])
        let data = try await request(path: "/api/mobile/login", method: "POST", body: body, sendCSRF: false)
        let decoded = try JSONDecoder().decode(LoginResponse.self, from: data)
        if let csrf = decoded.csrf { self.csrf = csrf }
        guard decoded.ok, let username = decoded.username, let tools = decoded.tools, let csrf = decoded.csrf else {
            throw APIError.message(decoded.error ?? "Login failed")
        }
        return MobileUser(username: username, csrf: csrf, tools: tools)
    }

    func logout() async {
        _ = try? await request(path: "/api/mobile/logout", method: "POST", sendCSRF: true)
        csrf = ""
    }

    func itemLookup(query: String) async throws -> ItemLookupResponse {
        try await get("/api/mobile/item_lookup", query: ["q": query])
    }

    func orderLookup(query: String) async throws -> OrderLookupResponse {
        try await get("/api/mobile/order_lookup", query: ["q": query])
    }

    func invoiceLookup(query: String) async throws -> InvoiceLookupResponse {
        try await get("/api/mobile/invoice_lookup", query: ["q": query])
    }

    func customerSearch(query: String) async throws -> CustomerSearchResponse {
        try await get("/api/mobile/customer", query: ["q": query])
    }

    func customerDetails(id: String, db: String) async throws -> CustomerDetailsResponse {
        try await get("/api/mobile/customer/\(id)", query: ["db": db])
    }

    func bagMeta() async throws -> BagMetaResponse {
        try await get("/api/mobile/bag_style/meta")
    }

    func bagStyles(description: String, stock: String, color: String) async throws -> BagStyleResponse {
        var q = ["description": description]
        if !stock.isEmpty { q["stock_filter"] = stock }
        if !color.isEmpty { q["color_filter"] = color }
        return try await get("/api/mobile/bag_style", query: q)
    }

    func listCreator() async throws -> ListCreatorResponse {
        try await get("/api/mobile/list_creator")
    }

    func listFilter(descriptions: [String], stock: String) async throws -> ListFilterResponse {
        var body: [String: Any] = ["descriptions": descriptions]
        if !stock.isEmpty { body["stock_filter"] = stock }
        return try await post("/api/mobile/list_creator/filter", json: body)
    }

    func masterImages(query: String) async throws -> MasterImageResponse {
        try await get("/api/mobile/images", query: ["q": query])
    }

    func uploadCatalog(files: [(filename: String, data: Data)]) async throws -> UploadPreviewResponse {
        let payload = files.map { (name: "catalog_files", filename: $0.filename, mime: "image/jpeg", data: $0.data) }
        let data = try await upload(path: "/api/mobile/upload_catalog", fields: [:], files: payload)
        return try decode(UploadPreviewResponse.self, data: data)
    }

    func processCatalog(sessionId: String, images: [UploadImage]) async throws -> PrintResultResponse {
        let payload: [[String: String]] = images.map { ["path": $0.path, "sku": $0.sku] }
        return try await post("/api/mobile/upload_catalog/process", json: [
            "session_id": sessionId,
            "images": payload,
        ])
    }

    func lorisOrders() async throws -> LorisOrdersResponse {
        try await get("/api/mobile/loris/orders")
    }

    func lorisItems(po: String) async throws -> LorisItemsResponse {
        try await get("/api/mobile/loris/orders/\(po)")
    }

    func container(query: String) async throws -> ContainerResponse {
        try await get("/api/mobile/container", query: ["q": query])
    }

    func printStatus() async throws -> PrintStatusResponse {
        try await get("/api/mobile/print/status")
    }

    func printFile(target: String, orientation: String, copies: Int, filename: String, data: Data) async throws -> PrintResultResponse {
        let mime = filename.lowercased().hasSuffix(".pdf") ? "application/pdf" : "image/jpeg"
        let raw = try await upload(
            path: "/api/mobile/print",
            fields: [
                "target": target,
                "orientation": orientation,
                "copies": String(copies),
            ],
            files: [(name: "file", filename: filename, mime: mime, data: data)]
        )
        return try decode(PrintResultResponse.self, data: raw)
    }

    func scan(sku: String? = nil, upc: String? = nil) async throws -> ScanResponse {
        var body: [String: String] = [:]
        if let sku, !sku.isEmpty { body["sku"] = sku }
        if let upc, !upc.isEmpty { body["upc"] = upc }
        return try await post("/api/mobile/scan", json: body)
    }

    func imageData(from urlString: String) async -> Data? {
        guard let url = resolvedImageURL(urlString) else { return nil }
        var req = URLRequest(url: url)
        req.setValue("image/jpeg,image/png,image/webp,image/gif,image/*,*/*", forHTTPHeaderField: "Accept")
        do {
            let (data, response) = try await session.data(for: req)
            let http = response as? HTTPURLResponse
            let code = http?.statusCode ?? 0
            let type = (http?.value(forHTTPHeaderField: "Content-Type") ?? "").lowercased()
            guard (200..<300).contains(code), !data.isEmpty else { return nil }
            if type.contains("json") || type.contains("html") { return nil }
            return data
        } catch {
            return nil
        }
    }

    func resolvedImageURL(_ raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !trimmed.lowercased().contains("no_image") else { return nil }
        if let url = URL(string: trimmed), url.scheme != nil { return url }
        if trimmed.hasPrefix("/") {
            return URL(string: trimmed, relativeTo: AppConfig.baseURL)?.absoluteURL
        }
        return URL(string: trimmed)
    }

    private func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        var comps = URLComponents(url: url(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            comps.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        let data = try await request(url: comps.url!, method: "GET")
        return try decode(T.self, data: data)
    }

    private func post<T: Decodable>(_ path: String, json: Any) async throws -> T {
        let body = try JSONSerialization.data(withJSONObject: json)
        let data = try await request(path: path, method: "POST", body: body, sendCSRF: true)
        return try decode(T.self, data: data)
    }

    private func decode<T: Decodable>(_ type: T.Type, data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let message = (obj["error"] as? String) ?? (obj["message"] as? String)
                if let message, !message.isEmpty {
                    throw APIError.message(message)
                }
            }
            throw error
        }
    }

    private func url(_ path: String) -> URL {
        AppConfig.baseURL.appending(path: path.trimmingCharacters(in: CharacterSet(charactersIn: "/")))
    }

    private func request(path: String, method: String, body: Data? = nil, sendCSRF: Bool = false) async throws -> Data {
        try await request(url: url(path), method: method, body: body, sendCSRF: sendCSRF)
    }

    private func request(url: URL, method: String, body: Data? = nil, sendCSRF: Bool = false) async throws -> Data {
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = body
        }
        if sendCSRF, !csrf.isEmpty {
            req.setValue(csrf, forHTTPHeaderField: "X-CSRF-Token")
        }
        return try await send(req)
    }

    private func upload(path: String, fields: [String: String], files: [(name: String, filename: String, mime: String, data: Data)]) async throws -> Data {
        let boundary = "fdc-\(UUID().uuidString)"
        var req = URLRequest(url: url(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        if !csrf.isEmpty {
            req.setValue(csrf, forHTTPHeaderField: "X-CSRF-Token")
        }
        var body = Data()
        for (key, value) in fields {
            body.append("--\(boundary)\r\n")
            body.append("Content-Disposition: form-data; name=\"\(key)\"\r\n\r\n")
            body.append("\(value)\r\n")
        }
        for file in files {
            body.append("--\(boundary)\r\n")
            body.append("Content-Disposition: form-data; name=\"\(file.name)\"; filename=\"\(file.filename)\"\r\n")
            body.append("Content-Type: \(file.mime)\r\n\r\n")
            body.append(file.data)
            body.append("\r\n")
        }
        body.append("--\(boundary)--\r\n")
        req.httpBody = body
        return try await send(req)
    }

    private func send(_ req: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: req)
        let http = response as? HTTPURLResponse
        let code = http?.statusCode ?? 0
        let type = (http?.value(forHTTPHeaderField: "Content-Type") ?? "").lowercased()
        let looksJSON = type.contains("json") || data.first == UInt8(ascii: "{") || data.first == UInt8(ascii: "[")
        if !looksJSON {
            throw APIError.notJSON(code)
        }
        if code == 401 {
            throw APIError.message("Login required")
        }
        if (200..<300).contains(code) || code == 400 || code == 403 || code == 404 || code == 429 {
            return data
        }
        throw APIError.http(code)
    }
}

private extension Data {
    mutating func append(_ string: String) {
        if let data = string.data(using: .utf8) {
            append(data)
        }
    }
}
