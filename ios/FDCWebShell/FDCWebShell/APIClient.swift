import Foundation

enum APIError: LocalizedError {
    case message(String)
    case http(Int)

    var errorDescription: String? {
        switch self {
        case .message(let text): return text
        case .http(let code): return "Server error (\(code))"
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
        config.timeoutIntervalForRequest = 45
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
        var comps = URLComponents(url: url("/api/mobile/item_lookup"), resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "q", value: query)]
        let data = try await request(url: comps.url!, method: "GET")
        let decoded = try JSONDecoder().decode(ItemLookupResponse.self, from: data)
        if decoded.ok { return decoded }
        throw APIError.message(decoded.error ?? "Lookup failed")
    }

    func orderLookup(query: String) async throws -> OrderLookupResponse {
        var comps = URLComponents(url: url("/api/mobile/order_lookup"), resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "q", value: query)]
        let data = try await request(url: comps.url!, method: "GET")
        let decoded = try JSONDecoder().decode(OrderLookupResponse.self, from: data)
        if decoded.ok { return decoded }
        throw APIError.message(decoded.error ?? "Order not found")
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
        let (data, response) = try await session.data(for: req)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 {
            throw APIError.message("Login required")
        }
        if (200..<300).contains(code) || code == 400 || code == 403 || code == 404 || code == 429 {
            return data
        }
        throw APIError.http(code)
    }
}
