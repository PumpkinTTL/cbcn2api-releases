import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional, AsyncGenerator, AsyncIterator

import httpx
from fastapi import FastAPI, APIRouter, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .token_rotator import token_rotator
from .api_client import build_headers, build_chat_payload, resolve_base_url, AVAILABLE_MODELS
from src.storage.store import add_log, update_account_stats

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None
_proxy_password: str = os.environ.get("CBCN_PROXY_PASSWORD", "")
_platform: str = os.environ.get("CBCN_PROXY_PLATFORM", "workbuddy")
_port: int = int(os.environ.get("CBCN_PROXY_PORT", "8001"))


def update_config(port: int, password: str, platform: str):
    """更新网关配置（进程内模式：每次启动时调用，覆盖 import 时的值）。"""
    global _proxy_password, _platform, _port
    _proxy_password = password
    _platform = platform
    _port = port

security = HTTPBearer(auto_error=False)

# 上游错误分类
QUOTA_ERROR_CODES = {14018}                # 额度耗尽（body code）
QUOTA_STATUS_CODES = {429}                 # 额度耗尽（HTTP 429）
TRANSIENT_STATUS_CODES = {401, 502, 503, 504}  # 临时错误

PEEK_BYTE_LIMIT = 32768  # peek 阶段最多缓冲字节数

# httpx 的 read 超时是「单次 socket 读取」的间隔超时，不是总时长，httpx 也没有
# total timeout 的概念。上游只要每 <read 超时 发一个心跳字节（SSE 的 ": ping"
# 注释行会被 _normalize_text_stream 静默跳过），请求就能永久挂着。
# 下面两个是墙钟上限，用来兜住这种「滴水不断但永不结束」的情况。
PEEK_TIMEOUT = 25.0        # peek 阶段最长等待：超过就当作正常数据放行，先让客户端见到响应
STREAM_DEADLINE = 600.0    # 单次请求从建连到收尾的总时长上限


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            verify=False,
            trust_env=False,
            timeout=httpx.Timeout(60.0, connect=10.0, read=60.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
    return _http_client


async def _close_http_client():
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


def authenticate(creds: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not _proxy_password:
        return True
    if creds is None or creds.credentials != _proxy_password:
        raise HTTPException(status_code=403, detail="Invalid proxy password")
    return True


def parse_sse_line(line: str) -> Optional[dict]:
    if not line.startswith("data: "):
        return None
    data = line[6:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _classify_upstream_error(status_code: int, body: str) -> Optional[str]:
    """解析上游错误体，返回可重试类型：'quota' | 'auth' | 'transient' | None(不可重试)。"""
    code = None
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            data = err.get("data")
            if isinstance(data, dict) and data.get("code") is not None:
                code = data.get("code")
            elif err.get("code") is not None:
                code = err.get("code")
    if code in QUOTA_ERROR_CODES:
        return "quota"
    if status_code in QUOTA_STATUS_CODES:
        return "quota"
    if status_code == 403:
        return "auth"
    if status_code in TRANSIENT_STATUS_CODES:
        return "transient"
    return None


def _safe_log(*args, **kwargs):
    """add_log 的兜底包装：写日志失败绝不能影响请求本身。"""
    try:
        add_log(*args, **kwargs)
    except Exception as e:
        logger.warning("[调度] 写日志失败: %r", e)


def _first_event_kind(text: str):
    """扫描文本中的首个 SSE data 事件。
    返回 ('error', payload_str) | ('data', None) | ('none', None)。"""
    has_data = False
    for line in text.split("\n"):
        s = line.strip()
        if not s.startswith("data:"):
            continue
        payload_str = s[5:].strip()
        if not payload_str or payload_str == "[DONE]":
            continue
        try:
            obj = json.loads(payload_str)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
            return "error", payload_str
        if isinstance(obj, dict) and (obj.get("choices") or obj.get("id") or obj.get("usage")):
            has_data = True
    return ("data" if has_data else "none"), None


def _extract_consumed(usage: dict) -> float:
    """从 usage 对象提取消耗的积分/额度。"""
    for key in ("credit", "deduction", "cost", "credits", "consumed", "points"):
        v = usage.get(key)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 0.0


async def _normalize_text_stream(
    aiter: AsyncIterator[str],
    usage_box: Optional[dict] = None,
    deadline: Optional[float] = None,
) -> AsyncGenerator[str, None]:
    """规整上游 SSE，对照 9router passthrough 模式：
    - 只删空 tool_calls: []（CodeBuddy CN 每块都带，AI SDK 误判）
    - 补 object/created 若缺失
    - 丢弃无实质内容的空块（hasValuableContent）
    - 其余字段原样透传

    deadline 是 time.monotonic() 的绝对时刻；超过就补一个 [DONE] 收尾退出，
    避免上游慢速滴字导致这个 async for 永不结束。
    """
    buffer = ""
    async for chunk in aiter:
        if deadline is not None and time.monotonic() > deadline:
            logger.warning("[调度] 流式响应超过总时长上限，主动收尾")
            yield ("data: " + json.dumps(
                {"error": {"message": "响应超过总时长上限，已中断", "type": "timeout"}},
                ensure_ascii=False) + "\n\n")
            yield "data: [DONE]\n\n"
            return
        if not chunk:
            continue
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            stripped = line.strip()
            if stripped.startswith(":") or not stripped:
                continue
            if "[DONE]" in stripped:
                yield "data: [DONE]\n\n"
                return
            if stripped.startswith("data:"):
                payload_str = stripped[5:].strip()
                try:
                    obj = json.loads(payload_str)

                    # 补 object/created
                    if "choices" in obj:
                        obj.setdefault("object", "chat.completion.chunk")
                        obj.setdefault("created", int(time.time()))

                    # 捕获 usage（最后一个 chunk 携带）
                    if usage_box is not None and isinstance(obj.get("usage"), dict):
                        usage_box["usage"] = obj["usage"]

                    for choice in obj.get("choices", []):
                        delta = choice.get("delta", {})
                        # 删空 tool_calls: []（9router 专门为 CodeBuddy CN 加的修复）
                        tc = delta.get("tool_calls")
                        if isinstance(tc, list) and len(tc) == 0:
                            del delta["tool_calls"]

                    # hasValuableContent：丢弃无实质内容的块
                    choices = obj.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        has_content = (
                            (delta.get("content") is not None and delta["content"] != "")
                            or (delta.get("reasoning_content") is not None and delta["reasoning_content"] != "")
                            or (delta.get("tool_calls") and len(delta["tool_calls"]) > 0)
                            or choices[0].get("finish_reason")
                            or delta.get("role")
                        )
                        if not has_content:
                            continue

                    yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
                    continue
                except (json.JSONDecodeError, IndexError):
                    pass
            yield stripped + "\n\n"
    if buffer.strip() and not buffer.strip().startswith(":"):
        yield buffer.strip() + "\n\n"
    yield "data: [DONE]\n\n"




async def _stream_inner(
    payload: dict,
    conversation_id: Optional[str],
    platform: str,
) -> AsyncGenerator[str, None]:
    """带故障转移的流式生成器：首个账号出错（额度/鉴权）时自动切下一个账号，
    在向客户端发送任何内容之前完成切换。"""
    fallback = "codebuddy_cn" if platform != "codebuddy_cn" else "workbuddy"
    # count_usable 在池未加载时返回 0（get_next 才会触发 reload），max(...,1) 会让
    # 明明有 N 个号的情况只试一次。先确保池已加载再算重试次数。
    token_rotator.ensure_loaded(platform)
    max_attempts = max(token_rotator.count_usable(), 1)
    last_msg = "无可用账号"
    model = payload.get("model", "")
    deadline = time.monotonic() + STREAM_DEADLINE
    sent_any = False   # 是否已经向客户端 yield 过正文；一旦为 True 就不能再重试换号

    for _attempt in range(max_attempts):
        if time.monotonic() > deadline:
            last_msg = "请求超过总时长上限"
            break
        acc = token_rotator.get_next(platform) or token_rotator.get_next(fallback)
        if not acc:
            break

        # add_log 是同步 sqlite 写，GUI 线程并发写库时可能抛 "database is locked"。
        # 这里裸调会让异常从生成器里穿出去，把一次本来能成功的请求打成 500。
        _safe_log("request", platform, acc.id, acc.nickname, model, f"开始请求 model={model}", "")

        base_url = resolve_base_url()
        api_url = f"{base_url}/v2/chat/completions"
        headers = build_headers(acc.access_token, acc.uid, conversation_id)
        client = _get_http_client()

        try:
            resp = await client.send(
                client.build_request("POST", api_url, json=payload, headers=headers),
                stream=True,
            )
        except httpx.ConnectError as e:
            logger.warning("[调度] 账号=%s 连接失败: %s", acc.nickname, e)
            _safe_log("error", platform, acc.id, acc.nickname, model, f"连接失败: {e}", "")
            last_msg = f"无法连接上游: {e}"
            # 原先是 break 且不标记冷却 —— 单个账号连不上就放弃整轮，没有故障转移。
            # 连接失败按 transient 处理并继续换号，全部失败时循环自然走完。
            token_rotator.mark_disabled(acc.id, "transient")
            continue
        except httpx.TimeoutException as e:
            logger.warning("[调度] 账号=%s 超时, 标记transient: %s", acc.nickname, e)
            _safe_log("error", platform, acc.id, acc.nickname, model, f"请求超时", "")
            token_rotator.mark_disabled(acc.id, "transient")
            last_msg = f"上游超时: {e}"
            continue

        try:
            # 非 200：解析错误体，可重试则切号
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="ignore")
                await resp.aclose()
                kind = _classify_upstream_error(resp.status_code, body)
                logger.warning("[调度] 账号=%s 上游返回 %d, kind=%s, body=%s", acc.nickname, resp.status_code, kind, body[:200])
                if kind:
                    token_rotator.mark_disabled(acc.id, kind)
                    _safe_log("error", platform, acc.id, acc.nickname, model, f"HTTP {resp.status_code} → {kind}", body[:500])
                    last_msg = body[:300]
                    continue
                _safe_log("error", platform, acc.id, acc.nickname, model, f"HTTP {resp.status_code} (不可重试)", body[:500])
                yield f"data: {json.dumps({'error': {'message': body, 'type': 'upstream_error', 'status': resp.status_code}}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 200：peek 首个事件，检测内联错误，确认无误后再向客户端输出
            # 只创建一个迭代器，peek 和后续共用（httpx 不允许重复 aiter_text）
            text_iter = resp.aiter_text(chunk_size=8192)
            buffered = []
            buffered_len = 0
            decision = "none"
            inline_err = None
            peek_deadline = min(time.monotonic() + PEEK_TIMEOUT, deadline)
            async for chunk in text_iter:
                buffered.append(chunk)
                buffered_len += len(chunk)
                # 必须对**累积文本**解析：一个 SSE 错误事件可能被切在 8KB chunk
                # 边界上，两半各自 json.loads 都失败，于是 14018/429 检测不到、
                # 不触发换号，错误体直接透传给客户端，故障转移形同虚设。
                kind, err = _first_event_kind("".join(buffered))
                if kind == "error":
                    decision = "error"
                    inline_err = err
                    break
                if kind == "data":
                    decision = "data"
                    break
                if buffered_len > PEEK_BYTE_LIMIT:
                    decision = "data"
                    break
                # 只收到心跳/注释时，退出条件全是字节量的话会一直卡在这里，
                # 而响应头已经发出去了，客户端表现为「连上了但一个字都没有」。
                if time.monotonic() > peek_deadline:
                    logger.warning("[调度] 账号=%s peek 超时，按正常数据放行", acc.nickname)
                    decision = "data"
                    break

            if decision == "error" and inline_err:
                await resp.aclose()
                kind = _classify_upstream_error(200, inline_err)
                logger.warning("[调度] 账号=%s 200内联错误, kind=%s, err=%s", acc.nickname, kind, inline_err[:200])
                if kind:
                    token_rotator.mark_disabled(acc.id, kind)
                    _safe_log("error", platform, acc.id, acc.nickname, model, f"200内联错误 → {kind}", inline_err[:500])
                    last_msg = inline_err[:300]
                    continue
                _safe_log("error", platform, acc.id, acc.nickname, model, "200内联错误(不可重试)", inline_err[:500])
                # 不可重试的内联错误
                try:
                    eobj = json.loads(inline_err)
                    err_payload = eobj.get("error", {"message": inline_err})
                except (ValueError, TypeError):
                    err_payload = {"message": inline_err}
                yield f"data: {json.dumps({'error': err_payload, 'type': 'upstream_error'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 成功：重放已缓冲内容 + 剩余流（用同一个迭代器）
            async def _combined():
                for c in buffered:
                    yield c
                async for c in text_iter:
                    yield c

            logger.info("[调度] 账号=%s 请求成功", acc.nickname)
            usage_box = {}
            try:
                async for piece in _normalize_text_stream(_combined(), usage_box, deadline):
                    sent_any = True
                    yield piece
            finally:
                await resp.aclose()
                if usage_box.get("usage"):
                    u = usage_box["usage"]
                    consumed = _extract_consumed(u)
                    if consumed > 0:
                        token_rotator.deduct_quota(acc.id, consumed)
                    update_account_stats(platform, acc.id, u)
                    _safe_log("success", platform, acc.id, acc.nickname, model, f"消耗 {consumed}" if consumed > 0 else "请求成功", "")
            return
        except httpx.TimeoutException as e:
            await resp.aclose()
            last_msg = f"上游超时: {e}"
            token_rotator.mark_disabled(acc.id, "transient")
            # 已经吐给客户端的内容收不回来了。此时再换号重发会让流式客户端收到
            # 「半个回答 + 一个完整回答」，非流式更糟：两次正文会被拼进同一条
            # message。所以只能就地收尾，不能 continue。
            if sent_any:
                logger.warning("[调度] 账号=%s 流中途超时，已发出内容，不再重试", acc.nickname)
                _safe_log("error", platform, acc.id, acc.nickname, model, "流中途超时，已发出部分内容", "")
                yield ("data: " + json.dumps(
                    {"error": {"message": last_msg, "type": "upstream_timeout"}},
                    ensure_ascii=False) + "\n\n")
                yield "data: [DONE]\n\n"
                return
            continue
        except Exception:
            await resp.aclose()
            raise

    # 全部账号耗尽
    logger.warning("[调度] 所有账号不可用, last_msg=%s", last_msg)
    _safe_log("error", platform, "", "", model, f"所有账号不可用: {last_msg}", "")
    yield f"data: {json.dumps({'error': {'message': last_msg, 'type': 'no_account'}}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_with_failover(
    payload: dict,
    conversation_id: Optional[str],
    platform: str,
) -> AsyncGenerator[str, None]:
    """带 active 状态追踪的流式生成器包装。"""
    token_rotator.set_active(True)
    try:
        async for chunk in _stream_inner(payload, conversation_id, platform):
            yield chunk
    finally:
        token_rotator.set_active(False)


async def _non_stream_chat(
    payload: dict,
    conversation_id: Optional[str],
    platform: str,
) -> dict:
    content_parts = []
    tool_call_map = {}
    tool_call_order = []
    current_tool_index = None
    finish_reason = "stop"
    resp_id = None
    resp_model = None
    usage_info = None

    gen = _stream_with_failover(payload, conversation_id, platform)
    try:
        async for raw in gen:
            obj = parse_sse_line(raw)
            if not obj:
                continue
            if isinstance(obj.get("error"), dict):
                return {"error": obj["error"]}

            resp_id = resp_id or obj.get("id")
            resp_model = resp_model or obj.get("model")
            if obj.get("usage"):
                usage_info = obj["usage"]

            choices = obj.get("choices", [])
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta", {})

            if delta.get("content"):
                content_parts.append(delta["content"])

            tool_calls = delta.get("tool_calls", [])
            for tc in tool_calls:
                tool_index = tc.get("index")
                tid = tc.get("id")
                if tool_index is None:
                    tool_index = current_tool_index
                if tool_index is None:
                    tool_index = len(tool_call_map)

                if tool_index not in tool_call_map:
                    tool_call_map[tool_index] = {
                        "id": tid or "",
                        "type": tc.get("type", "function"),
                        "function": {"name": "", "arguments": ""},
                    }
                    tool_call_order.append(tool_index)

                current_tool_index = tool_index
                current = tool_call_map[tool_index]
                if tid:
                    current["id"] = tid
                if tc.get("type"):
                    current["type"] = tc["type"]
                func = tc.get("function", {})
                if func.get("name"):
                    current["function"]["name"] = func["name"]
                if func.get("arguments"):
                    current["function"]["arguments"] += func["arguments"]

        content = "".join(content_parts)
        final_tool_calls = [tool_call_map[index] for index in tool_call_order] if tool_call_order else None
        final_finish = "tool_calls" if final_tool_calls else (finish_reason or "stop")

        message = {"role": "assistant", "content": content if content else (None if final_tool_calls else "")}
        if final_tool_calls:
            message["tool_calls"] = final_tool_calls

        result = {
            "id": resp_id or str(uuid.uuid4()),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp_model or payload.get("model", "auto"),
            "choices": [{"index": 0, "message": message, "finish_reason": final_finish}],
        }
        if usage_info:
            result["usage"] = usage_info
        return result
    finally:
        await gen.aclose()


router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    _auth=Depends(authenticate),
    x_conversation_id: Optional[str] = Header(None, alias="X-Conversation-ID"),
):
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    messages = body.get("messages", [])
    if not messages or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages required")
    if len(messages) == 0:
        raise HTTPException(status_code=400, detail="at least one message required")

    payload = build_chat_payload(body)

    try:
        if body.get("stream", False):
            return StreamingResponse(
                _stream_with_failover(payload, x_conversation_id, _platform),
                media_type="text/event-stream",
            )

        result = await _non_stream_chat(payload, x_conversation_id, _platform)
        if isinstance(result, dict) and result.get("error"):
            raise HTTPException(
                status_code=503,
                detail=result["error"].get("message", "upstream error"),
            )
        return JSONResponse(content=result)
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Cannot connect to copilot.tencent.com")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream API timed out")


@router.get("/v1/models")
async def list_models(_auth=Depends(authenticate)):
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": int(time.time()), "owned_by": "copilot.tencent.com"}
            for m in AVAILABLE_MODELS
        ],
    }


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/proxy/info")
async def proxy_info():
    st = token_rotator.status()
    return {
        "platform": _platform,
        "proxy_port": _port,
        "upstream": resolve_base_url(),
        "accounts_total": st["total"],
        "accounts_usable": st["usable"],
        "accounts_disabled": st["disabled"],
        "current_account": st["current"],
        "active": st["active"],
        "threshold_switch": st.get("threshold_switch"),
        "models": AVAILABLE_MODELS,
    }


app = FastAPI(title="cbcn2api Proxy", version="0.3.0", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.on_event("startup")
async def preload():
    token_rotator.reload(_platform)


@app.on_event("shutdown")
async def cleanup():
    await _close_http_client()
