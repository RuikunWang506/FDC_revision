from py.order.order_search import generate_order

# Test order 1942
order_number = "1942"
header_info, items_html = generate_order(order_number)

if header_info:
    print(f"\n{'='*80}")
    print(f"Order Information for: {order_number}")
    print(f"{'='*80}\n")
    
    print(f"Order Number: {header_info['order_number']}")
    print(f"Customer: {header_info['customer']}")
    print(f"Order Date: {header_info['order_date']}")
    print(f"Ship Date: {header_info['ship_date']}")
    print(f"Cancel Date: {header_info['cancel_date']}")
    print(f"\nShipping & Status:")
    print(f"  Status: {header_info['order_status']}")
    print(f"  Priority: {header_info['priority']}")
    print(f"  Ship Method: {header_info['ship_method']}")
    print(f"  Tracking: {header_info['tracking_number']}")
    print(f"\n  Ship To Address:")
    if header_info.get('ship_customer_name'):
        print(f"    {header_info['ship_customer_name']}")
    if header_info.get('ship_address_1'):
        print(f"    {header_info['ship_address_1']}")
    if header_info.get('ship_address_2'):
        print(f"    {header_info['ship_address_2']}")
    if header_info.get('ship_address_3'):
        print(f"    {header_info['ship_address_3']}")
    else:
        print(f"    {header_info['ship_to_city']}, {header_info['ship_to_state']} {header_info['ship_to_zip']}")
    if header_info.get('ship_to_country'):
        print(f"    {header_info['ship_to_country']}")
    print(f"\n  Attention: {header_info['attention']}")
    print(f"  Phone: {header_info['phone']}")
    print(f"  Email: {header_info['email']}")
    
    if header_info.get('locations'):
        print(f"\nLocations: {', '.join(header_info['locations'])}")
else:
    print(f"No data found for order {order_number}")
