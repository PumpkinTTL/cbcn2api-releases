import json
import os
import sys
import tempfile
from typing import Optional

import requests

APP_VERSION = "v1.0.8"
REPO = "PumpkinTTL/cbcn2api-releases"
GITHUB_API = f"https://api.github.com/repos/{REPO}/releases/latest"


def _parse_version(tag: str) -> tuple:
    parts = tag.lstrip("vV").replace("-", ".").split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return (0, 0, 0)


def _proxy():
    p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    return {"http": p, "https": p} if p else {}


def check_latest() -> dict:
    try:
        resp = requests.get(
            GITHUB_API,
            headers={
                "User-Agent": f"AI-Gateway/{APP_VERSION}",
                "Accept": "application/vnd.github+json",
            },
            proxies=_proxy(),
            timeout=10,
        )
        if resp.status_code != 200:
            return {"error": f"GitHub API 返回 {resp.status_code}"}
        data = resp.json()
        latest_tag = data.get("tag_name", "")
        if not latest_tag:
            return {"error": "无法获取版本信息"}
        latest_ver = _parse_version(latest_tag)
        current_ver = _parse_version(APP_VERSION)
        has_update = latest_ver > current_ver
        assets = data.get("assets", [])
        download_url = ""
        for a in assets:
            name = a.get("name", "")
            if name.endswith(".exe"):
                download_url = a.get("browser_download_url", "")
                break
        return {
            "has_update": has_update,
            "latest_version": latest_tag,
            "current_version": APP_VERSION,
            "download_url": download_url,
            "release_url": data.get("html_url", ""),
            "release_name": data.get("name", ""),
            "release_body": (data.get("body", "") or "")[:500],
        }
    except requests.RequestException as e:
        return {"error": f"检查更新失败: {e}"}


def download_update(download_url: str, progress_callback=None) -> dict:
    try:
        resp = requests.get(download_url, stream=True, timeout=30, proxies=_proxy())
        if resp.status_code != 200:
            return {"error": f"下载失败: HTTP {resp.status_code}"}
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        fd, tmp_path = tempfile.mkstemp(suffix=".exe", prefix="ai-gateway-update-")
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
    vbs_path = os.path.join(tempfile.gettempdir(), "ai-gateway-update.vbs")
    log_path = os.path.join(tempfile.gettempdir(), "ai-gateway-update.err")
    pid = os.getpid()
    src = download_path.replace("\\", "\\\\")
    dst = current_exe.replace("\\", "\\\\")
    log = log_path.replace("\\", "\\\\")
    vbs_content = (
        "Set WshShell = CreateObject(\"WScript.Shell\")\n"
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")\n"
        "pid = \"" + str(pid) + "\"\n"
        "Do\n"
        "  WScript.Sleep 1000\n"
        "  On Error Resume Next\n"
        "  Set proc = GetObject(\"winmgmts:root\\cimv2:Win32_Process.Handle='\" & pid & \"'\")\n"
        "  If Err.Number <> 0 Then Exit Do\n"
        "  Set proc = Nothing\n"
        "  On Error Goto 0\n"
        "Loop\n"
        "On Error Resume Next\n"
        "fso.CopyFile \"" + src + "\", \"" + dst + "\", True\n"
        "If Err.Number <> 0 Then\n"
        "  Set f = fso.CreateTextFile(\"" + log + "\", True)\n"
        "  f.WriteLine \"copy failed: \" & Err.Description\n"
        "  f.Close\n"
        "End If\n"
        "fso.DeleteFile \"" + src + "\", True\n"
        "WshShell.Run \"\"\"\" & \"" + dst + "\" & \"\"\"\", 0, False\n"
    )
    try:
        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_content)
        os.startfile(vbs_path)
        return {"ok": True}
    except Exception as e:
        return {"error": f"启动更新程序失败: {e}"}
