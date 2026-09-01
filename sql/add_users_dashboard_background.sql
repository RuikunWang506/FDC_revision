-- Run once on the MySQL database used by FDC Flask (same as Config.DB_CONFIG).
-- Stores the signed-in user's home dashboard wallpaper id.

ALTER TABLE users
  ADD COLUMN dashboard_background VARCHAR(64) NULL DEFAULT NULL;
