-- Run once on the MySQL database used by FDC Flask (same as Config.DB_CONFIG).
-- Records last successful username/password login for display on /manage_users.

ALTER TABLE users
  ADD COLUMN last_login_at DATETIME NULL DEFAULT NULL;
