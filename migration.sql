-- ==========================================
-- 記帳米粒 中控後台 - 資料庫遷移腳本
-- 部署前請先在你的 MySQL（jizhang_mili）執行這支 SQL 一次
-- 用法：mysql -h <host> -u <user> -p jizhang_mili < migration.sql
-- ==========================================

-- 1. 機器人全域設定（目前只有 bot_enabled 一筆，之後要加其他全域開關也可以沿用這張表）
CREATE TABLE IF NOT EXISTS bot_settings (
    `key`   VARCHAR(64) PRIMARY KEY,
    `value` VARCHAR(255) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO bot_settings (`key`, `value`) VALUES ('bot_enabled', '1');

-- 2. 關鍵字回覆（取代原本寫死在程式碼裡的 SPECIFIC_KEYWORDS）
CREATE TABLE IF NOT EXISTS keyword_replies (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    keyword    VARCHAR(100) NOT NULL UNIQUE,
    reply_text TEXT NOT NULL,
    enabled    TINYINT(1) NOT NULL DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. 敏感詞（取代原本寫死在程式碼裡的 SENSITIVE_KEYWORDS）
CREATE TABLE IF NOT EXISTS sensitive_words (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    word       VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. 錯誤紀錄（給中控後台的錯誤監控頁面用）
CREATE TABLE IF NOT EXISTS error_logs (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    source     VARCHAR(100),
    message    TEXT,
    target_id  VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 完成後，第一次啟動 main.py 時，程式會自動把原本寫死的
-- SPECIFIC_KEYWORDS / SENSITIVE_KEYWORDS 內容灌進 keyword_replies / sensitive_words，
-- 之後這兩張表就是唯一真實來源，全部改在中控後台線上操作即可。
