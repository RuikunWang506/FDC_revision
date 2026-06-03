-- Approved styles (cus_invt) toggle + manual style list for customer purchase access.
ALTER TABLE users
  ADD COLUMN customer_purchase_include_approved_styles TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN customer_purchase_manual_styles TEXT NULL DEFAULT NULL;
