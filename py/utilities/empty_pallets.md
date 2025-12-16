# Table: empty_pallets
# Columns: location (varchar, primary key), created_at (datetime), updated_at (datetime)
# This table will store empty pallets so they appear in the list even if no items are present.
# When a pallet is saved with no items, insert or update a row in this table.
# When a pallet is deleted or receives its first item, remove it from this table.
# This file is a placeholder for the schema and logic to be implemented in pallet_detail.py.
