import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from src.models.account import Account

DB_DIR = Path.home() / ".cbcn2api"
DB_PATH = DB_DIR / "accounts.db"


def _get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
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
                email=excluded.email, uid=excluded.uid,
                nickname=excluded.nickname,
                enterprise_id=excluded.enterprise_id,
                enterprise_name=excluded.enterprise_name,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                token_type=excluded.token_type,
                expires_at=excluded.expires_at,
                domain=excluded.domain,
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
        rows = conn.execute(
            "SELECT * FROM accounts WHERE platform=? ORDER BY last_used DESC", (platform,)
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
        if uid:
            row = conn.execute(
                "SELECT id FROM accounts WHERE platform=? AND uid=?",
                (platform, uid)
            ).fetchone()
            if row:
                return row["id"]
        if email:
            norm = email.strip().lower()
            if "@" in norm:
                rows = conn.execute(
                    "SELECT id, uid, email FROM accounts WHERE platform=? AND LOWER(email)=?",
                    (platform, norm)
                ).fetchall()
                for r in rows:
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
