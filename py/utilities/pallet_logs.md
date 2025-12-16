# Table: pallet_logs
# Columns:
#   id (int, primary key, auto_increment)
#   location (varchar)
#   username (varchar)
#   action (varchar) -- e.g. 'save', 'delete', 'create', etc.
#   content (longtext) -- JSON string of the full pallet content (list of items)
#   timestamp (datetime)
#
# This table will store a log entry for every modification to a pallet, including the full content after each change.
# Add logic to pallet_detail.py to insert a row into this table on every save or delete.
