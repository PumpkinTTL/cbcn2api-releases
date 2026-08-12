"""Grok HTTP API 子路由（/api/grok/*）。

⚠️ 前端 iframe（grok.html）已不走 HTTP —— 改经 postMessage RPC 桥 → 主 frame
pywebview.api → src/grok/service.py（与 CodeBuddy 同款调用链，不依赖网关是否启动）。
本路由保留给无 GUI 场景（脚本/调试/未来客户端），响应结构与 service 层一致。

鉴权：复用网关密码（x-grok-token header）。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from . import config, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/grok", tags=["grok"])

# 网关密码（由 proxy_server 注入；HTTP 客户端带在 x-grok-token header）
_pwd: str = ""


def configure(password: str):
    global _pwd
    _pwd = password or ""


async def _require_auth(x_grok_token: Optional[str] = Header(None, alias="x-grok-token")):
    """密码校验依赖。dev 模式（无密码）放行。"""
    if _pwd and x_grok_token != _pwd:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/models")
async def list_models():
    """模型列表（无需鉴权，供客户端枚举）。"""
    return {"models": config.MODELS}


@router.get("/accounts")
async def list_accounts(_auth=Depends(_require_auth)):
    return {"accounts": service.list_accounts()}


@router.post("/oauth/start")
async def oauth_start(_auth=Depends(_require_auth)):
    try:
        return service.oauth_start()
    except Exception as e:
        logger.exception("[grok] oauth start failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/oauth/poll")
async def oauth_poll(login_id: str = Query(...), _auth=Depends(_require_auth)):
    try:
        result = service.oauth_poll(login_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        return {"status": "pending"}
    return {"status": "ok", "credentials": result}


@router.post("/oauth/cancel")
async def oauth_cancel(login_id: str = Query(...), _auth=Depends(_require_auth)):
    service.oauth_cancel(login_id)
    return {"ok": True}


@router.post("/oauth/complete")
async def oauth_complete(body: dict, _auth=Depends(_require_auth)):
    """保存登录成功的账号（credentials 来自 /oauth/poll 的返回）。"""
    try:
        return {"ok": True, "account": service.complete_login(body.get("credentials") or {})}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/refresh")
async def refresh_account(body: dict, _auth=Depends(_require_auth)):
    """手动刷新某账号 token。"""
    try:
        return {"ok": True, **service.refresh(body.get("account_id"))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"刷新失败: {e}")


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, _auth=Depends(_require_auth)):
    service.delete(account_id)
    return {"ok": True}
