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

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 22) {
                    ForEach(grouped, id: \.0) { category, tools in
                        Text(category.uppercased())
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(FDCTheme.muted)
                            .padding(.horizontal, 4)
                        ForEach(tools) { tool in
                            if tool.native {
                                NavigationLink(value: tool) {
                                    ToolRow(tool: tool)
                                }
                                .buttonStyle(.plain)
                            } else {
                                ToolRow(tool: tool)
                            }
                        }
                    }
                }
                .padding(16)
            }
            .fdcScreen()
            .navigationTitle("FDC")
            .navigationBarTitleDisplayMode(.large)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Log out") {
                        Task { await session.logout() }
                    }
                    .foregroundStyle(FDCTheme.cyan)
                }
            }
            .navigationDestination(for: MobileTool.self) { tool in
                switch tool.id {
                case "item_lookup":
                    ItemLookupView()
                case "order_lookup":
                    OrderLookupView()
                default:
                    Text("Not on iOS yet").fdcScreen()
                }
            }
        }
    }
}

private struct ToolRow: View {
    let tool: MobileTool

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(tool.title)
                    .font(.headline)
                    .foregroundStyle(.white)
                Text(tool.native ? "Native" : "Not on iOS yet")
                    .font(.caption)
                    .foregroundStyle(tool.native ? FDCTheme.mint : FDCTheme.muted)
            }
            Spacer()
            Image(systemName: tool.native ? "chevron.right" : "lock.fill")
                .foregroundStyle(FDCTheme.muted)
        }
        .padding(14)
        .background(FDCTheme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .opacity(tool.native ? 1 : 0.55)
    }
}
