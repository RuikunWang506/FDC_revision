-- Per-user customer IDs for purchase access (comma-separated lists).
ALTER TABLE users
  ADD COLUMN customer_purchase_fdc_ids VARCHAR(512) NULL DEFAULT NULL,
  ADD COLUMN customer_purchase_efashion_ids VARCHAR(512) NULL DEFAULT NULL,
  ADD COLUMN customer_purchase_include_approved_styles TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN customer_purchase_manual_styles TEXT NULL DEFAULT NULL,
  ADD COLUMN master_image_viewer_customer_purchase_only TINYINT(1) NULL DEFAULT NULL,
  ADD COLUMN client_product_detail_customer_purchase_only TINYINT(1) NULL DEFAULT NULL,
  ADD COLUMN master_image_viewer_hide_path TINYINT(1) NULL DEFAULT NULL;
