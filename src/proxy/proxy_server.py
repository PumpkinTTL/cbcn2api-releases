import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional, AsyncGenerator

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


async def _stream_from_copilot(
    payload: dict,
    bearer_token: str,
    user_id: Optional[str],
    conversation_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    base_url = resolve_base_url()
    api_url = f"{base_url}/v2/chat/completions"
    headers = build_headers(bearer_token, user_id, conversation_id)
    client = _get_http_client()

    async with client.stream("POST", api_url, json=payload, headers=headers) as resp:
        if resp.status_code != 200:
            error_text = await resp.aread()
            yield f"data: {json.dumps({'error': {'message': error_text.decode('utf-8', errors='ignore'), 'type': 'api_error'}})}\n\n"
            return

        buffer = ""
        saw_tool_calls = False
        async for chunk in resp.aiter_text(chunk_size=8192):
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
                if stripped.startswith("data: "):
                    try:
                        obj = json.loads(stripped[6:])
                        for choice in obj.get("choices", []):
                            delta = choice.get("delta", {})
                            # Strip empty tool_calls[] — CodeBuddy sends this on every chunk,
                            # breaks AI SDK reasoning tracking (premature reasoning-end)
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


async def _non_stream_chat(
    payload: dict,
    bearer_token: str,
    user_id: Optional[str],
    conversation_id: Optional[str] = None,
) -> dict:
    content_parts = []
    tool_call_map = {}
    tool_call_order = []
    current_tool_index = None
    finish_reason = "stop"
    resp_id = None
    resp_model = None
    usage_info = None

    async for raw in _stream_from_copilot(payload, bearer_token, user_id, conversation_id):
        obj = parse_sse_line(raw)
        if not obj:
            continue

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

    acc = None
    for p in (_platform, "codebuddy_cn"):
        acc = token_rotator.get_next(p)
        if acc:
            break
    if not acc:
        raise HTTPException(status_code=503, detail="No valid credentials available")

    payload = build_chat_payload(body)

    try:
        if body.get("stream", False):
            async def event_stream():
                async for chunk in _stream_from_copilot(
                    payload, acc.access_token, acc.uid, x_conversation_id
                ):
                    yield chunk
            return StreamingResponse(event_stream(), media_type="text/event-stream")

        result = await _non_stream_chat(payload, acc.access_token, acc.uid, x_conversation_id)
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
    return {
        "platform": _platform,
        "proxy_port": _port,
        "upstream": resolve_base_url(),
        "accounts_loaded": token_rotator.count(),
        "models": AVAILABLE_MODELS,
    }


app = FastAPI(title="cbcn2api Proxy", version="0.2.0", docs_url=None, redoc_url=None)
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
