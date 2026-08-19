"""账号封禁探测：发真实 chat 请求判定是否被上游封号。

被封禁的账号能签到、能拉额度（上游不拦这些接口），但发 chat 请求会被
11140 拦截。因此判定封号必须发真实请求，只看额度接口是否成功无效。

流程：用便宜模型连发 3 次（hy3 / hy3 / deepseek-v4-flash，stream），
  Hy3 优先，若 Hy3 被限流/失败则兜底 deepseek-v4-flash：
  全 11140 → "banned"（封号）
  任意 200 → "ok"（账号活着）
  其他错误/网络异常 → "unknown"（不算封号证据，不判定）

指定 model 时：只探该模型，用于「单模型限流」的恢复判定——
  200 → ok（该模型已恢复）；6004/其他 → limited（仍限流，明确非封号）；11140 → banned。
"""
import logging

from src.proxy.api_client import build_headers, CHAT_API_BASE

logger = logging.getLogger(__name__)

_PROBE_MODELS = ("hy3", "hy3", "deepseek-v4-flash")


def probe_chat_available(account, access_token: str, model: str = None) -> str:
    """返回 'ok' | 'banned' | 'limited' | 'unknown'。

    - ok:      任意一次 200（账号/模型可用）
    - banned:  所有尝试都是 11140 / request illegal（封号证据确凿）
    - limited: 拿到上游真实响应但非 11140（限流 6004 / 额度 14018 / 403 / 5xx 等）
               → 明确「非封号」，只是受限；封禁判定不能只看状态码，必须看 body code
    - unknown: 只有网络异常（没拿到任何上游响应），无法判定
    """
    from src.api.client import get_session

    headers = build_headers(access_token, user_id=account.uid or None, fingerprint=account.fingerprint)
    headers["X-TUID"] = account.uid or ""
    headers["x-traffic-id"] = account.uid or ""

    models = [model] if model else list(_PROBE_MODELS)
    fail_count = 0
    session = get_session()
    for m in models:
        payload = {
            "model": m,
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
                    return "limited"
        except Exception:
            continue  # 网络异常 → 换下一个模型再试
    return "banned" if fail_count >= len(models) else "unknown"
