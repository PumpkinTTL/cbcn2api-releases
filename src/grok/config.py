"""Grok CLI / Grok Build 配置常量。

数据源：官方 @xai-official/grok 0.2.99 与 cli-chat-proxy.grok.com 的 wire capture。
所有伪装指纹（UA / client-identifier / version / 自定义认证头）逐字节对齐官方 CLI，
否则 cli-chat-proxy 会按非官方客户端拒绝或降级。

链路区别（三条独立，不要混用）：
  - xai       → api.x.ai          （API key / xAI OAuth PKCE，按量计费）
  - grok-web  → grok.com          （Web SSO cookie）
  - grok-cli  → cli-chat-proxy.grok.com （本模块，Grok Build 订阅 credits）
"""
import os

# 上游基址（Responses API）。环境变量 GROK_BASE_URL 可覆盖（本地 mock 上游测试用）。
BASE_URL = os.environ.get("GROK_BASE_URL") or "https://cli-chat-proxy.grok.com/v1"

# 伪装的官方 CLI 版本（抓包对齐 0.2.99）
CLIENT_VERSION = "0.2.99"
CLIENT_IDENTIFIER = "grok-shell"
USER_AGENT = f"grok-shell/{CLIENT_VERSION} (linux; x86_64)"

# OAuth（device code 流程，直连 auth.x.ai）
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
# 官方 CLI 在 device code 请求里带 referrer=grok-build，标识 Grok Build 订阅来源
REFERRER = "grok-build"
# scope 比 api-only 的 xai 多了 conversations 读写（HAR 抓包）
SCOPE = (
    "openid profile email offline_access "
    "grok-cli:access api:access conversations:read conversations:write"
)

# 自定义认证头名（cli-chat-proxy 不走标准 Authorization 校验，而是看这个头）
TOKEN_AUTH_HEADER_VALUE = "xai-grok-cli"

# 主动刷新提前量（token 约 40-45min 过期，提前 5min 刷新，避免静默失效）
REFRESH_LEAD_SECONDS = 5 * 60

# 设备码轮询参数
DEVICE_POLL_INTERVAL = 2  # 秒
DEVICE_TIMEOUT = 600      # 秒

# 模型列表（grok-build 是订阅默认模型；grok-4.5 系支持 reasoning.effort）
GROK_BUILD_MODEL = "grok-build"
MODELS = [
    {"id": GROK_BUILD_MODEL, "name": "Grok Build", "context": 500000, "max_output": 64000},
    {"id": "grok-4.5", "name": "Grok 4.5"},
    {"id": "grok-4.5-high", "name": "Grok 4.5 (High)", "upstream": "grok-4.5"},
    {"id": "grok-4.5-medium", "name": "Grok 4.5 (Medium)", "upstream": "grok-4.5"},
    {"id": "grok-4.5-low", "name": "Grok 4.5 (Low)", "upstream": "grok-4.5"},
]

# 仅 grok-4.5 系接受 reasoning.effort；grok-build / Composer 会拒绝
def supports_reasoning_effort(model: str) -> bool:
    return model.startswith("grok-4.5") if model else False

# Responses API 字段白名单（cli-chat-proxy 只接受这些，其余剔除）
RESPONSES_ALLOWLIST = {
    "model", "input", "instructions", "tools", "tool_choice",
    "stream", "store", "reasoning", "include",
    "temperature", "top_p", "max_output_tokens",
    "parallel_tool_calls", "text", "metadata", "prompt_cache_key",
}

PLATFORM_KEY = "grok"
