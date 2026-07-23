import logging
import secrets
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

CHAT_API_BASE = "https://copilot.tencent.com"

DEFAULT_UPSTREAM_CLIENT = "workbuddy"

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
) -> dict:
    client_profile = UPSTREAM_CLIENT_PROFILES[DEFAULT_UPSTREAM_CLIENT]
    return {
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
        "User-Agent": client_profile["User-Agent"],
    }


def build_chat_payload(openai_body: dict) -> dict:
    payload = dict(openai_body)
    requested_model = str(payload.get("model", DEFAULT_MODEL)).lower()
    payload["model"] = MODEL_ALIASES.get(requested_model, requested_model)
    payload["messages"] = convert_messages(payload.get("messages", []))
    payload["stream"] = True

    effort = payload.get("reasoning_effort")
    if effort in ("none", "off"):
        payload.pop("reasoning_effort", None)
        payload.pop("reasoning_summary", None)
    elif effort:
        payload["reasoning_summary"] = "auto"

    return payload
