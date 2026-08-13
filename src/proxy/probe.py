"""账号封禁探测：发真实 chat 请求判定是否被上游封号。

被封禁的账号能签到、能拉额度（上游不拦这些接口），但发 chat 请求会被
11140 拦截。因此判定封号必须发真实请求，只看额度接口是否成功无效。

流程：用便宜模型连发 3 次（hy3 / hy3 / deepseek-v4-flash，stream），
  Hy3 优先，若 Hy3 被限流/失败则兜底 deepseek-v4-flash：
  全 11140 → "banned"（封号）
  任意 200 → "ok"（账号活着）
  其他错误/网络异常 → "unknown"（不算封号证据，不判定）
"""
import logging

from src.proxy.api_client import build_headers, CHAT_API_BASE

logger = logging.getLogger(__name__)

_PROBE_MODELS = ("hy3", "hy3", "deepseek-v4-flash")


def probe_chat_available(account, access_token: str) -> str:
    """返回 'ok' | 'banned' | 'unknown'。"""
    from src.api.client import get_session

    headers = build_headers(access_token, user_id=account.uid or None, fingerprint=account.fingerprint)
    headers["X-TUID"] = account.uid or ""
    headers["x-traffic-id"] = account.uid or ""

    fail_count = 0
    session = get_session()
    for model in _PROBE_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "max_tokens": 3,
        }
        try:
            with session.post(
                f"{CHAT_API_BASE}/v2/chat/completions",
                json=payload, headers=headers,
                timeout=(10, 20), stream=True,
            ) as resp:
                if resp.status_code == 200:
                    return "ok"
                body = ""
                for chunk in resp.iter_content(1024):
                    body += chunk.decode("utf-8", errors="ignore")
                    if len(body) > 200:
                        break
                if "11140" in body or "request illegal" in body.lower():
                    fail_count += 1
                else:
                    return "unknown"
        except Exception:
            return "unknown"
    return "banned" if fail_count >= 3 else "unknown"
