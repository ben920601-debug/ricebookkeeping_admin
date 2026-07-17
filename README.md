# 記帳米粒 ｜ 中控後台（獨立專案）

這是從 LINE bot 主程式拆出來的**獨立後台服務**，跟 LINE bot 是兩個完全分開的程式、
可以分開部署、分開重啟、分開擴充。兩邊唯一的關聯是「連到同一個 MySQL 資料庫」：

```
┌─────────────────┐        ┌──────────────────┐
│  LINE bot 專案   │        │  中控後台專案      │
│  main.py         │        │  admin-panel/     │
│  （webhook、AI）  │        │  （登入、CRUD）    │
└────────┬─────────┘        └────────┬──────────┘
         │                            │
         └──────────┬─────────────────┘
                     ▼
           同一個 MySQL 資料庫
   (bot_settings / keyword_replies /
    sensitive_words / error_logs / groups ...)
```

- 後台改資料 → 寫進資料庫 → LINE bot 那邊最多 **15 秒**內自動讀到最新值，完全不用重啟 bot。
- 這個專案完全不需要 LINE 的任何金鑰、也不需要 Gemini API Key，只需要資料庫連線資訊。

## 部署步驟

### 1. 安裝套件
```bash
pip install -r requirements.txt
```

### 2. 設定資料庫連線
複製 `.env.example` 成 `.env`，填入**跟 LINE bot 專案完全相同**的 `MYSQL_*` 連線資訊
（同一台資料庫、同一個 database）。

> 如果這個資料庫之前還沒執行過 migration（LINE bot 專案的 README 也有提到）：
> ```bash
> mysql -h <host> -u <user> -p jizhang_mili < migration.sql
> ```

### 3. 設定登入帳密（最簡單的方式）
複製 `.env.example` 成 `.env`，直接填：
```
ADMIN_USERNAME=admin
ADMIN_PASSWORD=你想設定的密碼
```
就這樣，不用跑任何指令。JWT 密鑰也不用管，程式第一次啟動會自動產生、存成本地檔案 `.admin_jwt_secret`，之後重啟都會自動沿用。

> 想要密碼不要明文留在 `.env` 的話，可以改用進階版：執行 `python hash_password.py "你的密碼"`，
> 把印出的結果填進 `ADMIN_PASSWORD_HASH`，並把 `ADMIN_PASSWORD` 刪掉。兩者擇一即可，細節見 `.env.example` 裡的註解。

### 4. 啟動
```bash
uvicorn main:app --host 0.0.0.0 --port 8002
```
瀏覽器開啟 `http://<你的網址>:8002/admin` 登入即可。

> ⚠️ `.env` 和自動產生的 `.admin_jwt_secret` 這兩個檔案都不要提交進 Git、不要外流——
> 任何拿到其中一個的人都能登入你的後台。記得在 `.gitignore` 加上 `.env` 和 `.admin_jwt_secret`。

## 部署平台建議
可以跟 LINE bot 專案部署在同一台主機的不同 port、不同容器，甚至完全不同的雲端服務
（例如 bot 放 Zeabur，後台放另一個 Railway 專案），只要兩邊 `.env` 裡的 `MYSQL_*`
指向同一個資料庫即可。若後台對外網址不想公開讓人猜到，可以額外在反向代理層
（Nginx / Cloudflare）加一層 Basic Auth 或 IP 白名單，屬於後台登入之外的多一層防護。

## 這個專案有的東西
- 帳密登入（JWT）
- `/api/admin/status`：總覽（機器人開關、連線狀態、資料筆數）
- `/api/admin/bot-switch`：全域開關
- `/api/admin/keyword-replies`：關鍵字回覆 CRUD
- `/api/admin/sensitive-words`：敏感詞 CRUD
- `/api/admin/groups`、`/api/admin/groups/{id}/reset`：群組狀態查詢與重置
- `/api/admin/errors`：錯誤紀錄查詢與清空
- `/admin`：管理介面網頁（`admin.html`）

## 這個專案沒有的東西（還在 LINE bot 專案裡）
- LINE webhook（`/callback`）
- Gemini 對話與記帳邏輯
- `/api/expenses`、`/api/groups/{id}`、`/api/groups/{id}/orders` 等給 LIFF 報表頁用的 API
  （這些是使用者查看自己帳本的公開 API，跟管理者後台是不同東西，所以留在 LINE bot 專案）
# ricebookkeeping_admin
