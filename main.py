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
import time
import httpx
import certifi
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

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

app = FastAPI(title="記帳米粒 ｜ 中控後台")

DEFAULT_MAINTENANCE_MESSAGE = "🤖 系統維護中，請稍後再試。"

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
# 📮 LINE Messaging API（廣播訊息、好友數統計用）
# ------------------------------------------
# 這裡存一份跟 LINE bot 專案「相同」的 CHANNEL_ACCESS_TOKEN，
# 讓中控後台可以直接呼叫 LINE API，不需要另外跟 bot 服務互相溝通。
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_API_BASE = "https://api.line.me/v2/bot"

if not LINE_CHANNEL_ACCESS_TOKEN:
    print("⚠️ [LINE] 尚未設定 LINE_CHANNEL_ACCESS_TOKEN，「廣播訊息」「好友數統計」這兩個功能會無法使用", flush=True)

def _line_headers():
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

_friend_count_cache = {"ts": 0, "value": None}

def get_friend_count() -> Optional[int]:
    """好友數：LINE Insight API 資料通常有約 1 天延遲，這裡快取 1 小時避免每次刷新總覽都打 API"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return None
    if time.time() - _friend_count_cache["ts"] < 3600 and _friend_count_cache["value"] is not None:
        return _friend_count_cache["value"]
    try:
        # LINE Insight 的好友數資料至少要昨天(或更早)才有，今天的通常還沒生成
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = httpx.get(
            f"{LINE_API_BASE}/insight/followers",
            headers=_line_headers(),
            params={"date": yesterday},
            timeout=8.0,
            verify=certifi.where(),
        )
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "ready":
                _friend_count_cache["value"] = data.get("followers")
                _friend_count_cache["ts"] = time.time()
                return _friend_count_cache["value"]
        return _friend_count_cache["value"]  # API 還沒準備好資料時，沿用上次的值
    except Exception as e:
        print(f"⚠️ 好友數查詢失敗: {e}", flush=True)
        return _friend_count_cache["value"]



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
    maintenance_message: Optional[str] = None  # 關閉時要回覆給使用者的訊息，不填則沿用現有設定

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
    description: str = ""

class SensitiveWordUpdate(BaseModel):
    word: Optional[str] = None
    description: Optional[str] = None

class BroadcastRequest(BaseModel):
    user_id: str
    message: str


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
    """後台首頁總覽：機器人開關狀態、資料庫連線池狀況、各項資料筆數、使用率、好友數、回覆/敏感詞觸發則數
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
    maintenance_message = DEFAULT_MAINTENANCE_MESSAGE
    counts = {"groups": 0, "keyword_replies": 0, "sensitive_words": 0, "unresolved_errors": 0}
    usage = {"active_entities": 0, "total_entities": 0, "usage_rate": None}
    activity_today = {"replies": 0, "sensitive_blocks": 0, "pushes": 0}

    if db_ok:
        try:
            with db_cursor() as cur:
                cur.execute("SELECT `key`, `value` FROM bot_settings WHERE `key` IN ('bot_enabled', 'maintenance_message')")
                settings_rows = {r["key"]: r["value"] for r in cur.fetchall()}
                bot_enabled = settings_rows.get("bot_enabled", "1") == "1"
                maintenance_message = settings_rows.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE

                cur.execute("SELECT COUNT(*) AS c FROM `groups`")
                counts["groups"] = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM keyword_replies")
                counts["keyword_replies"] = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM sensitive_words")
                counts["sensitive_words"] = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM error_logs WHERE created_at >= NOW() - INTERVAL 24 HOUR")
                counts["unresolved_errors"] = cur.fetchone()["c"]

                # 今日活動量：回覆則數、敏感詞攔截則數、主動推播則數（例如行程提醒）
                cur.execute(
                    "SELECT event_type, COUNT(*) AS c FROM stat_events "
                    "WHERE created_at >= CURDATE() GROUP BY event_type"
                )
                for r in cur.fetchall():
                    if r["event_type"] == "reply":
                        activity_today["replies"] = r["c"]
                    elif r["event_type"] == "sensitive_block":
                        activity_today["sensitive_blocks"] = r["c"]
                    elif r["event_type"] == "push":
                        activity_today["pushes"] = r["c"]

                # 使用率：近 7 天有記帳/開團活動的群組＋個人，佔「曾經使用過」總數的比例
                cur.execute("SELECT COUNT(*) AS c FROM `groups`")
                total_groups = cur.fetchone()["c"]
                cur.execute(
                    "SELECT COUNT(DISTINCT owner_id) AS c FROM expenses "
                    "WHERE owner_type='group' AND created_at >= NOW() - INTERVAL 7 DAY"
                )
                active_groups = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(DISTINCT owner_id) AS c FROM expenses WHERE owner_type='user'")
                total_users = cur.fetchone()["c"]
                cur.execute(
                    "SELECT COUNT(DISTINCT owner_id) AS c FROM expenses "
                    "WHERE owner_type='user' AND created_at >= NOW() - INTERVAL 7 DAY"
                )
                active_users = cur.fetchone()["c"]

                total_entities = total_groups + total_users
                active_entities = active_groups + active_users
                usage["total_entities"] = total_entities
                usage["active_entities"] = active_entities
                usage["usage_rate"] = round(active_entities / total_entities * 100, 1) if total_entities > 0 else None
        except Exception as e:
            print(f"⚠️ 後台狀態查詢異常: {e}", flush=True)

    return {
        "bot_enabled": bot_enabled,
        "maintenance_message": maintenance_message,
        "db_ready": DB_READY,
        "db_pool_ok": db_ok,
        "db_pool_error": db_error,
        "counts": counts,
        "usage": usage,
        "activity_today": activity_today,
        "friend_count": get_friend_count(),
    }


@app.post("/api/admin/bot-switch")
def api_admin_bot_switch(body: BotSwitchUpdate, admin: str = Depends(require_admin)):
    """🔴 全域緊急開關：關閉後 LINE bot 會回覆設定好的維護訊息，不會再進入記帳/AI 流程
    （最多 5 秒後 LINE bot 那邊的快取會讀到最新值，不用重啟 bot 服務）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO bot_settings (`key`, `value`) VALUES ('bot_enabled', %s) "
                "ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)",
                ("1" if body.enabled else "0",)
            )
            if body.maintenance_message is not None:
                cur.execute(
                    "INSERT INTO bot_settings (`key`, `value`) VALUES ('maintenance_message', %s) "
                    "ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)",
                    (body.maintenance_message,)
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
            cur.execute("SELECT id, word, description, created_at FROM sensitive_words ORDER BY id DESC")
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
            cur.execute(
                "INSERT INTO sensitive_words (word, description) VALUES (%s, %s)",
                (body.word, body.description or "")
            )
        return {"ok": True}
    except pymysql.err.IntegrityError:
        raise HTTPException(status_code=409, detail="此敏感詞已存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/admin/sensitive-words/{item_id}")
def api_admin_update_sensitive_word(item_id: int, body: SensitiveWordUpdate, admin: str = Depends(require_admin)):
    _require_db()
    fields, params = [], []
    if body.word is not None:
        fields.append("word=%s"); params.append(body.word)
    if body.description is not None:
        fields.append("description=%s"); params.append(body.description)
    if not fields:
        return {"ok": True}
    params.append(item_id)
    try:
        with db_cursor() as cur:
            cur.execute(f"UPDATE sensitive_words SET {', '.join(fields)} WHERE id=%s", tuple(params))
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
    """群組清單，附上團單（orders）與核銷（settlements）的使用次數與比例
    （比例 = 核銷次數 / 團單次數，粗略反映「開的團有多少比例走過核銷流程」，僅供參考）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT group_id, state, active_order_code FROM `groups` ORDER BY group_id")
            groups = cur.fetchall()

            cur.execute("SELECT group_id, COUNT(*) AS c FROM orders GROUP BY group_id")
            order_counts = {r["group_id"]: r["c"] for r in cur.fetchall()}

            cur.execute("SELECT group_id, COUNT(*) AS c FROM settlements GROUP BY group_id")
            settlement_counts = {r["group_id"]: r["c"] for r in cur.fetchall()}

        for g in groups:
            gid = g["group_id"]
            order_count = order_counts.get(gid, 0)
            settlement_count = settlement_counts.get(gid, 0)
            g["order_count"] = order_count
            g["settlement_count"] = settlement_count
            g["settlement_ratio"] = round(settlement_count / order_count * 100, 1) if order_count > 0 else None

        return groups
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


# ---------- 群組廣播（1對1私訊給已互動過的使用者） ----------

@app.get("/api/admin/broadcast-targets")
def api_admin_broadcast_targets(admin: str = Depends(require_admin)):
    """列出曾經在「個人聊天」跟機器人互動過的使用者（owner_type='user'），供廣播訊息選擇對象"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT owner_id AS user_id, MAX(created_by_name) AS name, MAX(created_at) AS last_active
                   FROM expenses
                   WHERE owner_type='user'
                   GROUP BY owner_id
                   ORDER BY last_active DESC
                   LIMIT 200"""
            )
            rows = cur.fetchall()
        for r in rows:
            r["last_active"] = r["last_active"].isoformat() if r["last_active"] else None
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/broadcast")
def api_admin_broadcast(body: BroadcastRequest, admin: str = Depends(require_admin)):
    """發送 1 對 1 私訊給指定的 user_id（LINE push message），並把結果記錄到 broadcast_logs 供查核"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=503, detail="尚未設定 LINE_CHANNEL_ACCESS_TOKEN，無法發送訊息")
    if not body.user_id or not body.message.strip():
        raise HTTPException(status_code=422, detail="對象與訊息內容都不能是空的")

    status = "success"
    error_detail = None
    try:
        res = httpx.post(
            f"{LINE_API_BASE}/message/push",
            headers=_line_headers(),
            json={"to": body.user_id, "messages": [{"type": "text", "text": body.message}]},
            timeout=8.0,
            verify=certifi.where(),
        )
        if res.status_code != 200:
            status = "failed"
            error_detail = f"LINE API 回傳 {res.status_code}: {res.text[:300]}"
    except Exception as e:
        status = "failed"
        error_detail = str(e)

    if DB_READY:
        try:
            with db_cursor() as cur:
                cur.execute("SELECT MAX(created_by_name) AS name FROM expenses WHERE owner_id=%s", (body.user_id,))
                row = cur.fetchone()
                target_name = row["name"] if row else None
                cur.execute(
                    """INSERT INTO broadcast_logs
                       (target_user_id, target_name, message, sent_by, status, error_detail)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (body.user_id, target_name, body.message, admin, status, error_detail)
                )
        except Exception as e:
            print(f"⚠️ 廣播紀錄寫入失敗: {e}", flush=True)

    if status == "failed":
        raise HTTPException(status_code=502, detail=error_detail or "發送失敗")
    return {"ok": True}


# ==========================================
# 🧪 測試限定功能管理（行程模式／群組團單／收據辨識）
# ------------------------------------------
# 這三個功能在 bot 端是用共用密碼 + 效期開通的（TEST_MODE_PASSWORD／TEST_MODE_HOURS），
# 這裡讓你不用密碼流程，也能直接看誰開通了、手動開通/延長/撤銷。
# ==========================================
TEST_FEATURE_LABELS = {
    "itinerary": "行程模式",
    "group_split": "群組團單",
    "receipt_ocr": "收據辨識",
}

class TestModeGrant(BaseModel):
    owner_type: str  # 'user' | 'group'
    owner_id: str
    feature: str      # 'itinerary' | 'group_split' | 'receipt_ocr'
    hours: int = 16

class TestModeExtend(BaseModel):
    hours: int = 16

def _validate_feature(feature: str) -> str:
    if feature not in TEST_FEATURE_LABELS:
        raise HTTPException(status_code=422, detail="未知的功能代碼")
    return feature


@app.get("/api/admin/test-mode/sessions")
def api_test_mode_list(admin: str = Depends(require_admin)):
    """列出所有測試模式授權（含已過期的，前端會標示狀態），依到期時間新到舊排序"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT owner_type, owner_id, feature, expires_at FROM test_mode_sessions "
                "ORDER BY expires_at DESC"
            )
            rows = cur.fetchall()
        now = datetime.now()
        for r in rows:
            r["feature_label"] = TEST_FEATURE_LABELS.get(r["feature"], r["feature"])
            r["active"] = r["expires_at"] > now
            r["expires_at"] = r["expires_at"].isoformat()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/test-mode/pending")
def api_test_mode_pending(admin: str = Depends(require_admin)):
    """列出目前正在等待輸入密碼的請求（超過 5 分鐘 bot 端會自動視為過期，這裡單純顯示原始紀錄）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT owner_type, owner_id, feature, requested_at FROM test_mode_pending "
                "ORDER BY requested_at DESC"
            )
            rows = cur.fetchall()
        for r in rows:
            r["feature_label"] = TEST_FEATURE_LABELS.get(r["feature"], r["feature"])
            r["requested_at"] = r["requested_at"].isoformat()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/test-mode/pending/{owner_type}/{owner_id}")
def api_test_mode_clear_pending(owner_type: str, owner_id: str, admin: str = Depends(require_admin)):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "DELETE FROM test_mode_pending WHERE owner_type=%s AND owner_id=%s",
                (owner_type, owner_id)
            )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/test-mode/grant")
def api_test_mode_grant(body: TestModeGrant, admin: str = Depends(require_admin)):
    """手動開通（略過密碼流程），常用於幫忙測試或臨時授權"""
    _require_db()
    feature = _validate_feature(body.feature)
    if body.owner_type not in ("user", "group"):
        raise HTTPException(status_code=422, detail="owner_type 只能是 user 或 group")
    if body.hours <= 0 or body.hours > 24 * 30:
        raise HTTPException(status_code=422, detail="開通時數請填 1～720（30天）之間")
    try:
        expires = datetime.now() + timedelta(hours=body.hours)
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO test_mode_sessions (owner_type, owner_id, feature, expires_at)
                   VALUES (%s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE expires_at=VALUES(expires_at)""",
                (body.owner_type, body.owner_id, feature, expires)
            )
        return {"ok": True, "expires_at": expires.isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/admin/test-mode/sessions/{owner_type}/{owner_id}/{feature}")
def api_test_mode_extend(owner_type: str, owner_id: str, feature: str, body: TestModeExtend, admin: str = Depends(require_admin)):
    """延長效期：以現在時間 + hours 重新計算到期時間（不是在原本效期上累加）"""
    _require_db()
    feature = _validate_feature(feature)
    if body.hours <= 0 or body.hours > 24 * 30:
        raise HTTPException(status_code=422, detail="延長時數請填 1～720（30天）之間")
    try:
        expires = datetime.now() + timedelta(hours=body.hours)
        with db_cursor() as cur:
            cur.execute(
                "UPDATE test_mode_sessions SET expires_at=%s WHERE owner_type=%s AND owner_id=%s AND feature=%s",
                (expires, owner_type, owner_id, feature)
            )
        return {"ok": True, "expires_at": expires.isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/test-mode/sessions/{owner_type}/{owner_id}/{feature}")
def api_test_mode_revoke(owner_type: str, owner_id: str, feature: str, admin: str = Depends(require_admin)):
    """撤銷：直接刪除該筆授權，等同立刻恢復未開通狀態"""
    _require_db()
    feature = _validate_feature(feature)
    try:
        with db_cursor() as cur:
            cur.execute(
                "DELETE FROM test_mode_sessions WHERE owner_type=%s AND owner_id=%s AND feature=%s",
                (owner_type, owner_id, feature)
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


# ==========================================
# 🗄️ 資料庫瀏覽器（限定記帳相關表：expenses / orders / order_items / settlements / groups）
# ------------------------------------------
# 其他設定類的表（keyword_replies / sensitive_words / bot_settings / error_logs 等）
# 已經有專屬管理頁面，這裡刻意不開放，避免跟專屬頁面的邏輯打架、也降低誤操作風險。
# 所有欄位/表名都會先跟 information_schema 對過，才會拼進 SQL 裡，避免注入風險。
# ==========================================
DB_BROWSER_ALLOWED_TABLES = [
    "expenses", "orders", "order_items", "settlements", "groups",
    "itineraries", "test_mode_sessions", "test_mode_pending", "pending_itinerary_confirm",
]

class DbRowUpsert(BaseModel):
    values: dict

def _validate_table(table: str) -> str:
    if table not in DB_BROWSER_ALLOWED_TABLES:
        raise HTTPException(status_code=403, detail="這張表不開放透過資料庫瀏覽器存取")
    return table

def _get_table_schema(table: str):
    """回傳該表的欄位資訊（含哪一欄是主鍵），欄位存在性也順便驗證過"""
    with db_cursor() as cur:
        cur.execute(
            """SELECT COLUMN_NAME AS name, DATA_TYPE AS data_type, IS_NULLABLE AS nullable,
                      COLUMN_KEY AS col_key, COLUMN_DEFAULT AS col_default, EXTRA AS extra
               FROM information_schema.columns
               WHERE table_schema = DATABASE() AND table_name = %s
               ORDER BY ORDINAL_POSITION""",
            (table,)
        )
        cols = cur.fetchall()
    if not cols:
        raise HTTPException(status_code=404, detail="資料庫裡找不到這張表")
    pk_cols = [c["name"] for c in cols if c["col_key"] == "PRI"]
    return cols, (pk_cols[0] if pk_cols else None)

def _valid_columns(table: str) -> set:
    cols, _ = _get_table_schema(table)
    return {c["name"] for c in cols}


@app.get("/api/admin/db/tables")
def api_db_list_tables(admin: str = Depends(require_admin)):
    return {"tables": DB_BROWSER_ALLOWED_TABLES}


@app.get("/api/admin/db/tables/{table}/schema")
def api_db_table_schema(table: str, admin: str = Depends(require_admin)):
    _require_db()
    table = _validate_table(table)
    try:
        cols, pk = _get_table_schema(table)
        return {"columns": cols, "primary_key": pk}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/db/tables/{table}/rows")
def api_db_list_rows(
    table: str,
    page: int = 1,
    page_size: int = 25,
    search: str = "",
    sort_by: str = "",
    sort_dir: str = "desc",
    admin: str = Depends(require_admin),
):
    _require_db()
    table = _validate_table(table)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    try:
        cols, pk = _get_table_schema(table)
        col_names = [c["name"] for c in cols]
        sort_col = sort_by if sort_by in col_names else (pk or col_names[0])
        sort_dir = "ASC" if sort_dir.lower() == "asc" else "DESC"

        where_sql, params = "", []
        if search.strip():
            # 只對文字型欄位做模糊搜尋，避免對數字/日期欄位下 LIKE 出現型別錯誤
            text_cols = [c["name"] for c in cols if c["data_type"] in
                         ("varchar", "text", "char", "mediumtext", "longtext")]
            if text_cols:
                where_sql = "WHERE " + " OR ".join(f"`{c}` LIKE %s" for c in text_cols)
                params = [f"%{search.strip()}%"] * len(text_cols)

        with db_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM `{table}` {where_sql}", tuple(params))
            total = cur.fetchone()["c"]

            offset = (page - 1) * page_size
            cur.execute(
                f"SELECT * FROM `{table}` {where_sql} ORDER BY `{sort_col}` {sort_dir} LIMIT %s OFFSET %s",
                tuple(params) + (page_size, offset)
            )
            rows = cur.fetchall()

        for r in rows:
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()

        return {"rows": rows, "total": total, "page": page, "page_size": page_size, "primary_key": pk}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/db/tables/{table}/rows")
def api_db_create_row(table: str, body: DbRowUpsert, admin: str = Depends(require_admin)):
    _require_db()
    table = _validate_table(table)
    try:
        valid_cols = _valid_columns(table)
        data = {k: v for k, v in body.values.items() if k in valid_cols and v not in (None, "")}
        if not data:
            raise HTTPException(status_code=422, detail="沒有可新增的欄位內容")
        cols_sql = ", ".join(f"`{k}`" for k in data)
        placeholders = ", ".join(["%s"] * len(data))
        with db_cursor() as cur:
            cur.execute(f"INSERT INTO `{table}` ({cols_sql}) VALUES ({placeholders})", tuple(data.values()))
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/admin/db/tables/{table}/rows/{pk_value}")
def api_db_update_row(table: str, pk_value: str, body: DbRowUpsert, admin: str = Depends(require_admin)):
    _require_db()
    table = _validate_table(table)
    try:
        cols, pk = _get_table_schema(table)
        if not pk:
            raise HTTPException(status_code=400, detail="這張表沒有偵測到主鍵，無法安全地修改單筆資料")
        valid_cols = {c["name"] for c in cols} - {pk}
        data = {k: v for k, v in body.values.items() if k in valid_cols}
        if not data:
            return {"ok": True}
        set_sql = ", ".join(f"`{k}`=%s" for k in data)
        with db_cursor() as cur:
            cur.execute(
                f"UPDATE `{table}` SET {set_sql} WHERE `{pk}`=%s LIMIT 1",
                tuple(data.values()) + (pk_value,)
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/db/tables/{table}/rows/{pk_value}")
def api_db_delete_row(table: str, pk_value: str, admin: str = Depends(require_admin)):
    _require_db()
    table = _validate_table(table)
    try:
        _, pk = _get_table_schema(table)
        if not pk:
            raise HTTPException(status_code=400, detail="這張表沒有偵測到主鍵，無法安全地刪除單筆資料")
        with db_cursor() as cur:
            cur.execute(f"DELETE FROM `{table}` WHERE `{pk}`=%s LIMIT 1", (pk_value,))
        return {"ok": True}
    except HTTPException:
        raise
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