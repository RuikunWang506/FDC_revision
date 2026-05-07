-- Mail UI: accounts and optional message metadata/cache (MySQL 8+).
-- Run against the same database as Config.DB_CONFIG (e.g. onlinedb).

CREATE TABLE IF NOT EXISTS mail_accounts (
    id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    label VARCHAR(128) NOT NULL COMMENT 'Human label e.g. orders, sales',
    email VARCHAR(255) NOT NULL,
    imap_host VARCHAR(255) NOT NULL DEFAULT 'imap.fatcow.com',
    imap_port INT UNSIGNED NOT NULL DEFAULT 993,
    smtp_host VARCHAR(255) NOT NULL DEFAULT 'smtp.fatcow.com',
    smtp_port INT UNSIGNED NOT NULL DEFAULT 465,
    encrypted_password VARBINARY(4096) NOT NULL,
    inbox_folder VARCHAR(191) NOT NULL DEFAULT 'INBOX',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_mail_accounts_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS mail_message_meta (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    mail_account_id INT UNSIGNED NOT NULL,
    folder VARCHAR(191) NOT NULL DEFAULT 'INBOX',
    imap_uid BIGINT UNSIGNED NOT NULL,
    message_id VARCHAR(512) NULL,
    subject VARCHAR(1024) NULL,
    from_addr VARCHAR(512) NULL,
    date_sent DATETIME NULL,
    is_read TINYINT(1) NOT NULL DEFAULT 0,
    has_attachments TINYINT(1) NULL DEFAULT NULL,
    body_html_sanitized MEDIUMTEXT NULL,
    body_cached_at DATETIME NULL,
    container_ids_json VARCHAR(768) NULL DEFAULT NULL COMMENT 'JSON array of BIC-style IDs; filled on view/backfill',
    INDEX idx_mail_msg_account_folder (mail_account_id, folder),
    INDEX idx_mail_msg_date (mail_account_id, folder, date_sent),
    UNIQUE KEY uq_mail_msg_uid (mail_account_id, folder, imap_uid),
    CONSTRAINT fk_mail_msg_account
        FOREIGN KEY (mail_account_id) REFERENCES mail_accounts (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
