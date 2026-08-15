import json
import os
import sys
import tempfile
from typing import Optional

import requests

APP_VERSION = "v1.1.2"
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
        # 从下载 URL 解析目标版本 tag（.../releases/download/{tag}/asset.exe）
        tag = "latest"
        try:
            tag = download_url.split("/releases/download/")[1].split("/")[0]
        except Exception:
            pass
        # 下载到当前 exe 同级目录，独立文件名（不覆盖运行中的旧 exe）
        if getattr(sys, "frozen", False):
            target_dir = os.path.dirname(sys.executable)
        else:
            target_dir = tempfile.gettempdir()
        final_path = os.path.join(target_dir, f"AI Gateway {tag}.exe")
        tmp_path = final_path + ".part"
        # 断点续传：.part 已存在的字节数作为 Range 起点
        offset = 0
        try:
            offset = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
        except OSError:
            offset = 0
        headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        resp = requests.get(download_url, stream=True, timeout=30, proxies=_proxy(), headers=headers)
        # 206=续传命中（从 offset 续写）；200=全新下载（服务器不支持 Range 或文件已变，回退重头）
        if resp.status_code == 206:
            cr = resp.headers.get("Content-Range", "")
            try:
                total = int(cr.split("/")[-1])
            except (ValueError, IndexError):
                total = 0
            mode = "ab"
            downloaded = offset
        elif resp.status_code == 200:
            total = int(resp.headers.get("content-length", 0))
            mode = "wb"
            offset = 0
            downloaded = 0
        else:
            return {"error": f"下载失败: HTTP {resp.status_code}"}
        # 续传时立即反映已下载进度，避免 UI 从 0% 跳起
        if progress_callback and total > 0 and offset > 0:
            progress_callback(int(downloaded * 100 / total))
        chunk_size = 65536
        with open(tmp_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        progress_callback(int(downloaded * 100 / total))
        # 完整性校验：累计字节数必须等于 total（不完整保留 .part，下次可继续续传）
        if total > 0 and downloaded != total:
            return {"error": "下载不完整"}
        # 下载完成：替换为目标文件名（覆盖同名旧下载）
        os.replace(tmp_path, final_path)
        return {"ok": True, "path": final_path, "size": downloaded, "tag": tag}
    except Exception as e:
        # 异常（网络中断等）保留 .part，下次调用可从断点继续
        return {"error": f"下载失败: {e}"}


def _get_current_exe() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return ""


def apply_update(download_path: str) -> dict:
    """全量更新：新 exe 是同级目录的独立文件（已下载完成）。

    VBS 流程：等旧进程退出（重试删除旧 exe，锁释放即删除成功）→
    启动新 exe（独立文件，无覆盖锁、杀软扫描早已完成）→
    通过启动标记文件确认 GUI 真正起来（最多重试 3 次）。
    """
    current_exe = _get_current_exe()
    if not current_exe:
        return {"error": "仅在打包后可执行更新"}
    if not os.path.exists(download_path):
        return {"error": f"新版本文件不存在: {download_path}"}
    vbs_path = os.path.join(tempfile.gettempdir(), "ai-gateway-update.vbs")
    log_path = os.path.join(tempfile.gettempdir(), "ai-gateway-update.err")
    check_path = os.path.join(tempfile.gettempdir(), "ai-gateway-check.txt")
    # VBS 字符串没有反斜杠转义，路径直接原样写入（不要 replace 成 \\）。
    new_exe = download_path
    old_exe = current_exe
    log = log_path
    chk = check_path
    vbs_content = (
        "Set WshShell = CreateObject(\"WScript.Shell\")\n"
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")\n"
        "On Error Resume Next\n"
        "' 等旧进程退出：重试删除旧 exe（进程退出前被锁定 → 失败重试；退出后删除成功）。\n"
        "For i = 1 To 60\n"
        "  If Not fso.FileExists(\"" + old_exe + "\") Then Exit For\n"
        "  fso.DeleteFile \"" + old_exe + "\", True\n"
        "  If Err.Number = 0 Then Exit For\n"
        "  Err.Clear\n"
        "  WScript.Sleep 1000\n"
        "Next\n"
        "Err.Clear\n"
        "' 启动新版本（独立文件，下载完成已过杀软扫描，无需再等待）。\n"
        "started = False\n"
        "For i = 1 To 3\n"
        "  fso.DeleteFile \"" + chk + "\", True\n"
        "  Err.Clear\n"
        "  WshShell.Run \"\"\"\" & \"" + new_exe + "\" & \"\"\"\", 0, False\n"
        "  WScript.Sleep 12000\n"
        "  If fso.FileExists(\"" + chk + "\") Then\n"
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
        # 写更新标记：新版本首次启动时凭它清理同目录旧版本，
        # 平时手动启动不清理，避免误删构建产物目录里的其他版本。
        updated_mark = os.path.join(tempfile.gettempdir(), "ai-gateway-updated.txt")
        try:
            with open(updated_mark, "w", encoding="utf-8") as f:
                f.write("updated")
        except Exception:
            pass
        os.startfile(vbs_path)
        return {"ok": True}
    except Exception as e:
        return {"error": f"启动更新程序失败: {e}"}


def cleanup_old_versions():
    """启动后清理同目录的其他版本 exe（只删 AI Gateway 开头的，不动自己）。

    更新流程中旧 exe 可能因删除失败残留，由新实例启动后兜底清理。
    """
    if not getattr(sys, "frozen", False):
        return
    exe_dir = os.path.dirname(sys.executable)
    me = os.path.basename(sys.executable)
    try:
        for f in os.listdir(exe_dir):
            if f.startswith("AI Gateway ") and f.endswith(".exe") and f != me:
                try:
                    os.remove(os.path.join(exe_dir, f))
                except OSError:
                    pass
    except OSError:
        pass
