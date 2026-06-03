-- Per-user purchase limits and Master Image Viewer hide-path (moved from group permissions).
ALTER TABLE users
  ADD COLUMN master_image_viewer_customer_purchase_only TINYINT(1) NULL DEFAULT NULL,
  ADD COLUMN client_product_detail_customer_purchase_only TINYINT(1) NULL DEFAULT NULL,
  ADD COLUMN master_image_viewer_hide_path TINYINT(1) NULL DEFAULT NULL;
