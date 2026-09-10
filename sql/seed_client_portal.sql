-- Client portal group + users (same tools as legacy /tiktok TK password access).
-- Run once on production if app seed has not run yet.

INSERT INTO user_groups (name, slug, permissions, sort_order)
SELECT 'Client', 'client',
       '{"tools": ["clientview", "master_image_viewer"], "client_product_detail_restrict": true, "client_product_detail_sections": []}',
       25
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM user_groups WHERE slug = 'client');

SET @client_gid := (SELECT id FROM user_groups WHERE slug = 'client' LIMIT 1);

UPDATE user_groups
SET permissions = '{"tools": ["clientview", "master_image_viewer"]}'
WHERE slug = 'client';

INSERT INTO users (username, password, email, created_at, user_group_id)
SELECT 'xingxing', 'ucandoit', NULL, NOW(), @client_gid
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'xingxing');

INSERT INTO users (username, password, email, created_at, user_group_id)
SELECT 'mydepot', 'LA8888', NULL, NOW(), @client_gid
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'mydepot');

-- Do not overwrite group/password for existing users; assign via /manage_users.
