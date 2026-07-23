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

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None
_proxy_password: str = os.environ.get("CBCN_PROXY_PASSWORD", "")
_platform: str = os.environ.get("CBCN_PROXY_PLATFORM", "workbuddy")
_port: int = int(os.environ.get("CBCN_PROXY_PORT", "8001"))

security = HTTPBearer(auto_error=False)

# 上游错误分类
QUOTA_ERROR_CODES = {14018}          # 额度耗尽
TRANSIENT_STATUS_CODES = {429, 502, 503, 504}  # 临时错误（401封禁/403鉴权单独处理）

PEEK_BYTE_LIMIT = 32768  # peek 阶段最多缓冲字节数


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            verify=False,
            trust_env=False,
            timeout=httpx.Timeout(300.0, connect=30.0, read=300.0),
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
    if status_code == 401:
        return "banned"
    if status_code == 403:
        return "auth"
    if status_code in TRANSIENT_STATUS_CODES:
        return "transient"
    return None


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


async def _normalize_text_stream(aiter: AsyncIterator[str]) -> AsyncGenerator[str, None]:
    """规整上游 SSE：剥离空 tool_calls[]、修正 finish_reason、补 [DONE]。"""
    buffer = ""
    saw_tool_calls = False
    async for chunk in aiter:
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
                    for choice in obj.get("choices", []):
                        delta = choice.get("delta", {})
                        tc = delta.get("tool_calls")
                        if isinstance(tc, list) and len(tc) == 0:
                            del delta["tool_calls"]
                        elif isinstance(tc, list) and tc:
                            saw_tool_calls = True
                        if saw_tool_calls and choice.get("finish_reason") not in ("tool_calls", None):
                            choice["finish_reason"] = "tool_calls"
                    yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
                    continue
                except (json.JSONDecodeError, IndexError):
                    pass
            yield stripped + "\n\n"
    if buffer.strip() and not buffer.strip().startswith(":"):
        yield buffer.strip() + "\n\n"
    yield "data: [DONE]\n\n"


async def _stream_with_failover(
    payload: dict,
    conversation_id: Optional[str],
    platform: str,
) -> AsyncGenerator[str, None]:
    """带故障转移的流式生成器：首个账号出错（额度/鉴权）时自动切下一个账号，
    在向客户端发送任何内容之前完成切换。"""
    fallback = "codebuddy_cn" if platform != "codebuddy_cn" else "workbuddy"
    max_attempts = max(token_rotator.count_usable(), 1)
    last_msg = "无可用账号"

    for _attempt in range(max_attempts):
        acc = token_rotator.get_next(platform) or token_rotator.get_next(fallback)
        if not acc:
            break

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
            last_msg = f"无法连接上游: {e}"
            break
        except httpx.TimeoutException as e:
            last_msg = f"上游超时: {e}"
            token_rotator.mark_disabled(acc.id, "transient")
            continue

        try:
            # 非 200：解析错误体，可重试则切号
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="ignore")
                await resp.aclose()
                kind = _classify_upstream_error(resp.status_code, body)
                if kind:
                    token_rotator.mark_disabled(acc.id, kind)
                    last_msg = body[:300]
                    continue
                yield f"data: {json.dumps({'error': {'message': body, 'type': 'upstream_error', 'status': resp.status_code}}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 200：peek 首个事件，检测内联错误，确认无误后再向客户端输出
            buffered = []
            decision = "none"
            inline_err = None
            async for chunk in resp.aiter_text(chunk_size=8192):
                buffered.append(chunk)
                kind, err = _first_event_kind(chunk)
                if kind == "error":
                    decision = "error"
                    inline_err = err
                    break
                if kind == "data":
                    decision = "data"
                    break
                if sum(len(c) for c in buffered) > PEEK_BYTE_LIMIT:
                    decision = "data"
                    break

            if decision == "error" and inline_err:
                await resp.aclose()
                kind = _classify_upstream_error(200, inline_err)
                if kind:
                    token_rotator.mark_disabled(acc.id, kind)
                    last_msg = inline_err[:300]
                    continue
                # 不可重试的内联错误
                try:
                    eobj = json.loads(inline_err)
                    err_payload = eobj.get("error", {"message": inline_err})
                except (ValueError, TypeError):
                    err_payload = {"message": inline_err}
                yield f"data: {json.dumps({'error': err_payload, 'type': 'upstream_error'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 成功：重放已缓冲内容 + 剩余流，并规整 SSE
            async def _combined():
                for c in buffered:
                    yield c
                async for c in resp.aiter_text(chunk_size=8192):
                    yield c

            async for piece in _normalize_text_stream(_combined()):
                yield piece
            return
        except httpx.TimeoutException as e:
            await resp.aclose()
            last_msg = f"上游超时: {e}"
            token_rotator.mark_disabled(acc.id, "transient")
            continue
        except Exception:
            await resp.aclose()
            raise

    # 全部账号耗尽
    yield f"data: {json.dumps({'error': {'message': last_msg, 'type': 'no_account'}}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


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

    async for raw in _stream_with_failover(payload, conversation_id, platform):
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
