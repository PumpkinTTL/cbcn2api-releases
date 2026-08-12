"""Grok 转发核心：账号池 + 伪装 headers + Responses 透传。

刻意不复用 src/proxy/token_rotator（那是单 platform 单例，CodeBuddy 专用）。
这里实现一个独立的、简化的 Grok 账号池（粘性优先 + 冷却换号），与 CodeBuddy 完全隔离。

转发策略（首期）：Responses 格式透传。
  客户端用 OpenAI Responses API 原生接入（/v1/responses），
  本模块只做「选号 + 伪装指纹 + 字段清洗 + SSE 字节流透传」，不做 Chat↔Responses 转换。
  Chat 兼容留待后续（sidecar 或转换层）。
"""
import asyncio
import logging
import time
import threading
import uuid
from typing import AsyncGenerator, Optional

import httpx

from src.storage import store
from src.models.account import Account

from . import config

logger = logging.getLogger(__name__)

# 冷却时长（秒）—— 与 token_rotator 对齐语义，但独立常量
COOLDOWN_QUOTA = 3600      # 额度耗尽（429 / credits 不足）
COOLDOWN_AUTH = 600        # 鉴权失败（401）
COOLDOWN_TRANSIENT = 60    # 临时错误（5xx / 超时）

# 单次转发流式总时长上限
STREAM_DEADLINE = 600

_http_client: Optional[httpx.AsyncClient] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_client() -> httpx.AsyncClient:
    """惰性创建 httpx client，绑定当前事件循环（重启网关后 loop 变化需重建）。"""
    global _http_client, _client_loop
    loop = asyncio.get_event_loop()
    if _http_client is None or _client_loop is not loop:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(STREAM_DEADLINE, connect=15.0))
        _client_loop = loop
    return _http_client


# 进程级稳定 agent id（模拟「单机单 agent」，对齐官方 CLI 每进程一个 device/agent 标识）。
# 9router 用 getConsistentMachineId 取机器码；这里没有机器码服务，用进程级常量近似。
_AGENT_ID = str(uuid.uuid4())
# 用 prompt_cache_key 派生稳定 session id 的命名空间
_SESSION_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "cbcn2api.grok.session")


def _resolve_session_id(body: dict) -> str:
    """同一会话尽量返回稳定 id（官方 CLI 在一个会话内复用 session/conv id）。

    优先按客户端的 prompt_cache_key 派生（Responses 客户端常带，OpenAI 用作缓存键），
    派生是确定性的 → 同一 key 跨请求得到同一 id，多轮时 session/turn 指纹才正常。
    没有就退化为每请求新 uuid（视作单轮）。
    """
    pck = body.get("prompt_cache_key")
    if isinstance(pck, str) and pck:
        return str(uuid.uuid5(_SESSION_NS, pck))
    return str(uuid.uuid4())


def _count_user_turns(body: dict) -> int:
    """数 Responses input 里的用户消息（1-based，对齐官方 x-grok-turn-idx 语义）。

    官方 HAR：首轮 turn-idx=1，约等于用户消息数。全历史客户端天然递增。
    """
    inp = body.get("input")
    if not isinstance(inp, list):
        return 1
    n = 0
    for item in inp:
        if isinstance(item, dict) and item.get("role") == "user":
            t = item.get("type")
            if not t or t == "message":  # Responses 消息项 type 省略或 "message"
                n += 1
    return max(1, n)


def build_headers(access_token: str, body: dict, account: Account) -> dict:
    """伪装官方 grok CLI 0.2.99 的请求头（逐字节对齐 wire capture）。

    除静态指纹外，补齐官方 CLI 每请求都带的会话/身份头（来自 9router grok-cli.js
    buildHeaders 的 HAR 还原）。缺这些头，cli-chat-proxy 可能按非官方客户端降级。

      - x-grok-session-id / x-grok-conv-id：同一会话稳定（按 prompt_cache_key 派生）
      - x-grok-req-id：每请求新 uuid
      - x-grok-turn-idx：1-based 用户消息计数
      - x-grok-agent-id：进程级稳定（模拟单机单 agent）
      - x-grok-model-override：解析后的上游模型
      - x-email / x-userid：账号身份
    """
    session_id = _resolve_session_id(body)
    h = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": config.USER_AGENT,
        "x-grok-client-identifier": config.CLIENT_IDENTIFIER,
        "x-grok-client-version": config.CLIENT_VERSION,
        # cli-chat-proxy 的鉴权约定：看这个自定义头而非 Authorization
        "x-xai-token-auth": config.TOKEN_AUTH_HEADER_VALUE,
        "x-grok-session-id": session_id,
        # CLI 在 chat 轮次里 conv-id 与 session-id 同值
        "x-grok-conv-id": session_id,
        "x-grok-req-id": str(uuid.uuid4()),
        "x-grok-turn-idx": str(_count_user_turns(body)),
        "x-grok-agent-id": _AGENT_ID,
        "x-grok-model-override": body.get("model") or config.GROK_BUILD_MODEL,
    }
    if account and account.email:
        h["x-email"] = account.email
    if account and account.uid:
        h["x-userid"] = account.uid
    return h


def _resolve_model(model: str) -> tuple[str, Optional[str]]:
    """拆解模型 id 与 effort 后缀。

    grok-4.5-high → (grok-4.5, high)
    grok-build    → (grok-build, None)
    """
    if not model:
        return config.GROK_BUILD_MODEL, None
    for eff in ("xhigh", "high", "medium", "low"):
        suffix = f"-{eff}"
        if model.endswith(suffix):
            base = model[: -len(suffix)]
            if base in ("grok-4.5",):
                return base, eff
    return model, None


def _normalize_effort(effort: Optional[str]) -> Optional[str]:
    if effort in ("xhigh", "high", "medium", "low"):
        return effort
    return None


def transform_request(body: dict) -> dict:
    """把客户端请求清洗成 cli-chat-proxy 接受的 Responses 格式。

    对照 9router executors/grok-cli.js transformRequest：
      - 字段白名单过滤（剔除 Chat 残留 messages/max_tokens 等）
      - stream=True / store=False（无状态，previous_response_id 必须删）
      - reasoning.effort 仅 grok-4.5 系支持；grok-build 会被上游拒绝
      - 总是带 reasoning.summary=concise + include reasoning.encrypted_content

    TODO（首期未做，多轮/工具调用需要时再补）：
      - stripStoredItemReferences：剔除 item_reference 与服务端生成 id（rs_/fc_/resp_/msg_，
        store=False 时上游无法解析，会 400）
      - normalizeGrokCliInput / normalizeGrokCliTools：custom_tool_call↔function_call 形状
        转换、Chat tools[] 扁平化、function_call_output 配对过滤
      故当前仅支持「单轮纯文本」透传；带历史 reasoning.encrypted_content 的多轮和工具调用可能 400。
    """
    payload = dict(body)
    resolved, effort_from_suffix = _resolve_model(str(payload.get("model", config.GROK_BUILD_MODEL)))

    # reasoning.effort 来源优先级：显式 reasoning.effort > reasoning_effort > 模型后缀
    reasoning = payload.get("reasoning")
    explicit_effort = (reasoning or {}).get("effort") if isinstance(reasoning, dict) else None
    reasoning_effort_field = payload.get("reasoning_effort")
    effort = _normalize_effort(explicit_effort or reasoning_effort_field or effort_from_suffix)

    payload["reasoning"] = {"summary": "concise"}
    if config.supports_reasoning_effort(resolved) and effort:
        payload["reasoning"]["effort"] = effort
    # grok-build 不接受 effort，但 summary / encrypted continuity 仍要带

    # 多轮加密连续性（官方 CLI 总是请求）
    include = list(payload.get("include") or [])
    if "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")
    payload["include"] = include

    payload["model"] = resolved
    payload["stream"] = True
    payload["store"] = False

    # 剔除 Chat Completions 残留字段（Responses 端点会 400）
    for k in (
        "messages", "max_tokens", "max_completion_tokens", "n", "seed",
        "logprobs", "top_logprobs", "frequency_penalty", "presence_penalty",
        "logit_bias", "user", "stream_options", "prompt_cache_retention",
        "safety_identifier", "previous_response_id", "reasoning_effort",
    ):
        payload.pop(k, None)

    # 白名单兜底
    for k in list(payload.keys()):
        if k not in config.RESPONSES_ALLOWLIST:
            payload.pop(k, None)

    return payload


class GrokPool:
    """独立账号池（粘性优先 + 冷却换号）。

    不依赖 token_rotator，从 store.list_accounts("grok") 加载。
    与 CodeBuddy 的池互不影响。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self._current_id: Optional[str] = None
        # id → {"reason": str, "until": float}
        self._disabled: dict[str, dict] = {}
        self._loaded: bool = False

    def ensure_loaded(self):
        with self._lock:
            if not self._accounts:
                self.reload()

    def reload(self):
        with self._lock:
            self._accounts = [a for a in store.list_accounts(config.PLATFORM_KEY) if a.access_token]
            self._loaded = True
            # 校验当前锁定仍可用
            if self._current_id:
                cur = next((a for a in self._accounts if a.id == self._current_id), None)
                if not cur or not self._is_usable(cur):
                    self._current_id = None
            if not self._current_id:
                for acc in self._accounts:
                    if self._is_usable(acc):
                        self._current_id = acc.id
                        break

    def count_usable(self) -> int:
        with self._lock:
            return sum(1 for a in self._accounts if self._is_usable(a))

    def _is_usable(self, acc: Account) -> bool:
        if not acc.access_token:
            return False
        if acc.status in ("disabled", "banned"):
            return False
        st = self._disabled.get(acc.id)
        if st:
            if st.get("until", 0) > time.time():
                return False
            self._disabled.pop(acc.id, None)
        return True

    def get_next(self) -> Optional[Account]:
        """粘性返回当前号；不可用则切下一个可用号。"""
        with self._lock:
            if self._current_id:
                cur = next((a for a in self._accounts if a.id == self._current_id), None)
                if cur and self._is_usable(cur):
                    return cur
            # 选第一个可用
            for acc in self._accounts:
                if self._is_usable(acc):
                    self._current_id = acc.id
                    return acc
            self._current_id = None
            return None

    def mark_disabled(self, account_id: str, reason: str):
        with self._lock:
            cd = {
                "quota": COOLDOWN_QUOTA,
                "auth": COOLDOWN_AUTH,
                "transient": COOLDOWN_TRANSIENT,
            }.get(reason, COOLDOWN_TRANSIENT)
            self._disabled[account_id] = {"reason": reason, "until": time.time() + cd}
            if self._current_id == account_id:
                self._current_id = None
            logger.info("[grok] 账号 %s 冷却 %s (%ss)", account_id, reason, cd)


grok_pool = GrokPool()


def _classify_error(status: int, body: str) -> Optional[str]:
    """按 HTTP 状态码分类上游错误 → 冷却类型。"""
    if status in (429,):
        return "quota"
    if status in (401, 403):
        return "auth"
    if status in (502, 503, 504):
        return "transient"
    return None


async def handle_request(
    payload: dict,
    on_log=None,
) -> AsyncGenerator[str, None]:
    """Responses 透传主流程：选号 → 伪装转发 → SSE 字节流透传，故障自动换号。

    on_log: 可选回调 (event, account_id, nickname, model, message, details) → 用于日志落库。
    """
    import json

    body = transform_request(payload)
    model = body.get("model", "")
    api_url = f"{config.BASE_URL}/responses"

    grok_pool.ensure_loaded()
    max_attempts = max(grok_pool.count_usable(), 1)
    deadline = time.monotonic() + STREAM_DEADLINE
    last_msg = "无可用 Grok 账号"

    for _attempt in range(max_attempts):
        if time.monotonic() > deadline:
            last_msg = "请求超过总时长上限"
            break
        acc = grok_pool.get_next()
        if not acc:
            break

        if on_log:
            on_log("request", acc.id, acc.nickname or acc.email, model, "开始请求", "")

        headers = build_headers(acc.access_token, body, acc)
        client = _get_client()

        try:
            resp = await client.send(
                client.build_request("POST", api_url, json=body, headers=headers),
                stream=True,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning("[grok] 账号=%s 连接失败: %s", acc.nickname, e)
            grok_pool.mark_disabled(acc.id, "transient")
            last_msg = f"无法连接上游: {e}"
            continue

        try:
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", errors="ignore")
                await resp.aclose()
                kind = _classify_error(resp.status_code, raw)
                logger.warning("[grok] 账号=%s 上游 %d kind=%s body=%s",
                               acc.nickname, resp.status_code, kind, raw[:200])
                if on_log:
                    on_log("error", acc.id, acc.nickname or acc.email, model,
                           f"HTTP {resp.status_code} → {kind or '不可重试'}", raw[:500])
                if kind:
                    grok_pool.mark_disabled(acc.id, kind)
                    last_msg = raw[:300]
                    continue
                # 不可重试的错误直接透传给客户端
                err = json.dumps({"error": {"message": raw, "type": "upstream_error",
                                            "status": resp.status_code}}, ensure_ascii=False)
                yield f"data: {err}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 200：SSE 字节流透传（Responses 格式原样转发）
            sent_any = False
            async for chunk in resp.aiter_text(chunk_size=8192):
                sent_any = True
                yield chunk
            if on_log and sent_any:
                on_log("success", acc.id, acc.nickname or acc.email, model, "请求完成", "")
            return
        finally:
            await resp.aclose()

    # 所有账号都失败
    err = json.dumps({"error": {"message": last_msg, "type": "no_account"}}, ensure_ascii=False)
    yield f"data: {err}\n\n"
    yield "data: [DONE]\n\n"
