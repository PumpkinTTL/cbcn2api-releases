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


def build_headers(access_token: str = None, uid: str = None,
                  enterprise_id: str = None, domain: str = None) -> dict:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        # 客户端身份头（与网关转发链 src/proxy/api_client.py 的 workbuddy profile 一致）：
        # 上游 get-user-resource 等额度接口校验 UA，缺失返回 10085「请求不合法」，
        # 且对"无 UA/身份头异常"的请求有封号风控风险 —— 所有上游请求必须成套携带。
        "User-Agent": "WorkBuddy/5.2.6 WorkBuddy/5.2.6 CLI/2.106.4",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": "5.2.6",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
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
