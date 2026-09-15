import SwiftUI

enum FDCTheme {
    static let navy = Color(red: 6 / 255, green: 9 / 255, blue: 14 / 255)
    static let card = Color(red: 9 / 255, green: 13 / 255, blue: 20 / 255).opacity(0.86)
    static let line = Color.white.opacity(0.10)
    static let ink = Color(red: 243 / 255, green: 246 / 255, blue: 250 / 255)
    static let cyan = Color(red: 0, green: 212 / 255, blue: 1)
    static let mint = Color(red: 28 / 255, green: 200 / 255, blue: 138 / 255)
    static let muted = Color(red: 154 / 255, green: 168 / 255, blue: 184 / 255)

    static let catalog = Color(red: 139 / 255, green: 184 / 255, blue: 212 / 255)
    static let sales = Color(red: 212 / 255, green: 176 / 255, blue: 106 / 255)
    static let inventory = Color(red: 116 / 255, green: 194 / 255, blue: 154 / 255)
    static let warehouse = Color(red: 116 / 255, green: 180 / 255, blue: 192 / 255)
    static let admin = Color(red: 196 / 255, green: 146 / 255, blue: 106 / 255)

    static func categoryColor(_ category: String) -> Color {
        switch category.lowercased() {
        case "catalog": return catalog
        case "sales": return sales
        case "inventory": return inventory
        case "warehouse": return warehouse
        case "admin": return admin
        default: return cyan
        }
    }

    static func toolIcon(_ id: String) -> String {
        switch id {
        case "item_lookup": return "barcode"
        case "bag_style_viewer": return "bag"
        case "v7_listcreator": return "list.bullet.rectangle"
        case "master_image_viewer": return "photo.on.rectangle"
        case "image_upload": return "square.and.arrow.up"
        case "order_lookup": return "shippingbox"
        case "customer": return "person.2"
        case "invoice_lookup": return "doc.text"
        case "loris_fedex": return "airplane"
        case "fdcef_container_search": return "cube.box"
        case "print_center": return "printer"
        case "mobile_scanner": return "barcode.viewfinder"
        default: return "square.grid.2x2"
        }
    }
}

extension View {
    func fdcScreen() -> some View {
        self
            .foregroundStyle(FDCTheme.ink)
            .background(FDCTheme.navy.ignoresSafeArea())
    }
}
