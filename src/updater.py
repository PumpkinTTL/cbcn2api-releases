import json
import os
import subprocess
import sys
import tempfile
from typing import Optional

import requests

APP_VERSION = "v1.0.5"
REPO = "PumpkinTTL/cbcn2api-releases"
GITHUB_API = f"https://api.github.com/repos/{REPO}/releases/latest"


def _parse_version(tag: str) -> tuple:
    parts = tag.lstrip("vV").replace("-", ".").split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return (0, 0, 0)


def _find_gh() -> Optional[str]:
    for candidate in [
        "gh",
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Program Files (x86)\GitHub CLI\gh.exe",
    ]:
        try:
            subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
            return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _fetch_via_gh() -> Optional[dict]:
    gh = _find_gh()
    if not gh:
        return None
    try:
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        env = os.environ.copy()
        if proxy:
            env["HTTPS_PROXY"] = proxy
        result = subprocess.run(
            [gh, "release", "view", "--repo", REPO,
             "--json", "tagName,name,body,assets"],
            capture_output=True, text=True, timeout=15, env=env,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return None
        data = json.loads(result.stdout)
        assets = data.get("assets", [])
        download_url = ""
        for a in assets:
            name = a.get("name", "")
            if name.endswith(".exe"):
                download_url = a.get("url", "")
                break
        return {
            "tag_name": data["tagName"],
            "html_url": f"https://github.com/{REPO}/releases/tag/{data['tagName']}",
            "name": data.get("name", ""),
            "body": data.get("body", ""),
            "download_url": download_url,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _fetch_via_api() -> Optional[dict]:
    try:
        resp = requests.get(
            GITHUB_API,
            headers={"User-Agent": f"AI-Gateway/{APP_VERSION}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        assets = data.get("assets", [])
        download_url = ""
        for a in assets:
            name = a.get("name", "")
            if name.endswith(".exe"):
                download_url = a.get("browser_download_url", "")
                break
        return {
            "tag_name": data.get("tag_name", ""),
            "html_url": data.get("html_url", ""),
            "name": data.get("name", ""),
            "body": data.get("body", ""),
            "download_url": download_url,
        }
    except requests.RequestException:
        return None


def check_latest() -> dict:
    data = _fetch_via_gh() or _fetch_via_api()
    if not data:
        return {"error": "检查更新失败: 无法连接 GitHub"}
    latest_tag = data.get("tag_name", "")
    if not latest_tag:
        return {"error": "无法获取版本信息"}
    latest_ver = _parse_version(latest_tag)
    current_ver = _parse_version(APP_VERSION)
    has_update = latest_ver > current_ver
    return {
        "has_update": has_update,
        "latest_version": latest_tag,
        "current_version": APP_VERSION,
        "download_url": data.get("download_url", ""),
        "release_url": data.get("html_url", ""),
        "release_name": data.get("name", ""),
        "release_body": (data.get("body", "") or "")[:500],
    }


def _ensure_proxy():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def download_update(download_url: str, progress_callback=None) -> dict:
    try:
        proxies = _ensure_proxy()
        resp = requests.get(download_url, stream=True, timeout=30, proxies=proxies)
        if resp.status_code != 200:
            return {"error": f"下载失败: HTTP {resp.status_code}"}
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        ext = ".exe"
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="ai-gateway-update-")
        os.close(fd)
        chunk_size = 65536
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        progress_callback(int(downloaded * 100 / total))
        if total > 0 and downloaded != total:
            os.unlink(tmp_path)
            return {"error": "下载不完整"}
        return {"ok": True, "path": tmp_path, "size": downloaded}
    except Exception as e:
        return {"error": f"下载失败: {e}"}


def _get_current_exe() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return ""


def apply_update(download_path: str) -> dict:
    current_exe = _get_current_exe()
    if not current_exe:
        return {"error": "仅在打包后可执行更新"}
    bat_path = os.path.join(tempfile.gettempdir(), "ai-gateway-update.bat")
    bat_content = f"""@echo off
chcp 65001 >nul
echo 正在更新 AI Gateway...
:wait
tasklist /fi "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
copy /y "{download_path}" "{current_exe}" >nul
if errorlevel 1 (
    echo 更新失败
    pause
    exit /b 1
)
del /q "{download_path}"
start "" "{current_exe}"
exit
"""
    try:
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_content)
        os.startfile(bat_path)
        return {"ok": True}
    except Exception as e:
        return {"error": f"启动更新程序失败: {e}"}
