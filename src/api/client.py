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
