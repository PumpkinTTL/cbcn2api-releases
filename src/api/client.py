import requests

BASE_URL = "https://www.codebuddy.cn"

PLATFORM_CONFIG = {
    "codebuddy_cn": {
        "platform": "ide",
        "login_prefix": "cb_",
    },
    "workbuddy": {
        "platform": "workbuddy",
        "login_prefix": "wb_",
    },
}


def build_headers(access_token: str, uid: str = None,
                  enterprise_id: str = None, domain: str = None) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        # 必须带 UA：上游 get-user-resource 等额度接口校验 User-Agent，
        # 缺失直接返回 10085「请求不合法」→ 被旧代码误判为封号（一刷就封一个）。
        # 用官方 WorkBuddy 客户端 UA，与网关转发链（src/proxy/api_client.py）保持一致。
        "User-Agent": "WorkBuddy/5.2.6 WorkBuddy/5.2.6 CLI/2.106.4",
    }
    if uid:
        headers["X-User-Id"] = uid
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    if domain:
        headers["X-Domain"] = domain
    return headers


def get_session() -> requests.Session:
    return requests.Session()
