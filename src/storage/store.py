import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from src.models.account import Account

logger = logging.getLogger(__name__)

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
    # 列级迁移无条件执行（幂等）：老库 user_version 已是 1 时不会再走 _ensure_schema，
    # 新增列必须在这里补，否则报 "no such column"。
    _run_migrations(conn)
    return conn


def _run_migrations(conn: sqlite3.Connection):
    """所有幂等列级迁移。每次连接都跑（ALTER ... ADD COLUMN 失败即已存在，忽略）。
    不能依赖 user_version —— 老库版本号已置 1，新增列不会自动补上。"""
    try:
        conn.execute("ALTER TABLE account_stats ADD COLUMN lifetime_credit REAL DEFAULT 0")
        conn.execute("UPDATE account_stats SET lifetime_credit=total_credit WHERE lifetime_credit=0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN deleted_at INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN delete_batch INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN batch_note TEXT")
    except sqlite3.OperationalError:
        pass
    # 旧数据补批次：批次功能上线前删除的账号 delete_batch 为 NULL，
    # 按删除时间（秒）补一个批次号，让回收站旧数据也能整组恢复/删除。
    # 幂等：补齐后不再命中 WHERE。UPDATE 是 DML，必须显式 commit。
    try:
        conn.execute(
            "UPDATE accounts SET delete_batch = deleted_at * 1000 "
            "WHERE status='deleted' AND delete_batch IS NULL AND deleted_at IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN fingerprint TEXT")
    except sqlite3.OperationalError:
        pass
    # auth_raw：grok 等新 provider 的 OAuth 原始数据兜底（id_token/scope/订阅信息）。
    # account.py 早已定义该字段，这里补落库；CodeBuddy 账号该列为 NULL，零影响。
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN auth_raw TEXT")
    except sqlite3.OperationalError:
        pass
    # 新表无条件建（老库 user_version 已 1，_ensure_schema 不再跑）
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offline_license_records (
                code TEXT PRIMARY KEY,
                used_at INTEGER NOT NULL,
                expires_at INTEGER,
                machine_code TEXT DEFAULT ''
            )
        """)
    except sqlite3.OperationalError:
        pass


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
    # 列级迁移（lifetime_credit / deleted_at）统一在 _run_migrations 无条件执行，
    # 这里不重复加，避免老库 user_version=1 时漏补。


def upsert_account(platform: str, account: Account) -> Account:
    now = int(time.time())
    conn = _get_conn()
    try:
        # 防复活防线 1：硬删除后 24h 内的 tombstone —— 拦截快照类并发回写
        # （checkin_all / batch_checkin_status 等先取列表快照再逐个 upsert，
        # 删除进行中它们的循环会把刚删的账号整行写回，表现为「删了又回来，
        # 删第二遍才行」）。tombstone 期间一律不落库，调用方无感。
        if _is_tombstoned(account.id):
            logger.warning("[store] 拦截已删除账号回写: %s (tombstone)", account.id)
            return account
        row = conn.execute(
            "SELECT * FROM accounts WHERE id=? AND platform=?", (account.id, platform)
        ).fetchone()
        existing = _row_to_account(row) if row else None
        if existing:
            account.created_at = existing.created_at
        else:
            account.created_at = now
        account.last_used = now
        # 防复活防线 2：软删除账号不允许被后台快照/状态操作覆盖回正常，
        # 恢复只能走显式恢复接口（restore_account / revive_account）。
        if existing and existing.status == "deleted" and account.status != "deleted":
            account.status = "deleted"

        conn.execute("""
            INSERT INTO accounts (
                id, platform, email, uid, nickname,
                enterprise_id, enterprise_name,
                access_token, refresh_token, token_type, expires_at, domain,
                status, tags,
                last_checkin_time, checkin_streak, quota_raw, auth_raw,
                created_at, last_used
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                tags=COALESCE(excluded.tags, accounts.tags),
                last_checkin_time=COALESCE(excluded.last_checkin_time, accounts.last_checkin_time),
                checkin_streak=CASE WHEN excluded.checkin_streak > 0 THEN excluded.checkin_streak ELSE accounts.checkin_streak END,
                quota_raw=COALESCE(excluded.quota_raw, accounts.quota_raw),
                auth_raw=COALESCE(excluded.auth_raw, accounts.auth_raw),
                created_at=accounts.created_at,
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
            json.dumps(account.auth_raw, ensure_ascii=False) if account.auth_raw else None,
            account.created_at, account.last_used,
        ))
        conn.commit()
        return account
    finally:
        conn.close()


def save_fingerprint(platform: str, account_id: str, fingerprint: Optional[dict]):
    """独立 UPDATE 指纹列：快照类并发回写（upsert）不携带指纹，因此不会覆盖已保存值。"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE accounts SET fingerprint=? WHERE id=? AND platform=? AND status != 'deleted'",
            (json.dumps(fingerprint, ensure_ascii=False) if fingerprint else None,
             account_id, platform),
        )
        conn.commit()
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
        "SELECT * FROM accounts WHERE id=? AND platform=? AND status != 'deleted'",
        (account_id, platform)
    ).fetchone()
    if not row:
        return None
    return _row_to_account(row)


def _row_to_account(row: sqlite3.Row) -> Account:
    tags = json.loads(row["tags"]) if row["tags"] else None
    quota_raw = json.loads(row["quota_raw"]) if row["quota_raw"] else None
    fingerprint = json.loads(row["fingerprint"]) if row["fingerprint"] else None
    auth_raw = json.loads(row["auth_raw"]) if row["auth_raw"] else None
    return Account(
        id=row["id"], email=row["email"] or "", uid=row["uid"],
        nickname=row["nickname"],
        enterprise_id=row["enterprise_id"],
        enterprise_name=row["enterprise_name"],
        tags=tags,
        fingerprint=fingerprint,
        access_token=row["access_token"] or "",
        refresh_token=row["refresh_token"],
        token_type=row["token_type"] or "Bearer",
        expires_at=row["expires_at"], domain=row["domain"],
        status=row["status"] or "normal",
        last_checkin_time=row["last_checkin_time"],
        checkin_streak=row["checkin_streak"] or 0,
        quota_raw=quota_raw,
        auth_raw=auth_raw,
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
            "SELECT * FROM accounts WHERE platform=? AND status != 'deleted' ORDER BY created_at DESC, id ASC", (platform,)
        ).fetchall()
        return [_row_to_account(r) for r in rows]
    finally:
        conn.close()


def list_deleted_accounts(platform: str) -> list[dict]:
    """回收站列表：返回原状态（软删前 normal/banned/disabled）+ 删除时间 + 删除批次的精简 dict。
    展示和筛选用原状态——用户关心的是恢复后它回到什么状态。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, email, nickname, deleted_at, delete_batch, batch_note FROM accounts "
            "WHERE platform=? AND status='deleted' ORDER BY deleted_at DESC, id ASC",
            (platform,)
        ).fetchall()
        states = _soft_states()
        return [
            {
                "id": r["id"],
                "email": r["email"] or "",
                "nickname": r["nickname"],
                "status": states.get(r["id"], "normal"),
                "deleted_at": r["deleted_at"] or 0,
                "batch": r["delete_batch"],
                "note": r["batch_note"] or "",
            }
            for r in rows
        ]
    finally:
        conn.close()


def soft_delete_account(platform: str, account_id: str, batch: Optional[int] = None, note: str = ""):
    """软删除：status='deleted' + 记录删除时间（及删除批次），数据保留在库，各读路径自动过滤，可随时恢复。
    恢复时还原软删前的原状态（banned 还是 banned、disabled 还是 disabled）。
    batch：同一次批量删除共用同一批次号，回收站按批次整组恢复/彻底删除。
    note：批次备注（可选，回收站展示/查找用），仅软删除（有批次）才填。
    整个 SELECT+UPDATE+_soft_states 写入在同一锁内，与 restore 互斥，无竞态窗口。"""
    with _soft_states_lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM accounts WHERE id=? AND platform=? AND status != 'deleted'",
                (account_id, platform)
            ).fetchone()
            if not row:
                return
            if note:
                # 带备注：写入批次备注
                conn.execute(
                    "UPDATE accounts SET status='deleted', deleted_at=?, delete_batch=?, batch_note=? "
                    "WHERE id=? AND platform=? AND status != 'deleted'",
                    (int(time.time()), batch, note, account_id, platform)
                )
            else:
                # 无备注：只写状态+批次，不覆盖批次已有备注（同批次先删的号带了备注时保留）
                conn.execute(
                    "UPDATE accounts SET status='deleted', deleted_at=?, delete_batch=? "
                    "WHERE id=? AND platform=? AND status != 'deleted'",
                    (int(time.time()), batch, account_id, platform)
                )
            conn.commit()
            prev = row["status"] or "normal"
        finally:
            conn.close()
        st = _soft_states()
        st[account_id] = prev
        _save_soft_states(st)


def restore_account(platform: str, account_id: str) -> bool:
    """软删除恢复：去掉 deleted 标记、清删除时间/批次，还原软删前的原状态（仅作用于软删除的账号）。
    整个读 prev+UPDATE+清记录在同一锁内，与 soft_delete 互斥，无竞态窗口。"""
    with _soft_states_lock:
        st = _soft_states()
        prev = st.get(account_id, "normal")
        conn = _get_conn()
        try:
            cur = conn.execute(
                "UPDATE accounts SET status=?, deleted_at=NULL, delete_batch=NULL, batch_note=NULL "
                "WHERE id=? AND platform=? AND status='deleted'",
                (prev, account_id, platform)
            )
            conn.commit()
            ok = cur.rowcount > 0
        finally:
            conn.close()
        if ok and account_id in st:
            st.pop(account_id)
            _save_soft_states(st)
        return ok


def revive_account(platform: str, account_id: str) -> bool:
    """显式恢复（重新导入 / OAuth 登录同号）：清 tombstone + 软删号回原状态。"""
    clear_tombstone(account_id)
    return restore_account(platform, account_id)


def list_batch_accounts(platform: str, batch: int) -> list[str]:
    """按删除批次列出回收站账号 id（用于整组恢复/彻底删除）。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM accounts WHERE platform=? AND status='deleted' AND delete_batch=?",
            (platform, batch)
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


def destroy_batch(platform: str, batch: int) -> int:
    """按批次彻底删除：物理删除该批次所有账号（走硬删除路径，tombstone + 清统计）。"""
    ids = list_batch_accounts(platform, batch)
    destroyed = 0
    for aid in ids:
        try:
            remove_account(platform, aid)
            destroyed += 1
        except Exception:
            pass
    return destroyed


def set_batch_note(platform: str, batch: int, note: str):
    """批次备注：编辑/清空回收站某个删除批次的备注（该批次所有账号同步）。"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE accounts SET batch_note=? WHERE platform=? AND status='deleted' AND delete_batch=?",
            (note or None, platform, batch)
        )
        conn.commit()
    finally:
        conn.close()


# ── 软删除原状态记录：恢复时还原（banned 还是 banned，不强制回 normal）──
_SOFT_DELETE_STATES_KEY = "soft_deleted_states"
_soft_states_lock = threading.RLock()  # 读-改-写跨连接，防并发丢原状态记录


def _soft_states() -> dict:
    try:
        raw = json.loads(get_setting(_SOFT_DELETE_STATES_KEY, "{}"))
        if not isinstance(raw, dict):
            raw = {}
    except Exception as e:
        logger.warning("[store] 读取软删原状态映射失败，恢复将默认回 normal: %r", e)
        raw = {}
    return raw


def _save_soft_states(states: dict):
    try:
        save_setting(_SOFT_DELETE_STATES_KEY, json.dumps(states))
    except Exception as e:
        # 原状态映射丢失会让 banned/disabled 账号被错误恢复成 normal，必须可见
        logger.warning("[store] 保存软删原状态映射失败，恢复可能丢失原状态: %r", e)


# ── 硬删除 tombstone：永久防快照类并发回写复活 ──
# 24h TTL 曾导致理论复活窗口（极长寿命快照循环）；改为永久，由 revive_account
# （重新导入/OAuth 同号）显式清理，兼顾「删了想导回」场景。
_DELETED_TOMBSTONE_KEY = "deleted_tombstones"
_tombstones_lock = threading.RLock()  # 读-改-写跨连接，防并发丢 tombstone（可重入：锁内再调 _tombstones()）


def _tombstones() -> dict:
    with _tombstones_lock:
        try:
            raw = json.loads(get_setting(_DELETED_TOMBSTONE_KEY, "{}"))
            if not isinstance(raw, dict):
                raw = {}
        except Exception as e:
            logger.warning("[store] 读取 tombstone 失败: %r", e)
            raw = {}
        return raw


def _is_tombstoned(account_id: str) -> bool:
    return account_id in _tombstones()


def clear_tombstone(account_id: str):
    with _tombstones_lock:
        try:
            ts = _tombstones()
            if account_id in ts:
                ts.pop(account_id)
                save_setting(_DELETED_TOMBSTONE_KEY, json.dumps(ts))
        except Exception:
            pass


def remove_account(platform: str, account_id: str):
    """硬删除。tombstone 与 DELETE + 统计清理同一连接同一事务原子写入：
    快照类并发 upsert 要么看到已删无行、要么看到 tombstone，不存在中间窗口。"""
    conn = _get_conn()
    try:
        with _tombstones_lock:
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (_DELETED_TOMBSTONE_KEY,)
            ).fetchone()
            try:
                raw = json.loads(row["value"]) if row and row["value"] else {}
                if not isinstance(raw, dict):
                    raw = {}
            except Exception:
                raw = {}
            raw[account_id] = time.time()
            conn.execute(
                "DELETE FROM accounts WHERE id=? AND platform=?", (account_id, platform)
            )
            # 同步清理统计行，避免孤儿数据污染汇总（B3）
            conn.execute(
                "DELETE FROM account_stats WHERE account_id=? AND platform=?", (account_id, platform)
            )
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_DELETED_TOMBSTONE_KEY, json.dumps(raw)),
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
    # 立即重新生成 theme.js，保证刷新页面时读到的是最新主题。
    # 旧 bug：只在 main.py 启动时生成，用户切换主题后不重启就刷新，theme.js 还是旧值。
    try:
        regenerate_theme_js(theme)
    except Exception:
        pass


def load_theme() -> str:
    try:
        return THEME_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return "light"


def regenerate_theme_js(theme: str):
    """根据主题生成 src/gui/theme.js，让前端在 <head> 阶段同步设置 data-theme。

    消除「首次进入/刷新时默认暗色一闪而过」：旧流程要等 Vue mount + IPC 往返
    才设 data-theme，期间用 :root 默认暗色渲染了一帧。改为 theme.js 在 CSS
    应用前就把 data-theme 设对。

    在两处调用：main.py 启动时、save_theme 切换主题时。
    """
    import sys
    if theme not in ("light", "dark"):
        theme = "light"
    # 找 gui 目录：dev 模式在源码树，打包模式在 sys._MEIPASS（只读，写入会失败，
    # 由调用方 try/except 兜底，回退到 onMounted 的 IPC 读主题）
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = os.path.dirname(base)  # src/storage → src → 项目根
    theme_js_path = os.path.join(base, "src", "gui", "theme.js")
    t_repr = repr(theme)
    # localStorage 优先（刷新时浏览器持久存储，不重启 Python 时 theme.js 不会重写，
    # 靠 localStorage 保留用户上次选择）；Python theme.txt 兜底（首次进入无 localStorage）。
    # window.__THEME__ 必须取最终解析值，否则 Vue onMounted 读到硬编码旧值会把 data-theme 覆盖错。
    content = (
        f'window.__THEME__=(function(){{'
        f'var d={t_repr};'
        f'try{{'
        f'var s=localStorage.getItem("theme");'
        f'if(s==="light"||s==="dark")d=s;'
        f'document.documentElement.setAttribute("data-theme",d);'
        f'}}catch(e){{document.documentElement.setAttribute("data-theme",d);}}'
        f'return d;'
        f'}})();'
    )
    with open(theme_js_path, "w", encoding="utf-8") as f:
        f.write(content)


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


def mark_offline_used(code: str, expires_at=None, machine_code: str = ""):
    """登记已使用的离线授权码（防重用，落库不依赖激活码缓存文件）。"""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO offline_license_records (code, used_at, expires_at, machine_code) VALUES (?,?,?,?)",
            (code, int(time.time()), expires_at, machine_code),
        )
        conn.commit()
    finally:
        conn.close()


def is_offline_used(code: str) -> bool:
    """查询离线授权码是否已使用。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM offline_license_records WHERE code=?", (code,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_offline_record(code: str):
    """查询离线授权码记录（含 expires_at），无则返回 None。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT code, used_at, expires_at, machine_code FROM offline_license_records WHERE code=?", (code,)
        ).fetchone()
        return dict(row) if row else None
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


def add_switch_log(platform: str, from_id: str, from_name: str,
                   to_id: str, to_name: str, reason: str = ""):
    """记录一次切号（from → to）。不受 log_enabled 开关影响 —— 切号是调度关键事件，
    必须留痕以便排查"额度未耗尽却提前切号"类问题。回收站账号不在调度池内，天然不会出现。"""
    conn = _get_conn()
    try:
        msg = f"切号 {from_name} → {to_name}"
        if reason:
            msg += f" ({reason})"
        conn.execute(
            "INSERT INTO proxy_logs (timestamp, event, platform, account_id, account_name, model, message, details) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(time.time()), "switch", platform, from_id, from_name, "", msg, reason),
        )
        conn.commit()
        _prune_logs(conn)
    except Exception:
        pass
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
        today = time.strftime("%Y-%m-%d", time.localtime(now))
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


def get_account_stats(platform: str, account_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT s.* FROM account_stats s "
            "LEFT JOIN accounts a ON s.account_id=a.id AND s.platform=a.platform "
            "WHERE s.account_id=? AND s.platform=? AND (a.status IS NULL OR a.status != 'deleted')",
            (account_id, platform)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_account_stats(platform: str) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT s.* FROM account_stats s "
            "LEFT JOIN accounts a ON s.account_id=a.id AND s.platform=a.platform "
            "WHERE s.platform=? AND (a.status IS NULL OR a.status != 'deleted') ORDER BY s.updated_at DESC",
            (platform,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_usage_summary(platform: str) -> dict:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as active_accounts, "
            "COALESCE(SUM(s.request_count),0) as total_requests, "
            "COALESCE(SUM(s.prompt_tokens),0) as prompt_tokens, "
            "COALESCE(SUM(s.completion_tokens),0) as completion_tokens, "
            "COALESCE(SUM(s.total_tokens),0) as total_tokens, "
            "COALESCE(SUM(s.total_credit),0) as total_credit, "
            "COALESCE(SUM(s.cache_hits),0) as cache_hits, "
            "COALESCE(SUM(s.cache_misses),0) as cache_misses "
            "FROM account_stats s "
            "LEFT JOIN accounts a ON s.account_id=a.id AND s.platform=a.platform "
            "WHERE s.platform=? AND (a.status IS NULL OR a.status != 'deleted')", (platform,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def reset_account_credit(platform: str, account_id: str):
    """刷新额度后清零本次消耗积分（total_credit），保留累计 lifetime_credit。
    原先与 reset_account_stats 同名被覆盖成死代码，导致积分条长期漂移。"""
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
    """清空统计行（删行）：单号 / 整平台 / 全部。"""
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
