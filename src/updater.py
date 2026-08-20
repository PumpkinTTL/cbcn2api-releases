import hashlib
import json
import os
import sys
import tempfile
from typing import Optional

import requests

APP_VERSION = "v1.1.3"
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


def _server_update_info() -> Optional[dict]:
    """从授权服务器 config 接口取更新分发信息（lic-admin 的 update 字段）。

    update 在 Ed25519 签名负载内，remote_config 验签通过才带出来 —— 这里拿到的
    下载地址/哈希整体可信。取不到（老服务器不下发 / 断网 / 字段损坏）返回 None，
    更新流程回退纯 GitHub 通道，与旧行为完全一致。

    延迟导入 license：license._app_version 反向引用本模块，模块级互导会循环。"""
    try:
        from src import license as lic
        cfg = lic.remote_config()
    except Exception:
        return None  # 授权服务器不可达不影响更新检查，GitHub 通道照常
    info = cfg.get("update") if isinstance(cfg, dict) else None
    return info if isinstance(info, dict) else None


def _check_github() -> dict:
    """GitHub Releases 检查更新（原有流程原样保留，一字不改）。"""
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


def check_latest() -> dict:
    """检查更新：GitHub Releases + 授权服务器 update 双通道合并。

    - 版本发现取两者较新（防单边手滑填错版本/未同步，谁新信谁）
    - 下载源按序回退：GitHub 资产地址在前（保持原有第一下载源行为），
      服务端 urls 依次补位
    - 只有「版本与选中版本一致」的地址才进下载列表：另一通道报的是旧版本时，
      它的下载地址对应的是旧二进制，下回来会把降级包当新版本装
    - sha256 仅在选中版本 == 服务端版本且服务端提供时启用（不同版本的文件
      内容不同，跨版本套用必然校验失败）
    - GitHub 挂了/查旧了，服务端信息保底 —— 版本被拦场景 GitHub 未必可用
    - manual_urls（网盘分享页）原样透传，前端在自动源全失败时展示兜底
    """
    gh = _check_github()
    srv = _server_update_info()
    gh_ok = "error" not in gh and bool(gh.get("latest_version"))
    if not srv:
        # 无服务端通道：纯 GitHub 原行为（补齐新字段保持返回结构统一）
        if not gh_ok:
            return gh
        out = dict(gh)
        out["download_urls"] = [gh["download_url"]] if gh.get("download_url") else []
        out["sha256"] = ""
        out["manual_urls"] = []
        return out

    # 双通道都有（或 GitHub 失败仅剩服务端）：比较版本取新
    gh_ver = _parse_version(gh.get("latest_version") or "") if gh_ok else (0, 0, 0)
    srv_ver = _parse_version(srv["latest_version"])
    # 版本相同（元组相等）时选服务端 tag（保 sha/notes/manual 兜底全挂上）
    srv_wins = srv_ver >= gh_ver
    chosen_ver = srv_ver if srv_wins else gh_ver
    chosen_tag = srv["latest_version"] if srv_wins else gh["latest_version"]

    # 下载源：同版本地址才有资格进列表（见 docstring 第三点），GitHub 在前
    sources = []
    if gh_ok and gh_ver == chosen_ver and gh.get("download_url"):
        sources.append(gh["download_url"])
    if srv_ver == chosen_ver:
        sources.extend(srv.get("urls") or [])
    # sha256 只认服务端、且只在服务端版本被选中时适用
    sha256 = (srv.get("sha256") or "") if srv_wins else ""
    # 更新说明：选中通道优先，另一通道兜底（服务端 notes > GitHub body > 无）
    if srv_wins and srv.get("notes"):
        body = srv["notes"][:500]
    elif gh_ok and gh.get("release_body"):
        body = gh["release_body"]
    else:
        body = (srv.get("notes") or "")[:500]

    return {
        "has_update": chosen_ver > _parse_version(APP_VERSION),
        "latest_version": chosen_tag,
        "current_version": APP_VERSION,
        "download_url": sources[0] if sources else "",
        "download_urls": sources,
        "sha256": sha256,
        "release_url": gh.get("release_url", "") if gh_ok else "",
        "release_name": gh.get("release_name", "") if gh_ok else "",
        "release_body": body,
        "manual_urls": srv.get("manual_urls") or [],
    }


def _download_paths(tag: str) -> tuple:
    """按目标版本 tag 推导下载落盘路径：(成品路径, .part 临时路径)。

    下载到当前 exe 同级目录，独立文件名（不覆盖运行中的旧 exe）。"""
    if getattr(sys, "frozen", False):
        target_dir = os.path.dirname(sys.executable)
    else:
        target_dir = tempfile.gettempdir()
    final_path = os.path.join(target_dir, f"AI Gateway {tag}.exe")
    return final_path, final_path + ".part"


def _download_one(download_url: str, progress_callback=None, tag: Optional[str] = None) -> dict:
    """单源下载（原 download_update 主体原样保留，仅 tag 改为可显式传入：
    服务端分发 URL（如 dl.xxx/AI-Gateway-v1.1.4.exe）没有 releases/download
    路径可解析，靠调用方把 check_latest 选出的版本带进来）。"""
    try:
        # 目标版本 tag：显式传入优先，否则从 GitHub 下载 URL 解析
        # （.../releases/download/{tag}/asset.exe）
        if not tag:
            tag = "latest"
            try:
                tag = download_url.split("/releases/download/")[1].split("/")[0]
            except Exception:
                pass
        final_path, tmp_path = _download_paths(tag)
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


def _verify_sha256(path: str, expected: str) -> bool:
    """流式计算文件 sha256 并与期望值比对（大文件不整读进内存）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest() == expected.strip().lower()


def download_update(download_url: str, progress_callback=None) -> dict:
    """单源下载（兼容入口：老调用方式不变）。多源回退走 download_update_multi。"""
    return _download_one(download_url, progress_callback=progress_callback)


def download_update_multi(urls: list, sha256: str = "", tag: Optional[str] = None,
                          progress_callback=None) -> dict:
    """多源下载：按 urls 顺序逐个尝试，单源失败（超时/HTTP 错/网络中断/sha256
    不匹配）自动回退下一个源；全部失败返回汇总错误（前端再展示网盘手动链接）。

    - sha256 非空时对每个源的成品做完整性校验，不匹配视为该源失败继续换源
      （镜像被篡改/缓存了旧文件都能挡住）；为空跳过校验（服务端未提供，
      保持原行为）
    - 换源前丢弃 .part 断点：续传字节属于上一个源的响应，叠加到新源会把
      两个源的内容拼成损坏文件。首个源保留断点续传（原行为，通常就是
      GitHub 直链，中断重试还能续）
    """
    urls = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
    if not urls:
        return {"error": "没有可用的下载源"}
    sha256 = (sha256 or "").strip().lower()
    failures = []
    for i, url in enumerate(urls):
        if i > 0:
            # 换源重置：丢上一源的 .part + 进度条归零（新源从头下）
            if tag:
                try:
                    _, tmp_path = _download_paths(tag)
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass  # 删不掉也继续（后续 wb 模式会整文件重写覆盖）
            if progress_callback:
                progress_callback(0)
        r = _download_one(url, progress_callback=progress_callback, tag=tag)
        if not r.get("ok"):
            failures.append(f"{url} → {r.get('error', '下载失败')}")
            continue
        if sha256 and not _verify_sha256(r.get("path", ""), sha256):
            # 校验失败的成品必须删掉：留着会被 apply_update 当有效新版本装上
            try:
                os.remove(r.get("path", ""))
            except OSError:
                pass
            failures.append(f"{url} → sha256 校验失败")
            continue
        return r
    return {"error": "所有下载源均失败（" + "；".join(failures) + "）"}


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
