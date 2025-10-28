from py.order.order_search import generate_order

# Test the location extraction
header_info, items_html = generate_order("35275")

if header_info:
    print("Header info keys:", list(header_info.keys()))
    print("Locations:", header_info.get('locations', []))
    print("Items HTML preview:", items_html[:200] + "..." if len(items_html) > 200 else items_html)
else:
    print("No data returned")