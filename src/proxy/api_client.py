import logging
import random
import secrets
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

CHAT_API_BASE = "https://copilot.tencent.com"

DEFAULT_UPSTREAM_CLIENT = "workbuddy"

# 客户端指纹随机池：只随机版本号类字段，平台/架构/语言等固定（见 build_headers），
# 保证生成的指纹都是合法存在的组合，不会出现乱写的平台或版本。
_WORKBUDDY_IDE_VERSIONS = ["5.2.6", "5.2.5", "5.2.4", "5.3.0", "5.3.1", "5.1.9", "5.1.8", "5.2.7"]
_WORKBUDDY_CLI_VERSIONS = ["2.106.4", "2.106.5", "2.107.0", "2.107.1", "2.108.1", "2.108.2", "2.109.0", "2.110.0"]
_STAINLESS_VERSIONS = ["6.25.0", "6.24.0", "6.23.1", "6.26.0", "6.25.1", "6.22.0"]
_NODE_RUNTIME_VERSIONS = ["v22.21.1", "v22.20.0", "v22.22.0", "v22.19.0", "v20.19.0", "v20.18.1", "v23.0.0", "v22.18.0"]

# build_headers 允许账号指纹覆盖的字段白名单（其余 header 一律不接受覆盖）。
# X-IDE-Type / X-IDE-Name 是上游识别客户端身份的核心头 —— 只改 UA 不生效，
# 必须与 UA 三件套一起换（社区指纹规范：CLI 三件套 = UA + Type + Name）。
FINGERPRINT_FIELDS = (
    "X-IDE-Version",
    "User-Agent",
    "x-stainless-package-version",
    "x-stainless-runtime-version",
    "X-IDE-Type",
    "X-IDE-Name",
)


def generate_fingerprint(style: str = "workbuddy") -> dict:
    """随机一套成套指纹。style: workbuddy（默认）| cli —— 三件套联动，不会撕裂。

    workbuddy 风格：Type/Name=WorkBuddy，UA=WorkBuddy/{ide} WorkBuddy/{ide} CLI/{cli}
    cli 风格：Type/Name=CLI，UA=CLI/{ver} CodeBuddy/{ver}（社区指纹规范形态）
    """
    stainless = (
        random.choice(_STAINLESS_VERSIONS),
        random.choice(_NODE_RUNTIME_VERSIONS),
    )
    if (style or "workbuddy").lower() == "cli":
        ver = random.choice(_WORKBUDDY_CLI_VERSIONS)
        return {
            "X-IDE-Type": "CLI",
            "X-IDE-Name": "CLI",
            "X-IDE-Version": ver,
            "User-Agent": f"CLI/{ver} CodeBuddy/{ver}",
            "x-stainless-package-version": stainless[0],
            "x-stainless-runtime-version": stainless[1],
        }
    ide = random.choice(_WORKBUDDY_IDE_VERSIONS)
    cli = random.choice(_WORKBUDDY_CLI_VERSIONS)
    return {
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": ide,
        "User-Agent": f"WorkBuddy/{ide} WorkBuddy/{ide} CLI/{cli}",
        "x-stainless-package-version": stainless[0],
        "x-stainless-runtime-version": stainless[1],
    }


UPSTREAM_CLIENT_PROFILES = {
    "workbuddy": {
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": "5.2.6",
        "User-Agent": "WorkBuddy/5.2.6 WorkBuddy/5.2.6 CLI/2.106.4",
    },
    "codebuddy_cli": {
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "X-IDE-Version": "2.108.1",
        "User-Agent": "CLI/2.108.1 CodeBuddy/2.108.1",
    },
}

AVAILABLE_MODELS = [
    "auto",
    "hy3",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "kimi-k3-1",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
]

DEFAULT_MODEL = "deepseek-v4-flash"

MODEL_ALIASES = {
    "kimi-k3": "kimi-k3-1",
    "kimi-k2.7-code": "kimi-k2.7",
}


def resolve_base_url() -> str:
    return CHAT_API_BASE


def convert_messages(openai_messages: list) -> list:
    return [dict(message) for message in openai_messages]


def build_headers(
    bearer_token: str,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    fingerprint: Optional[dict] = None,
    enterprise_id: Optional[str] = None,
    domain: Optional[str] = None,
) -> dict:
    client_profile = UPSTREAM_CLIENT_PROFILES[DEFAULT_UPSTREAM_CLIENT]
    # Origin/Referer 按账号区域匹配（社区指纹规范）：国内 codebuddy.cn，国际 workbuddy.ai。
    # 无 domain 时按国内处理（本项目账号体系为 copilot.tencent.com）。
    is_intl = bool(domain) and "workbuddy.ai" in domain
    origin = "https://www.workbuddy.ai" if is_intl else "https://www.codebuddy.cn"
    headers = {
        "Host": "copilot.tencent.com",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Windows",
        "x-stainless-package-version": "6.25.0",
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v22.21.1",
        "X-Conversation-ID": conversation_id or str(uuid.uuid4()),
        "X-Conversation-Request-ID": secrets.token_hex(16),
        "X-Conversation-Message-ID": uuid.uuid4().hex,
        "X-Request-ID": uuid.uuid4().hex,
        "X-Agent-Intent": "craft",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Type": client_profile["X-IDE-Type"],
        "X-IDE-Name": client_profile["X-IDE-Name"],
        "X-IDE-Version": client_profile["X-IDE-Version"],
        "X-Private-Data": "false",
        "x-codebuddy-request": "1",
        "Authorization": f"Bearer {bearer_token}",
        "X-User-Id": user_id or "00000000-0000-0000-0000-000000000000",
        "X-Product": "SaaS",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": client_profile["User-Agent"],
    }
    # 账号头带全（社区指纹规范）：有值发实名头，缺省按官方 CLI 的 X-No-* 约定
    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
    else:
        headers["X-No-Enterprise-Id"] = "1"
    if domain:
        headers["X-Domain"] = domain
    else:
        headers["X-No-Department-Info"] = "1"
    if fingerprint:
        for key in FINGERPRINT_FIELDS:
            value = fingerprint.get(key)
            if value:
                headers[key] = str(value)
    return headers


def build_chat_payload(openai_body: dict) -> dict:
    payload = dict(openai_body)
    requested_model = str(payload.get("model", DEFAULT_MODEL)).lower()
    payload["model"] = MODEL_ALIASES.get(requested_model, requested_model)
    payload["messages"] = convert_messages(payload.get("messages", []))
    payload["stream"] = True

    # 对照 9router CodeBuddyExecutor.transformRequest
    effort = payload.get("reasoning_effort")
    if effort in ("none", "off"):
        payload.pop("reasoning_effort", None)
        payload.pop("reasoning_summary", None)
    elif effort:
        payload["reasoning_summary"] = "auto"

    return payload
