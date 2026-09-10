-- Client portal group + users.
-- Prefer the app helper seed_client_portal_group() — it hashes passwords and
-- keeps client_product_detail_restrict flags. This file only inserts missing rows
-- and does not overwrite existing Client group permissions.

INSERT INTO user_groups (name, slug, permissions, sort_order)
SELECT 'Client', 'client',
       '{"tools": ["clientview", "master_image_viewer"], "client_product_detail_restrict": true, "client_product_detail_sections": []}',
       25
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM user_groups WHERE slug = 'client');

SET @client_gid := (SELECT id FROM user_groups WHERE slug = 'client' LIMIT 1);

-- Do not INSERT plaintext passwords here. Create xingxing / mydepot via the app
-- (CLIENT_PORTAL_PASSWORD_* env vars) so bcrypt hashes are stored.
-- Existing users are left unchanged; assign groups via /manage_users.
