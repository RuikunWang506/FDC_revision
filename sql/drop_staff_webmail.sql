-- Drop unused staff webmail tables (replaced by Google).
-- Loris FedEx import does not use these tables.

DROP TABLE IF EXISTS mail_message_meta;
DROP TABLE IF EXISTS mail_boat_date_overrides;
DROP TABLE IF EXISTS mail_accounts;
