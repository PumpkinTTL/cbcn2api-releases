"""Grok Build 子系统（独立包）。

与 CodeBuddy/WorkBuddy 代码零 import 互依，仅共享 store 表（platform='grok'）。

对外暴露：
  - router：FastAPI 子路由（/api/grok/*），供 grok.html fetch
  - handle_request：Responses 透传主流程，供 proxy_server 的 /v1/responses 调用
  - configure：注入网关密码
"""
from .routes import router, configure
from .provider import handle_request, grok_pool

__all__ = ["router", "configure", "handle_request", "grok_pool"]
