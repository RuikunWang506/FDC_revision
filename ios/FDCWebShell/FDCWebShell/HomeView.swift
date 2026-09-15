import SwiftUI

struct HomeView: View {
    @EnvironmentObject private var session: SessionStore

    private var grouped: [(String, [MobileTool])] {
        let tools = session.user?.tools ?? []
        let order = ["Catalog", "Sales", "Inventory", "Warehouse", "Admin"]
        let buckets = Dictionary(grouping: tools, by: \.category)
        return order.compactMap { cat in
            guard let items = buckets[cat], !items.isEmpty else { return nil }
            return (cat, items)
        }
    }

    private let columns = [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Operations")
                            .font(.title.weight(.bold))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [FDCTheme.cyan, FDCTheme.mint],
                                    startPoint: .leading,
                                    endPoint: .trailing
                                )
                            )
                        Text(session.user?.username ?? "")
                            .font(.subheadline)
                            .foregroundStyle(FDCTheme.muted)
                    }

                    ForEach(grouped, id: \.0) { category, tools in
                        VStack(alignment: .leading, spacing: 10) {
                            Text(category.uppercased())
                                .font(.caption.weight(.semibold))
                                .tracking(0.8)
                                .foregroundStyle(FDCTheme.categoryColor(category))
                            LazyVGrid(columns: columns, spacing: 12) {
                                ForEach(tools) { tool in
                                    if tool.native {
                                        NavigationLink(value: tool) {
                                            ToolTile(tool: tool)
                                        }
                                        .buttonStyle(.plain)
                                    } else {
                                        ToolTile(tool: tool)
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(16)
            }
            .fdcScreen()
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Log out") {
                        Task { await session.logout() }
                    }
                    .foregroundStyle(FDCTheme.cyan)
                }
            }
            .navigationDestination(for: MobileTool.self) { tool in
                toolDestination(tool)
            }
        }
    }

    @ViewBuilder
    private func toolDestination(_ tool: MobileTool) -> some View {
        switch tool.id {
        case "item_lookup": ItemLookupView()
        case "order_lookup": OrderLookupView()
        case "invoice_lookup": InvoiceLookupView()
        case "customer": CustomerView()
        case "bag_style_viewer": BagStyleView()
        case "v7_listcreator": ListCreatorView()
        case "master_image_viewer": MasterImageView()
        case "image_upload": ImageUploadView()
        case "loris_fedex": LorisView()
        case "fdcef_container_search": ContainerHubView()
        case "print_center": PrintCenterView()
        case "mobile_scanner": ScannerView()
        default:
            Text("Not on iOS yet").fdcScreen()
        }
    }
}

private struct ToolTile: View {
    let tool: MobileTool

    var body: some View {
        let color = FDCTheme.categoryColor(tool.category)
        VStack(alignment: .leading, spacing: 10) {
            Image(systemName: FDCTheme.toolIcon(tool.id))
                .font(.title3.weight(.semibold))
                .foregroundStyle(tool.native ? color : FDCTheme.muted)
            Text(tool.title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.white)
                .lineLimit(3)
                .minimumScaleFactor(0.85)
            Spacer(minLength: 0)
            Text(tool.native ? "Open" : "Web only")
                .font(.caption2.weight(.medium))
                .foregroundStyle(tool.native ? color : FDCTheme.muted)
        }
        .padding(14)
        .frame(maxWidth: .infinity, minHeight: 118, alignment: .topLeading)
        .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(tool.native ? color.opacity(0.35) : FDCTheme.line, lineWidth: 1)
        )
        .opacity(tool.native ? 1 : 0.45)
    }
}
