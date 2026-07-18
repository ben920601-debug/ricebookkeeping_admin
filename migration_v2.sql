-- ==========================================
-- 記帳米粒 中控後台 v2 功能優化 - 資料庫遷移腳本
-- 部署前請先在 v1 的 migration.sql 執行過的基礎上，再執行這支一次
-- 用法：mysql -h <host> -u <user> -p RiceBookkeeping < migration_v2.sql
-- ==========================================

-- 1. 敏感詞新增「細項說明」欄位
ALTER TABLE sensitive_words ADD COLUMN description VARCHAR(255) NOT NULL DEFAULT '';

-- 2. 機器人統計事件（回覆則數、敏感詞觸發則數，給後台總覽頁用）
CREATE TABLE IF NOT EXISTS stat_events (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(30) NOT NULL,   -- 'reply'（成功回覆一次）｜ 'sensitive_block'（觸發敏感詞攔截一次）
    target_id  VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_event_type_created (event_type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. 廣播訊息紀錄（審計用：誰在什麼時候廣播給誰、成功或失敗）
CREATE TABLE IF NOT EXISTS broadcast_logs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    target_user_id  VARCHAR(100) NOT NULL,
    target_name     VARCHAR(100),
    message         TEXT NOT NULL,
    sent_by         VARCHAR(64),
    status          VARCHAR(20) NOT NULL,   -- 'success' | 'failed'
    error_detail    TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. 機器人關閉時的自訂回覆訊息（沿用 bot_settings 表，多一筆 key）
INSERT IGNORE INTO bot_settings (`key`, `value`) VALUES ('maintenance_message', '🤖 系統維護中，請稍後再試。');
