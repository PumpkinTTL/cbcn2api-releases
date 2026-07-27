import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from src.models.account import Account

DB_DIR = Path.home() / ".cbcn2api"
DB_PATH = DB_DIR / "accounts.db"

_schema_lock = threading.Lock()
_SCHEMA_VERSION = 1


def _get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # 每个 DB 独立用 user_version 标记是否已初始化（测试切换临时库也能正确建表）
    if conn.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
        with _schema_lock:
            if conn.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
                conn.execute("PRAGMA journal_mode=WAL")
                _ensure_schema(conn)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            email TEXT DEFAULT '',
            uid TEXT,
            nickname TEXT,
            enterprise_id TEXT,
            enterprise_name TEXT,
            access_token TEXT NOT NULL DEFAULT '',
            refresh_token TEXT,
            token_type TEXT DEFAULT 'Bearer',
            expires_at INTEGER,
            domain TEXT,
            status TEXT DEFAULT 'normal',
            tags TEXT,
            last_checkin_time INTEGER,
            checkin_streak INTEGER DEFAULT 0,
            quota_raw TEXT,
            created_at INTEGER DEFAULT 0,
            last_used INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_accounts_platform ON accounts(platform)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS proxy_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            event TEXT NOT NULL,
            account_id TEXT,
            account_name TEXT,
            model TEXT,
            message TEXT,
            details TEXT,
            platform TEXT DEFAULT 'workbuddy'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_logs_platform ON proxy_logs(platform)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON proxy_logs(timestamp)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_stats (
            account_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            total_credit REAL DEFAULT 0,
            lifetime_credit REAL DEFAULT 0,
            cache_hits INTEGER DEFAULT 0,
            cache_misses INTEGER DEFAULT 0,
            request_count INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0,
            PRIMARY KEY (account_id, platform)
        )
    """)
    try:
        conn.execute("ALTER TABLE account_stats ADD COLUMN lifetime_credit REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute("UPDATE account_stats SET lifetime_credit=total_credit WHERE lifetime_credit=0")


def upsert_account(platform: str, account: Account) -> Account:
    now = int(time.time())
    conn = _get_conn()
    try:
        existing = _load_by_id(conn, platform, account.id)
        if existing:
            account.created_at = existing.created_at
        else:
            account.created_at = now
        account.last_used = now

        conn.execute("""
            INSERT INTO accounts (
                id, platform, email, uid, nickname,
                enterprise_id, enterprise_name,
                access_token, refresh_token, token_type, expires_at, domain,
                status, tags,
                last_checkin_time, checkin_streak, quota_raw,
                created_at, last_used
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                email=COALESCE(NULLIF(excluded.email, ''), accounts.email),
                uid=COALESCE(NULLIF(excluded.uid, ''), accounts.uid),
                nickname=COALESCE(NULLIF(excluded.nickname, ''), accounts.nickname),
                enterprise_id=COALESCE(NULLIF(excluded.enterprise_id, ''), accounts.enterprise_id),
                enterprise_name=COALESCE(NULLIF(excluded.enterprise_name, ''), accounts.enterprise_name),
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                token_type=excluded.token_type,
                expires_at=excluded.expires_at,
                domain=COALESCE(NULLIF(excluded.domain, ''), accounts.domain),
                status=excluded.status,
                tags=excluded.tags,
                last_checkin_time=excluded.last_checkin_time,
                checkin_streak=excluded.checkin_streak,
                quota_raw=excluded.quota_raw,
                created_at=excluded.created_at,
                last_used=excluded.last_used
        """, (
            account.id, platform, account.email or "", account.uid,
            account.nickname, account.enterprise_id, account.enterprise_name,
            account.access_token or "", account.refresh_token,
            account.token_type or "Bearer", account.expires_at, account.domain,
            account.status or "normal",
            json.dumps(account.tags, ensure_ascii=False) if account.tags else None,
            account.last_checkin_time, account.checkin_streak or 0,
            json.dumps(account.quota_raw, ensure_ascii=False) if account.quota_raw else None,
            account.created_at, account.last_used,
        ))
        conn.commit()
        return account
    finally:
        conn.close()


def load_account(platform: str, account_id: str) -> Optional[Account]:
    conn = _get_conn()
    try:
        return _load_by_id(conn, platform, account_id)
    finally:
        conn.close()


def _load_by_id(conn: sqlite3.Connection, platform: str, account_id: str) -> Optional[Account]:
    row = conn.execute(
        "SELECT * FROM accounts WHERE id=? AND platform=?", (account_id, platform)
    ).fetchone()
    if not row:
        return None
    return _row_to_account(row)


def _row_to_account(row: sqlite3.Row) -> Account:
    tags = json.loads(row["tags"]) if row["tags"] else None
    quota_raw = json.loads(row["quota_raw"]) if row["quota_raw"] else None
    return Account(
        id=row["id"], email=row["email"] or "", uid=row["uid"],
        nickname=row["nickname"],
        enterprise_id=row["enterprise_id"],
        enterprise_name=row["enterprise_name"],
        tags=tags,
        access_token=row["access_token"] or "",
        refresh_token=row["refresh_token"],
        token_type=row["token_type"] or "Bearer",
        expires_at=row["expires_at"], domain=row["domain"],
        status=row["status"] or "normal",
        last_checkin_time=row["last_checkin_time"],
        checkin_streak=row["checkin_streak"] or 0,
        quota_raw=quota_raw,
        created_at=row["created_at"] or 0,
        last_used=row["last_used"] or 0,
    )


def list_accounts(platform: str) -> list[Account]:
    conn = _get_conn()
    try:
        # 按创建时间倒序（新导入的在前）。
        # 不用 last_used —— upsert 每次都无脑刷 last_used=now，导致禁用/启用/刷新 token
        # 后卡片飘走。created_at 稳定不变，顺序才稳。当前调度号的置顶在前端 filteredAccounts 里做。
        rows = conn.execute(
            "SELECT * FROM accounts WHERE platform=? ORDER BY created_at DESC, id ASC", (platform,)
        ).fetchall()
        return [_row_to_account(r) for r in rows]
    finally:
        conn.close()


def remove_account(platform: str, account_id: str):
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM accounts WHERE id=? AND platform=?", (account_id, platform)
        )
        conn.commit()
    finally:
        conn.close()


def find_duplicate(platform: str, uid: Optional[str], email: str) -> Optional[str]:
    conn = _get_conn()
    try:
        # 1. uid 精确匹配（最可靠，OAuth 必返回 uid）
        if uid:
            row = conn.execute(
                "SELECT id FROM accounts WHERE platform=? AND uid=?",
                (platform, uid)
            ).fetchone()
            if row:
                return row["id"]
        # 2. email 匹配 —— 不再要求「必须含 @」。
        # 官方没绑邮箱时，email 字段就是手机号或昵称（18775642907、又是一年冬）。
        # 旧代码 if "@" in norm 把所有手机号账号挡在外面，导致重复登录同号去重失败。
        # 手机号当 email 用时也要匹配。
        if email:
            norm = email.strip().lower()
            if norm:
                rows = conn.execute(
                    "SELECT id, uid, email FROM accounts WHERE platform=? AND LOWER(email)=?",
                    (platform, norm)
                ).fetchall()
                for r in rows:
                    # uid 不一致就跳过（避免不同账号碰巧 email 相同误判）
                    if uid and r["uid"] and r["uid"].strip().lower() != uid.strip().lower():
                        continue
                    return r["id"]
        return None
    finally:
        conn.close()


THEME_FILE = DB_DIR / "theme.txt"


def save_theme(theme: str):
    DB_DIR.mkdir(parents=True, exist_ok=True)
    THEME_FILE.write_text(theme, encoding="utf-8")


def load_theme() -> str:
    try:
        return THEME_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return "light"


def get_stats(platform: str) -> dict:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as total FROM accounts WHERE platform=?", (platform,)
        ).fetchone()
        return {"total_accounts": row["total"] if row else 0}
    finally:
        conn.close()


def save_setting(key: str, value: str):
    conn = _get_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = _get_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


MAX_LOG_ROWS = 5000


def add_log(event: str, platform: str = "workbuddy", account_id: str = "",
            account_name: str = "", model: str = "", message: str = "",
            details: str = ""):
    if get_setting("log_enabled", "1") != "1":
        return
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO proxy_logs (timestamp, event, platform, account_id, account_name, model, message, details) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(time.time()), event, platform, account_id, account_name, model, message, details),
        )
        conn.commit()
        _prune_logs(conn)
    finally:
        conn.close()


def list_logs(platform: str, limit: int = 200, offset: int = 0,
              event: str = "", since: int = 0) -> list[dict]:
    conn = _get_conn()
    try:
        sql = "SELECT * FROM proxy_logs WHERE platform=? "
        params = [platform]
        if event:
            sql += "AND event=? "
            params.append(event)
        if since:
            sql += "AND timestamp>=? "
            params.append(since)
        sql += "ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def clear_logs(platform: str = "", before: int = 0):
    conn = _get_conn()
    try:
        if before:
            conn.execute("DELETE FROM proxy_logs WHERE timestamp<?", (before,))
        elif platform:
            conn.execute("DELETE FROM proxy_logs WHERE platform=?", (platform,))
        else:
            conn.execute("DELETE FROM proxy_logs")
        conn.commit()
    finally:
        conn.close()


def _prune_logs(conn: sqlite3.Connection):
    row = conn.execute("SELECT COUNT(*) as cnt FROM proxy_logs").fetchone()
    if row and row["cnt"] > MAX_LOG_ROWS:
        excess = row["cnt"] - MAX_LOG_ROWS
        conn.execute(
            "DELETE FROM proxy_logs WHERE id IN (SELECT id FROM proxy_logs ORDER BY timestamp ASC LIMIT ?)",
            (excess,),
        )
        conn.commit()


def update_account_stats(platform: str, account_id: str, usage: dict):
    conn = _get_conn()
    try:
        def _i(v):
            try:
                return int(float(v)) if v else 0
            except (ValueError, TypeError):
                return 0
        pt = _i(usage.get("prompt_tokens", 0))
        ct = _i(usage.get("completion_tokens", 0))
        tt = _i(usage.get("total_tokens", pt + ct))
        try:
            credit = float(usage.get("credit", 0) or 0)
        except (ValueError, TypeError):
            credit = 0.0
        hits = _i(usage.get("prompt_cache_hit_tokens", 0))
        misses = _i(usage.get("prompt_cache_miss_tokens", 0))
        now = int(time.time())
        conn.execute("""
            INSERT INTO account_stats (account_id, platform, prompt_tokens, completion_tokens, total_tokens, total_credit, lifetime_credit, cache_hits, cache_misses, request_count, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,1,?)
            ON CONFLICT(account_id, platform) DO UPDATE SET
                prompt_tokens=prompt_tokens+?,
                completion_tokens=completion_tokens+?,
                total_tokens=total_tokens+?,
                total_credit=total_credit+?,
                lifetime_credit=lifetime_credit+?,
                cache_hits=cache_hits+?,
                cache_misses=cache_misses+?,
                request_count=request_count+1,
                updated_at=?
        """, (account_id, platform, pt, ct, tt, credit, credit, hits, misses, now,
              pt, ct, tt, credit, credit, hits, misses, now))
        conn.commit()
    finally:
        conn.close()


def get_account_stats(platform: str, account_id: str) -> dict:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM account_stats WHERE account_id=? AND platform=?",
            (account_id, platform)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_account_stats(platform: str) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM account_stats WHERE platform=? ORDER BY updated_at DESC",
            (platform,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def reset_account_credit(platform: str, account_id: str):
    """刷新账号时仅清零代理累积积分，保留 token/缓存统计。"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE account_stats SET total_credit=0 WHERE account_id=? AND platform=?",
            (account_id, platform)
        )
        conn.commit()
    finally:
        conn.close()


def reset_account_stats(platform: str = "", account_id: str = ""):
    conn = _get_conn()
    try:
        if account_id and platform:
            conn.execute("DELETE FROM account_stats WHERE account_id=? AND platform=?", (account_id, platform))
        elif platform:
            conn.execute("DELETE FROM account_stats WHERE platform=?", (platform,))
        else:
            conn.execute("DELETE FROM account_stats")
        conn.commit()
    finally:
        conn.close()
