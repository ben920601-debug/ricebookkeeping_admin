"""
記帳米粒 ｜ 中控後台（獨立專案）
----------------------------------------
這是從 LINE bot 主程式拆出來的獨立後台服務。
兩邊唯一的共通點是「同一個 MySQL 資料庫」：
  - 這裡負責讓管理者登入、編輯 bot_settings / keyword_replies / sensitive_words，
    以及查看 groups / error_logs。
  - LINE bot 那邊（另一個專案）會定期（預設 15 秒快取）從同一批資料表讀出最新設定。
兩個專案可以分開部署、分開重啟、分開擴充，互不影響。

部署前請先確認：
  1. .env 已設定跟 LINE bot 專案「相同」的 MYSQL_* 連線資訊（連同一個資料庫）
  2. 已經對該資料庫執行過 migration.sql（建立 bot_settings / keyword_replies /
     sensitive_words / error_logs 四張表；LINE bot 專案裡也附了一份一樣的 migration.sql）
  3. 已用 hash_password.py 產生 ADMIN_PASSWORD_HASH 並寫進 .env
"""
import os
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB
import jwt

from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="記帳米粒 ｜ 中控後台")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 🔌 資料庫連線（跟 LINE bot 專案接同一個 MySQL）
# ==========================================
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE", "jizhang_mili"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": True,
}

DB_POOL = None

def _init_pool():
    global DB_POOL
    DB_POOL = PooledDB(
        creator=pymysql,
        mincached=1,
        maxcached=3,
        maxconnections=10,
        blocking=True,
        ping=1,
        **MYSQL_CONFIG,
    )

def get_db_connection():
    if DB_POOL is None:
        _init_pool()
    return DB_POOL.connection()

@contextmanager
def db_cursor():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()

DB_READY = False
try:
    _init_pool()
    with db_cursor() as _cur:
        _cur.execute("SELECT 1")
    DB_READY = True
    print("🔥 [DATABASE] 中控後台 MySQL 連線池就位！", flush=True)
except Exception as e:
    DB_READY = False
    print(f"❌ [DATABASE] 中控後台 MySQL 連線初始化異常: {e}", flush=True)

def _require_db():
    if not DB_READY:
        raise HTTPException(status_code=503, detail="資料庫尚未就緒")


# ==========================================
# 🔐 登入驗證（帳號密碼 + JWT）
# ------------------------------------------
# 帳密存放在環境變數（ADMIN_USERNAME / ADMIN_PASSWORD_HASH），
# 「修改帳密」只需要改 .env 重啟，不用改程式碼。
# ==========================================
# ==========================================
# 🔐 登入驗證（帳號密碼 + JWT）
# ------------------------------------------
# 密碼設定二選一：
#   簡單版：ADMIN_PASSWORD=你的密碼（明文寫在 .env，改密碼直接改這行、重啟即可生效）
#   進階版：ADMIN_PASSWORD_HASH=<用 hash_password.py 產生的雜湊值>（.env 裡不留明文密碼）
# 兩個都有設定的話，優先使用 ADMIN_PASSWORD_HASH。
# ==========================================
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
ADMIN_PASSWORD_PLAIN = os.getenv("ADMIN_PASSWORD", "")

# JWT 密鑰：優先用 .env 裡固定的值；沒有的話自動產生一組並存成本地檔案，
# 下次啟動會直接讀檔案裡的值，不會每次重啟都變、也不用你自己手動產生貼上 .env。
ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET")
if not ADMIN_JWT_SECRET:
    _secret_file = os.path.join(os.path.dirname(__file__), ".admin_jwt_secret")
    if os.path.exists(_secret_file):
        with open(_secret_file, "r") as f:
            ADMIN_JWT_SECRET = f.read().strip()
    if not ADMIN_JWT_SECRET:
        ADMIN_JWT_SECRET = secrets.token_hex(32)
        with open(_secret_file, "w") as f:
            f.write(ADMIN_JWT_SECRET)
        print(f"🔑 [ADMIN] 已自動產生 JWT 密鑰並存到 {_secret_file}（請跟 .env 一樣，不要提交進版本控制）", flush=True)

ADMIN_JWT_TTL_HOURS = int(os.getenv("ADMIN_JWT_TTL_HOURS", "12"))

if not ADMIN_PASSWORD_HASH and not ADMIN_PASSWORD_PLAIN:
    print("⚠️ [ADMIN] 尚未設定密碼！請在 .env 加一行 ADMIN_PASSWORD=你的密碼（最簡單），"
          "或用 hash_password.py 產生 ADMIN_PASSWORD_HASH（較安全）", flush=True)

def hash_password(plain: str, salt: str = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"

def verify_password(plain: str) -> bool:
    if ADMIN_PASSWORD_HASH:
        # 進階版：跟儲存的雜湊值比對
        if "$" not in ADMIN_PASSWORD_HASH:
            return False
        salt, _ = ADMIN_PASSWORD_HASH.split("$", 1)
        candidate = hash_password(plain, salt)
        return hmac.compare_digest(candidate, ADMIN_PASSWORD_HASH)
    if ADMIN_PASSWORD_PLAIN:
        # 簡單版：直接比對明文（仍用 compare_digest 避免時序攻擊）
        return hmac.compare_digest(plain, ADMIN_PASSWORD_PLAIN)
    return False

def create_admin_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=ADMIN_JWT_TTL_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")

_bearer_scheme = HTTPBearer(auto_error=False)

def require_admin(credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme)):
    """所有 /api/admin/* 路由都掛這個依賴；沒帶 token 或 token 失效一律 401"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="請先登入中控後台")
    try:
        payload = jwt.decode(credentials.credentials, ADMIN_JWT_SECRET, algorithms=["HS256"])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登入已逾時，請重新登入")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="登入憑證無效，請重新登入")


class AdminLoginRequest(BaseModel):
    username: str
    password: str

class BotSwitchUpdate(BaseModel):
    enabled: bool

class KeywordReplyCreate(BaseModel):
    keyword: str
    reply_text: str
    enabled: bool = True

class KeywordReplyUpdate(BaseModel):
    keyword: Optional[str] = None
    reply_text: Optional[str] = None
    enabled: Optional[bool] = None

class SensitiveWordCreate(BaseModel):
    word: str


@app.get("/")
def health_check():
    return {"status": "admin_panel_active", "db_ready": DB_READY}


@app.post("/api/admin/login")
def api_admin_login(body: AdminLoginRequest):
    """帳號密碼登入，成功回傳 JWT；帳密設定在 .env 的 ADMIN_USERNAME / ADMIN_PASSWORD_HASH"""
    valid_user = hmac.compare_digest(body.username, ADMIN_USERNAME)
    valid_pass = verify_password(body.password)
    if not (valid_user and valid_pass):
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    return {"token": create_admin_token(body.username), "expires_in_hours": ADMIN_JWT_TTL_HOURS}


@app.get("/api/admin/status")
def api_admin_status(admin: str = Depends(require_admin)):
    """後台首頁總覽：機器人開關狀態、資料庫連線池狀況、各項資料筆數
    （注意：這裡的 db_pool_ok 只代表「後台這個服務」連得到資料庫，跟 LINE bot 服務是否連得到是分開的兩件事）"""
    db_ok = True
    db_error = None
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as e:
        db_ok = False
        db_error = str(e)

    bot_enabled = True
    counts = {"groups": 0, "keyword_replies": 0, "sensitive_words": 0, "unresolved_errors": 0}
    if db_ok:
        try:
            with db_cursor() as cur:
                cur.execute("SELECT `value` FROM bot_settings WHERE `key`='bot_enabled'")
                row = cur.fetchone()
                bot_enabled = (row is None) or (row["value"] == "1")

                cur.execute("SELECT COUNT(*) AS c FROM `groups`")
                counts["groups"] = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM keyword_replies")
                counts["keyword_replies"] = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM sensitive_words")
                counts["sensitive_words"] = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM error_logs WHERE created_at >= NOW() - INTERVAL 24 HOUR")
                counts["unresolved_errors"] = cur.fetchone()["c"]
        except Exception as e:
            print(f"⚠️ 後台狀態查詢異常: {e}", flush=True)

    return {
        "bot_enabled": bot_enabled,
        "db_ready": DB_READY,
        "db_pool_ok": db_ok,
        "db_pool_error": db_error,
        "counts": counts,
    }


@app.post("/api/admin/bot-switch")
def api_admin_bot_switch(body: BotSwitchUpdate, admin: str = Depends(require_admin)):
    """🔴 全域緊急開關：關閉後 LINE bot 對所有訊息完全靜默，不寫入任何資料
    （最多 15 秒後 LINE bot 那邊的快取會讀到最新值，不用重啟 bot 服務）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO bot_settings (`key`, `value`) VALUES ('bot_enabled', %s) "
                "ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)",
                ("1" if body.enabled else "0",)
            )
        return {"ok": True, "bot_enabled": body.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- 關鍵字回覆管理 ----------

@app.get("/api/admin/keyword-replies")
def api_admin_list_keyword_replies(admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id, keyword, reply_text, enabled, updated_at FROM keyword_replies ORDER BY id DESC")
            rows = cur.fetchall()
        for r in rows:
            r["updated_at"] = r["updated_at"].isoformat() if r["updated_at"] else None
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/keyword-replies")
def api_admin_create_keyword_reply(body: KeywordReplyCreate, admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO keyword_replies (keyword, reply_text, enabled) VALUES (%s, %s, %s)",
                (body.keyword, body.reply_text, 1 if body.enabled else 0)
            )
        return {"ok": True}
    except pymysql.err.IntegrityError:
        raise HTTPException(status_code=409, detail="此關鍵字已存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/admin/keyword-replies/{item_id}")
def api_admin_update_keyword_reply(item_id: int, body: KeywordReplyUpdate, admin: str = Depends(require_admin)):
    _require_db()
    fields, params = [], []
    if body.keyword is not None:
        fields.append("keyword=%s"); params.append(body.keyword)
    if body.reply_text is not None:
        fields.append("reply_text=%s"); params.append(body.reply_text)
    if body.enabled is not None:
        fields.append("enabled=%s"); params.append(1 if body.enabled else 0)
    if not fields:
        return {"ok": True}
    params.append(item_id)
    try:
        with db_cursor() as cur:
            cur.execute(f"UPDATE keyword_replies SET {', '.join(fields)} WHERE id=%s", tuple(params))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/keyword-replies/{item_id}")
def api_admin_delete_keyword_reply(item_id: int, admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM keyword_replies WHERE id=%s", (item_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- 敏感詞管理 ----------

@app.get("/api/admin/sensitive-words")
def api_admin_list_sensitive_words(admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id, word, created_at FROM sensitive_words ORDER BY id DESC")
            rows = cur.fetchall()
        for r in rows:
            r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/sensitive-words")
def api_admin_create_sensitive_word(body: SensitiveWordCreate, admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("INSERT INTO sensitive_words (word) VALUES (%s)", (body.word,))
        return {"ok": True}
    except pymysql.err.IntegrityError:
        raise HTTPException(status_code=409, detail="此敏感詞已存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/sensitive-words/{item_id}")
def api_admin_delete_sensitive_word(item_id: int, admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM sensitive_words WHERE id=%s", (item_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- 群組狀態管理 ----------

@app.get("/api/admin/groups")
def api_admin_list_groups(admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT group_id, state, active_order_code FROM `groups` ORDER BY group_id")
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/groups/{group_id}/reset")
def api_admin_reset_group(group_id: str, admin: str = Depends(require_admin)):
    """🛠️ 緊急恢復：強制把卡住的群組拉回 normal 模式、清空進行中單號"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE `groups` SET state='normal', active_order_code='' WHERE group_id=%s",
                (group_id,)
            )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- 錯誤紀錄監控 ----------

@app.get("/api/admin/errors")
def api_admin_list_errors(limit: int = 100, admin: str = Depends(require_admin)):
    _require_db()
    limit = max(1, min(limit, 500))
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT id, source, message, target_id, created_at FROM error_logs "
                "ORDER BY created_at DESC LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall()
        for r in rows:
            r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/errors")
def api_admin_clear_errors(admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM error_logs")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- 後台網頁本體 ----------

@app.get("/admin")
def admin_panel_page():
    admin_path = os.path.join(os.path.dirname(__file__), "admin.html")
    if not os.path.exists(admin_path):
        raise HTTPException(status_code=404, detail="admin.html 未跟 main.py 放在同一個目錄")
    return FileResponse(admin_path)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8002")))
