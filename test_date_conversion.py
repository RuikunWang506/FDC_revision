from datetime import datetime, timedelta

# Test the conversion for order 1942
can_dt = 82133
ord_dt = 82100
ship_dt = 82129

# Original formula: DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')
# This means: days_since_1900 - 81088 + days_from_20230101

base_1900 = datetime(1900, 1, 1)
base_2023 = datetime(2023, 1, 1)

# Calculate using the formula
days_from_1900_cancel = can_dt
days_from_1900_order = ord_dt
days_from_1900_ship = ship_dt

# Apply offset
offset = 81088
days_to_add_cancel = days_from_1900_cancel - offset
days_to_add_order = days_from_1900_order - offset
days_to_add_ship = days_from_1900_ship - offset

cancel_date = base_2023 + timedelta(days=days_to_add_cancel)
order_date = base_2023 + timedelta(days=days_to_add_order)
ship_date = base_2023 + timedelta(days=days_to_add_ship)

print(f"Order Date (82100): {order_date.strftime('%m/%d/%Y')}")
print(f"Ship Date (82129): {ship_date.strftime('%m/%d/%Y')}")
print(f"Cancel Date (82133): {cancel_date.strftime('%m/%d/%Y')}")

# Alternative: direct from 1900
print(f"\nDirect from 1900:")
print(f"Cancel Date: {(base_1900 + timedelta(days=can_dt-2)).strftime('%m/%d/%Y')}")
print(f"Order Date: {(base_1900 + timedelta(days=ord_dt-2)).strftime('%m/%d/%Y')}")
print(f"Ship Date: {(base_1900 + timedelta(days=ship_dt-2)).strftime('%m/%d/%Y')}")
