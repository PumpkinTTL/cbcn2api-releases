import json
import logging

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.codebuddy.cn"

PLATFORM_CONFIG = {
    "codebuddy_cn": {
        "platform": "ide",
        "login_prefix": "cb_",
    },
    "workbuddy": {
        "platform": "workbuddy",
        "login_prefix": "wb_",
    },
}

# 上游响应里值得落日志的关键字段白名单（其余不记录，避免刷屏/漏关键）
_UPSTREAM_LOG_KEYS = (
    "code", "msg", "message", "requestId", "state", "uid",
    "TotalCount", "TotalDosage", "dosageNotifyCode", "paymentType",
    "accessToken", "refreshToken",
)


def _log_upstream(op: str, account, url: str, resp: requests.Response):
    """记录一次上游失败交互（受统一日志开关 log_enabled 控制，add_log 内部检查）。

    成功探活（签到状态/额度查询 200 OK）是高频噪音，不落日志；
    只记录 FAIL / 非 200 / 业务码异常 —— 这些才是排查 6004/14018/11140 等问题的关键。
    只截取白名单字段（code/msg/额度数字等），不落完整 JSON。
    """
    # 账号标识：email 或 nickname 截断（不落完整 id，缩短日志行）
    name = ""
    account_id = ""
    try:
        if account is not None:
            name = (account.nickname or account.email or "")[:16]
            account_id = account.id
    except Exception:
        pass

    summary = {}
    body_text = ""
    raw_json = None
    try:
        data = resp.json()
        if isinstance(data, dict):
            raw_json = data
            for k in _UPSTREAM_LOG_KEYS:
                if k in data:
                    summary[k] = data[k]
            d = data.get("data")
            if isinstance(d, dict):
                for k in _UPSTREAM_LOG_KEYS:
                    if k in d:
                        summary[k] = d[k]
    except Exception:
        body_text = (resp.text or "")[:120]

    path = url.replace(BASE_URL, "")
    code = summary.get("code", summary.get("msg", ""))
    msg = summary.get("msg") or summary.get("message") or ""
    ok = "OK" if resp.status_code == 200 and (summary.get("code", 0) in (0, 200, None)) else "FAIL"
    message = f"{op} → HTTP {resp.status_code} {ok}"
    if msg and msg not in (ok, ""):
        message += f" | {msg}"
    details = {"url": path, "http": resp.status_code, **summary}
    # 附加上游返回的 JSON 关键内容（紧凑单行，超长截断 —— 排查时要看原始返回）
    if raw_json is not None:
        try:
            compact = json.dumps(raw_json, ensure_ascii=False, separators=(",", ":"))
            details["resp"] = compact[:600] + ("..." if len(compact) > 600 else "")
        except Exception:
            pass
    details = json.dumps(details, ensure_ascii=False)
    if body_text:
        details += f" | body: {body_text}"
    if ok == "OK":
        return
    try:
        from src.storage.store import add_log
        add_log("upstream", "workbuddy", account_id, name, "", message, details[:1000])
    except Exception as e:
        logger.warning("[上游日志] 写入失败: %r", e)


def api_request(session: requests.Session, method: str, url: str, op: str = "",
                account=None, **kwargs) -> requests.Response:
    """带日志的上游请求：与 session.request 同签名，请求后自动记录上游交互日志。"""
    resp = session.request(method, url, **kwargs)
    try:
        _log_upstream(op or method, account, url, resp)
    except Exception:
        pass
    return resp


def build_headers(access_token: str = None, uid: str = None,
                  enterprise_id: str = None, domain: str = None) -> dict:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        # 客户端身份头（与网关转发链 src/proxy/api_client.py 的 workbuddy profile 一致）：
        # 上游 get-user-resource 等额度接口校验 UA，缺失返回 10085「请求不合法」，
        # 且对"无 UA/身份头异常"的请求有封号风控风险 —— 所有上游请求必须成套携带。
        "User-Agent": "WorkBuddy/5.2.6 WorkBuddy/5.2.6 CLI/2.106.4",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": "5.2.6",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if uid:
        headers["X-User-Id"] = uid
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    if domain:
        headers["X-Domain"] = domain
    return headers


def get_session() -> requests.Session:
    return requests.Session()
