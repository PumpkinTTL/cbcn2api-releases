import json
import os
import sys
import tempfile
from typing import Optional

import requests

APP_VERSION = "v1.0.7"
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
    check_path = os.path.join(tempfile.gettempdir(), "ai-gateway-check.txt")
    # VBS 字符串没有反斜杠转义，路径直接原样写入（不要 replace 成 \\）。
    src = download_path
    dst = current_exe
    log = log_path
    chk = check_path
    vbs_content = (
        "Set WshShell = CreateObject(\"WScript.Shell\")\n"
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")\n"
        "On Error Resume Next\n"
        "' 不用 WMI 等进程退出（WMI 查询本身可能失败 → 误判退出 → 覆盖运行中的 exe）。\n"
        "' 直接重试 CopyFile：进程退出前 exe 被锁定 → 失败重试；退出后锁释放 → 成功。\n"
        "copy_ok = False\n"
        "For i = 1 To 60\n"
        "  fso.CopyFile \"" + src + "\", \"" + dst + "\", True\n"
        "  If Err.Number = 0 Then\n"
        "    copy_ok = True\n"
        "    Exit For\n"
        "  End If\n"
        "  Err.Clear\n"
        "  WScript.Sleep 1000\n"
        "Next\n"
        "If Not copy_ok Then\n"
        "  Set f = fso.CreateTextFile(\"" + log + "\", True)\n"
        "  f.WriteLine \"copy failed: \" & Err.Description\n"
        "  f.Close\n"
        "  WScript.Quit 1\n"
        "End If\n"
        "fso.DeleteFile \"" + src + "\", True\n"
        "' 等 5 秒再启动：让杀毒软件完成对刚写入 exe 的扫描，避免启动时解压加载失败。\n"
        "WScript.Sleep 5000\n"
        "exe_name = fso.GetFileName(\"" + dst + "\")\n"
        "chk = \"" + chk + "\"\n"
        "started = False\n"
        "For i = 1 To 3\n"
        "  WshShell.Run \"\"\"\" & \"" + dst + "\" & \"\"\"\", 0, False\n"
        "  WScript.Sleep 8000\n"
        "  WshShell.Run \"cmd /c tasklist > \" & chk, 0, True\n"
        "  Set f = Nothing\n"
        "  Set f = fso.OpenTextFile(chk, 1, False, 0)\n"
        "  txt = \"\"\n"
        "  If Not f Is Nothing Then\n"
        "    txt = f.ReadAll\n"
        "    f.Close\n"
        "  End If\n"
        "  Err.Clear\n"
        "  If InStr(txt, exe_name) > 0 Then\n"
        "    started = True\n"
        "    Exit For\n"
        "  End If\n"
        "Next\n"
        "If Not started Then\n"
        "  Set f = fso.CreateTextFile(\"" + log + "\", True)\n"
        "  f.WriteLine \"launch failed after 3 tries\"\n"
        "  f.Close\n"
        "  WScript.Quit 1\n"
        "End If\n"
    )
    try:
        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_content)
        os.startfile(vbs_path)
        return {"ok": True}
    except Exception as e:
        return {"error": f"启动更新程序失败: {e}"}
