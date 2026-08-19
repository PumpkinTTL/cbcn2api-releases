import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Optional, AsyncGenerator, AsyncIterator

import httpx
from fastapi import FastAPI, APIRouter, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .token_rotator import token_rotator
from .api_client import build_headers, build_chat_payload, resolve_base_url, AVAILABLE_MODELS
from src.storage.store import add_log, update_account_stats

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None
_http_client_loop = None  # client 绑定的事件循环；重启网关后新 loop 与旧 client 不匹配需重建
_proxy_password: str = os.environ.get("CBCN_PROXY_PASSWORD", "")
_platform: str = os.environ.get("CBCN_PROXY_PLATFORM", "workbuddy")
_port: int = int(os.environ.get("CBCN_PROXY_PORT", "8001"))


def update_config(port: int, password: str, platform: str):
    """更新网关配置（进程内模式：每次启动时调用，覆盖 import 时的值）。"""
    global _proxy_password, _platform, _port
    _proxy_password = password
    _platform = platform
    _port = port

security = HTTPBearer(auto_error=False)

# 上游错误分类
QUOTA_ERROR_CODES = {14018}                # 额度耗尽（账号级，body code）
MODEL_RATE_LIMIT_CODES = {6004}            # 单个模型每日额度/频率限制（body code）
QUEUE_ERROR_CODES = {6020}                 # 排队等待（模型级队列，queue.waiting.title，body code）
QUOTA_STATUS_CODES = {429}                 # HTTP 429 兜底：body code 未识别时归 quota
TRANSIENT_STATUS_CODES = {401, 502, 503, 504}  # 临时错误

# CodeBuddy 上游对 system prompt 做关键词审核：安全条款里列举攻击性术语
# （ZCode 等编程 agent 的固定 system prompt 常见）会被误判「输入存在敏感内容」
# 整段拦截，连普通对话都无法回复。这里把这类列举段重写为语义等价的中性
# 措辞（保留「拒绝恶意用途」语义、去掉攻击词清单），其余 prompt 原文不动。
_ZCODE_HINTS = ("You are ZCode", "You are an interactive ZCode agent")
_SAFE_SEGMENT_RE = re.compile(
    r"IMPORTANT: Assist with authorized security testing[^\n]*(?:\n(?!\n)[^\n]*)*"
)
# ZCode 会把当前项目的 gitStatus 快照（分支/改动文件/最近提交信息）注入
# system prompt 末尾。账号池/网关类项目的提交信息（签到、账号、导入等字眼）
# 会整段命中上游风控——连新会话发「你好」都被拦。替换为一行中性占位：
# 模型需要 git 状态时会自己跑命令，快照只是初始便利信息，去掉不影响使用。
_GIT_STATUS_RE = re.compile(r"\ngitStatus:.*\Z", re.S)
# 仅当快照内容命中风险词簇（账号池/签到/网关等运维特征词）时才脱敏该段；
# 普通项目的 gitStatus 原样保留，ZCode 的 git 上下文不受影响。
# 实测证据：ZCode 在 blog 项目带完整 gitStatus 通过、在 cbcn2api 项目
# 新会话即被拦——审核命中的是内容词，不是 prompt 格式。
_GIT_RISK_WORDS = ("账号", "签到", "网关", "号池", "反代", "封号", "抓包", "逆向")
# 实测（消融重放定位）：gitStatus 快照里 ZCode 固定注入的这行是强触发因子——
# "Main branch (you will usually use this for PRs): main" 单句即被上游风控
# 拦截（打分制，与 git 快照其他内容叠加更容易过阈值）。改写为等价短式：
# 语义完全一致（告诉模型主分支名），opencode/WorkBuddy 不带 gitStatus 从不触发。
_MAIN_BRANCH_RE = re.compile(r"Main branch \(you will usually use this for PRs\): (\S+)")


def _sanitize_system_prompt(text: str) -> str:
    """清洗 system prompt：攻击术语段改写 + Main branch 行改写 + 风险 gitStatus 脱敏（仅 ZCode）。"""
    if not any(h in text for h in _ZCODE_HINTS):
        return text
    text = _SAFE_SEGMENT_RE.sub(
        "IMPORTANT: Assist with authorized security testing and defensive use cases. "
        "Refuse requests for harmful purposes without proper authorization context.",
        text,
    )
    text = _MAIN_BRANCH_RE.sub(r"Main branch: \1", text)
    m = _GIT_STATUS_RE.search(text)
    if m and any(w in m.group(0) for w in _GIT_RISK_WORDS):
        text = text[: m.start()] + (
            "\ngitStatus: (snapshot omitted — run git commands if needed)\n"
        )
    return text

PEEK_BYTE_LIMIT = 32768  # peek 阶段最多缓冲字节数

# httpx 的 read 超时是「单次 socket 读取」的间隔超时，不是总时长，httpx 也没有
# total timeout 的概念。上游只要每 <read 超时 发一个心跳字节（SSE 的 ": ping"
# 注释行会被 _normalize_text_stream 静默跳过），请求就能永久挂着。
# 下面两个是墙钟上限，用来兜住这种「滴水不断但永不结束」的情况。
PEEK_TIMEOUT = 25.0        # peek 阶段最长等待：超过就当作正常数据放行，先让客户端见到响应
STREAM_DEADLINE = 600.0    # 单次请求从建连到收尾的总时长上限


def _get_http_client() -> httpx.AsyncClient:
    """获取与当前运行事件循环绑定的 httpx 异步客户端。

    httpx.AsyncClient 的连接池绑定在创建时的事件循环上。网关「停止→重启」
    会换一个新的事件循环（uvicorn 跑在独立线程），若继续复用旧 client，
    旧连接池指向已关闭的旧 loop → RuntimeError: Event loop is closed，
    重启后所有请求挂死，直到整个程序重启。

    这里记录 client 创建时所在的 loop，每次取用前比对当前 running loop：
    不一致（说明网关已重启到新 loop）就关闭旧的、按新 loop 重建。
    """
    global _http_client, _http_client_loop
    try:
        cur_loop = asyncio.get_running_loop()
    except RuntimeError:
        cur_loop = None
    if _http_client is not None and _http_client_loop is not cur_loop:
        # 绑定的 loop 已变（网关重启）：旧 client 不能用，丢弃重建。
        # 不 await aclose()——旧 loop 可能已关闭，await 会再次抛 Event loop is closed。
        # httpx client 不显式 close 只会延迟回收连接，不构成资源泄漏（进程级生命周期）。
        _http_client = None
        _http_client_loop = None
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            verify=False,
            trust_env=False,
            timeout=httpx.Timeout(60.0, connect=10.0, read=60.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
        _http_client_loop = cur_loop
    return _http_client


async def _close_http_client():
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


def authenticate(creds: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not _proxy_password:
        return True
    if creds is None or creds.credentials != _proxy_password:
        raise HTTPException(status_code=403, detail="Invalid proxy password")
    return True


def parse_sse_line(line: str) -> Optional[dict]:
    if not line.startswith("data: "):
        return None
    data = line[6:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _classify_upstream_error(status_code: int, body: str) -> Optional[str]:
    """解析上游错误体，返回可重试类型：
    'banned' | 'quota' | 'model' | 'queue' | 'auth' | 'transient' | None(不可重试)。

    11140（封号）的响应体是顶层 {"code":11140,"msg":"request illegal"}，无 error 包裹，
    与普通错误的嵌套结构不同，须单独识别 —— 只有真正的封号才返回 'banned'，
    普通 403 仍归 'auth'（临时鉴权，渐进冷却，不封号）。
    6020（queue.waiting.title）= 模型级排队，属限流的一种，返回 'queue'。
    """
    if "request illegal" in body.lower():
        return "banned"
    code = None
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        code = obj.get("code")  # 顶层 code（11140 等无 error 包裹的情况）
        if code is None:
            err = obj.get("error")
            if isinstance(err, dict):
                data = err.get("data")
                if isinstance(data, dict) and data.get("code") is not None:
                    code = data.get("code")
                elif err.get("code") is not None:
                    code = err.get("code")
    if code == 11140:
        return "banned"
    if code in QUOTA_ERROR_CODES:
        return "quota"
    if code in MODEL_RATE_LIMIT_CODES:
        return "model"
    if code in QUEUE_ERROR_CODES:
        return "queue"
    if status_code in QUOTA_STATUS_CODES:
        return "quota"
    if status_code == 403:
        return "auth"
    if status_code in TRANSIENT_STATUS_CODES:
        return "transient"
    return None


def _safe_log(*args, **kwargs):
    """add_log 的兜底包装：写日志失败绝不能影响请求本身。"""
    try:
        add_log(*args, **kwargs)
    except Exception as e:
        logger.warning("[调度] 写日志失败: %r", e)


def _first_event_kind(text: str):
    """扫描文本中的首个 SSE data 事件。
    返回 ('error', payload_str) | ('data', None) | ('none', None)。

    识别为 error 的形状：
      - {"error": {...}}（嵌套错误）
      - {"code":11140,"msg":"request illegal"}（顶层封号错误，无 error 包裹）
    其余按 data / none 处理。"""
    has_data = False
    for line in text.split("\n"):
        s = line.strip()
        if not s.startswith("data:"):
            continue
        payload_str = s[5:].strip()
        if not payload_str or payload_str == "[DONE]":
            continue
        try:
            obj = json.loads(payload_str)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            if isinstance(obj.get("error"), dict):
                return "error", payload_str
            if obj.get("code") == 11140 or "request illegal" in payload_str.lower():
                return "error", payload_str
            if obj.get("choices") or obj.get("id") or obj.get("usage"):
                has_data = True
    return ("data" if has_data else "none"), None


def _extract_consumed(usage: dict) -> float:
    """从 usage 对象提取消耗的积分/额度。"""
    import math
    for key in ("credit", "deduction", "cost", "credits", "consumed", "points"):
        v = usage.get(key)
        if v is not None:
            try:
                fv = float(v)
                if math.isfinite(fv):
                    return fv
            except (ValueError, TypeError):
                pass
    return 0.0


async def _normalize_text_stream(
    aiter: AsyncIterator[str],
    usage_box: Optional[dict] = None,
    deadline: Optional[float] = None,
) -> AsyncGenerator[str, None]:
    """规整上游 SSE，对照 9router passthrough 模式：
    - 只删空 tool_calls: []（CodeBuddy CN 每块都带，AI SDK 误判）
    - 补 object/created 若缺失
    - 丢弃无实质内容的空块（hasValuableContent）
    - 其余字段原样透传

    deadline 是 time.monotonic() 的绝对时刻；超过就补一个 [DONE] 收尾退出，
    避免上游慢速滴字导致这个 async for 永不结束。
    """
    buffer = ""
    async for chunk in aiter:
        if deadline is not None and time.monotonic() > deadline:
            logger.warning("[调度] 流式响应超过总时长上限，主动收尾")
            yield ("data: " + json.dumps(
                {"error": {"message": "响应超过总时长上限，已中断", "type": "timeout"}},
                ensure_ascii=False) + "\n\n")
            yield "data: [DONE]\n\n"
            return
        if not chunk:
            continue
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            stripped = line.strip()
            if stripped.startswith(":") or not stripped:
                continue
            if stripped.startswith("data:"):
                payload_str = stripped[5:].strip()
                if payload_str == "[DONE]":
                    yield "data: [DONE]\n\n"
                    return
                try:
                    obj = json.loads(payload_str)

                    # 补 object/created
                    if "choices" in obj:
                        obj.setdefault("object", "chat.completion.chunk")
                        obj.setdefault("created", int(time.time()))

                    # 捕获 usage（最后一个 chunk 携带）
                    if usage_box is not None and isinstance(obj.get("usage"), dict):
                        usage_box["usage"] = obj["usage"]

                    for choice in obj.get("choices", []):
                        delta = choice.get("delta", {})
                        # 删空 tool_calls: []（9router 专门为 CodeBuddy CN 加的修复）
                        tc = delta.get("tool_calls")
                        if isinstance(tc, list) and len(tc) == 0:
                            del delta["tool_calls"]

                    # hasValuableContent：丢弃无实质内容的块
                    choices = obj.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        has_content = (
                            (delta.get("content") is not None and delta["content"] != "")
                            or (delta.get("reasoning_content") is not None and delta["reasoning_content"] != "")
                            or (delta.get("tool_calls") and len(delta["tool_calls"]) > 0)
                            or choices[0].get("finish_reason")
                            or delta.get("role")
                        )
                        if not has_content:
                            continue

                    yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
                    continue
                except (json.JSONDecodeError, IndexError):
                    pass
            yield stripped + "\n\n"
    if buffer.strip() and not buffer.strip().startswith(":"):
        yield buffer.strip() + "\n\n"
    yield "data: [DONE]\n\n"




async def _stream_inner(
    payload: dict,
    conversation_id: Optional[str],
    platform: str,
) -> AsyncGenerator[str, None]:
    """带故障转移的流式生成器：首个账号出错（额度/鉴权）时自动切下一个账号，
    在向客户端发送任何内容之前完成切换。"""
    fallback = "codebuddy_cn" if platform != "codebuddy_cn" else "workbuddy"
    # system prompt 脱敏：上游关键词审核会误伤安全条款里的攻击词列举段
    # （ZCode 固定注入的 system prompt 必中），转发前重写，只处理一次
    for m in payload.get("messages", []):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            m["content"] = _sanitize_system_prompt(m["content"])
    # count_usable 在池未加载时返回 0（get_next 才会触发 reload），max(...,1) 会让
    # 明明有 N 个号的情况只试一次。先确保池已加载再算重试次数。
    token_rotator.ensure_loaded(platform)
    # 重试上限 = 池总数（正常号 + 限流探测号），配合 tried_ids 去重 ——
    # 每个账号本请求最多试一次：正常号失败进限流，限流号失败保持限流，
    # 不会对同一个号反复轰炸（探测风暴），也保证有限次数内必然结束（无死循环）。
    max_attempts = max(token_rotator.count_total(), 1)
    tried_ids: set = set()
    last_msg = "无可用账号"
    model = payload.get("model", "")
    deadline = time.monotonic() + STREAM_DEADLINE
    sent_any = False   # 是否已经向客户端 yield 过正文；一旦为 True 就不能再重试换号

    for _attempt in range(max_attempts):
        if time.monotonic() > deadline:
            last_msg = "请求超过总时长上限"
            break
        acc = token_rotator.get_next(platform) or token_rotator.get_next(fallback)
        if not acc:
            break
        if acc.id in tried_ids:
            # 池已轮空（get_next 只会重复返回试过的号）：所有账号本请求都试过了
            last_msg = "所有账号均已尝试且不可用"
            break
        tried_ids.add(acc.id)

        # add_log 是同步 sqlite 写，GUI 线程并发写库时可能抛 "database is locked"。
        # 这里裸调会让异常从生成器里穿出去，把一次本来能成功的请求打成 500。
        # request 日志只记模型 + 消息数，不保存消息内容/会话记录 ——
        # 全量会话会让单条 details 膨胀到几百 KB，日志页一次拉 200 条即内存爆炸。
        # （要排查「客户端注入提示词导致上游拦截」时，看 error 事件的详情即可）
        req_detail = ""
        try:
            msgs = payload.get("messages", []) or []
            req_detail = json.dumps(
                {"model": model, "msgs": len(msgs)}, ensure_ascii=False)
        except Exception:
            pass
        _safe_log("request", platform, acc.id, acc.nickname, model, f"开始请求 model={model}", req_detail)

        base_url = resolve_base_url()
        api_url = f"{base_url}/v2/chat/completions"
        headers = build_headers(acc.access_token, acc.uid, conversation_id,
                                fingerprint=acc.fingerprint,
                                enterprise_id=acc.enterprise_id, domain=acc.domain)
        client = _get_http_client()

        try:
            resp = await client.send(
                client.build_request("POST", api_url, json=payload, headers=headers),
                stream=True,
            )
        except httpx.ConnectError as e:
            logger.warning("[调度] 账号=%s 连接失败: %s", acc.nickname, e)
            _safe_log("error", platform, acc.id, acc.nickname, model, f"连接失败: {e}", "")
            last_msg = f"无法连接上游: {e}"
            # 原先是 break 且不标记冷却 —— 单个账号连不上就放弃整轮，没有故障转移。
            # 连接失败按 transient 处理并继续换号，全部失败时循环自然走完。
            token_rotator.mark_disabled(acc.id, "transient")
            continue
        except httpx.TimeoutException as e:
            logger.warning("[调度] 账号=%s 超时, 标记transient: %s", acc.nickname, e)
            _safe_log("error", platform, acc.id, acc.nickname, model, f"请求超时", "")
            token_rotator.mark_disabled(acc.id, "transient")
            last_msg = f"上游超时: {e}"
            continue

        try:
            # 非 200：解析错误体，可重试则切号
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="ignore")
                await resp.aclose()
                kind = _classify_upstream_error(resp.status_code, body)
                logger.warning("[调度] 账号=%s 上游返回 %d, kind=%s, body=%s", acc.nickname, resp.status_code, kind, body[:200])
                if kind:
                    token_rotator.mark_disabled(acc.id, kind, model=model)
                    _safe_log("error", platform, acc.id, acc.nickname, model, f"HTTP {resp.status_code} → {kind}", body[:500])
                    last_msg = body[:300]
                    continue
                _safe_log("error", platform, acc.id, acc.nickname, model, f"HTTP {resp.status_code} (不可重试)", body[:500])
                yield f"data: {json.dumps({'error': {'message': body, 'type': 'upstream_error', 'status': resp.status_code}}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 200：peek 首个事件，检测内联错误，确认无误后再向客户端输出
            # 只创建一个迭代器，peek 和后续共用（httpx 不允许重复 aiter_text）
            text_iter = resp.aiter_text(chunk_size=8192)
            buffered = []
            buffered_len = 0
            decision = "none"
            inline_err = None
            peek_deadline = min(time.monotonic() + PEEK_TIMEOUT, deadline)
            async for chunk in text_iter:
                buffered.append(chunk)
                buffered_len += len(chunk)
                # 必须对**累积文本**解析：一个 SSE 错误事件可能被切在 8KB chunk
                # 边界上，两半各自 json.loads 都失败，于是 14018/429 检测不到、
                # 不触发换号，错误体直接透传给客户端，故障转移形同虚设。
                kind, err = _first_event_kind("".join(buffered))
                if kind == "error":
                    decision = "error"
                    inline_err = err
                    break
                if kind == "data":
                    decision = "data"
                    break
                if buffered_len > PEEK_BYTE_LIMIT:
                    decision = "data"
                    break
                # 只收到心跳/注释时，退出条件全是字节量的话会一直卡在这里，
                # 而响应头已经发出去了，客户端表现为「连上了但一个字都没有」。
                if time.monotonic() > peek_deadline:
                    logger.warning("[调度] 账号=%s peek 超时，按正常数据放行", acc.nickname)
                    decision = "data"
                    break

            if decision == "none":
                # peek 全程只收到心跳/注释，无实质数据 → 上游假死，按 transient 换号
                await resp.aclose()
                token_rotator.mark_disabled(acc.id, "transient")
                _safe_log("error", platform, acc.id, acc.nickname, model, "peek 无实质内容，疑似上游假死", "")
                last_msg = "上游无实质响应"
                continue

            if decision == "error" and inline_err:
                await resp.aclose()
                kind = _classify_upstream_error(200, inline_err)
                logger.warning("[调度] 账号=%s 200内联错误, kind=%s, err=%s", acc.nickname, kind, inline_err[:200])
                if kind:
                    token_rotator.mark_disabled(acc.id, kind, model=model)
                    _safe_log("error", platform, acc.id, acc.nickname, model, f"200内联错误 → {kind}", inline_err[:500])
                    last_msg = inline_err[:300]
                    continue
                _safe_log("error", platform, acc.id, acc.nickname, model, "200内联错误(不可重试)", inline_err[:500])
                # 不可重试的内联错误
                try:
                    eobj = json.loads(inline_err)
                    err_payload = eobj.get("error", {"message": inline_err})
                except (ValueError, TypeError):
                    err_payload = {"message": inline_err}
                yield f"data: {json.dumps({'error': err_payload, 'type': 'upstream_error'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 成功：重放已缓冲内容 + 剩余流（用同一个迭代器）
            async def _combined():
                for c in buffered:
                    yield c
                async for c in text_iter:
                    yield c

            logger.info("[调度] 账号=%s 请求成功", acc.nickname)
            # 限流探测成功：真实请求通了 = 上游已解除，逐模型清除限流标记
            # （model 限流号只清本次请求的模型；transient/quota 号整体恢复）
            try:
                token_rotator.clear_model_disabled(acc.id, model)
            except Exception:
                pass
            usage_box = {}
            try:
                async for piece in _normalize_text_stream(_combined(), usage_box, deadline):
                    sent_any = True
                    yield piece
            finally:
                await resp.aclose()
                if usage_box.get("usage"):
                    u = usage_box["usage"]
                    consumed = _extract_consumed(u)
                    if consumed > 0:
                        token_rotator.deduct_quota(acc.id, consumed)
                    update_account_stats(platform, acc.id, u)
                    _safe_log("success", platform, acc.id, acc.nickname, model, f"消耗 {consumed}" if consumed > 0 else "请求成功", "")
            return
        except httpx.TimeoutException as e:
            await resp.aclose()
            last_msg = f"上游超时: {e}"
            token_rotator.mark_disabled(acc.id, "transient")
            # 已经吐给客户端的内容收不回来了。此时再换号重发会让流式客户端收到
            # 「半个回答 + 一个完整回答」，非流式更糟：两次正文会被拼进同一条
            # message。所以只能就地收尾，不能 continue。
            if sent_any:
                logger.warning("[调度] 账号=%s 流中途超时，已发出内容，不再重试", acc.nickname)
                _safe_log("error", platform, acc.id, acc.nickname, model, "流中途超时，已发出部分内容", "")
                yield ("data: " + json.dumps(
                    {"error": {"message": last_msg, "type": "upstream_timeout"}},
                    ensure_ascii=False) + "\n\n")
                yield "data: [DONE]\n\n"
                return
            continue
        except Exception:
            await resp.aclose()
            raise

    # 全部账号耗尽：透传给客户端，提示无号可用请查看日志（last_msg 是最后一条失败原因）。
    # 日志里附各限流原因统计（排队几个/耗尽几个/临时几个/封号几个），一眼看出
    # 调度停在哪类原因上，不用翻细节。
    reason_stats = {}
    try:
        with token_rotator._lock:
            for _aid, _s in (token_rotator._disabled or {}).items():
                _r = _s.get("reason")
                if _r:
                    reason_stats[_r] = reason_stats.get(_r, 0) + 1
    except Exception:
        pass
    stats_txt = "、".join(f"{k}={v}" for k, v in sorted(reason_stats.items())) if reason_stats else "无"
    logger.warning("[调度] 所有账号不可用, last_msg=%s, 限流分布: %s", last_msg, stats_txt)
    _safe_log("error", platform, "", "", model, f"所有账号不可用: {last_msg}（限流分布: {stats_txt}）", "")
    hint = f"无号可用，请查看日志（最后原因: {last_msg}）" if last_msg else "无号可用，请查看日志"
    yield f"data: {json.dumps({'error': {'message': hint, 'type': 'no_account'}}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_with_failover(
    payload: dict,
    conversation_id: Optional[str],
    platform: str,
) -> AsyncGenerator[str, None]:
    """带 active 状态追踪的流式生成器包装。"""
    token_rotator.set_active(True)
    try:
        async for chunk in _stream_inner(payload, conversation_id, platform):
            yield chunk
    finally:
        token_rotator.set_active(False)


async def _non_stream_chat(
    payload: dict,
    conversation_id: Optional[str],
    platform: str,
) -> dict:
    content_parts = []
    tool_call_map = {}
    tool_call_order = []
    current_tool_index = None
    finish_reason = "stop"
    resp_id = None
    resp_model = None
    usage_info = None

    gen = _stream_with_failover(payload, conversation_id, platform)
    try:
        async for raw in gen:
            obj = parse_sse_line(raw)
            if not obj:
                continue
            if isinstance(obj.get("error"), dict):
                return {"error": obj["error"]}

            resp_id = resp_id or obj.get("id")
            resp_model = resp_model or obj.get("model")
            if obj.get("usage"):
                usage_info = obj["usage"]

            choices = obj.get("choices", [])
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta", {})

            if delta.get("content"):
                content_parts.append(delta["content"])

            tool_calls = delta.get("tool_calls", [])
            for tc in tool_calls:
                tool_index = tc.get("index")
                tid = tc.get("id")
                if tool_index is None:
                    tool_index = current_tool_index
                if tool_index is None:
                    tool_index = len(tool_call_map)

                if tool_index not in tool_call_map:
                    tool_call_map[tool_index] = {
                        "id": tid or "",
                        "type": tc.get("type", "function"),
                        "function": {"name": "", "arguments": ""},
                    }
                    tool_call_order.append(tool_index)

                current_tool_index = tool_index
                current = tool_call_map[tool_index]
                if tid:
                    current["id"] = tid
                if tc.get("type"):
                    current["type"] = tc["type"]
                func = tc.get("function", {})
                if func.get("name"):
                    current["function"]["name"] = func["name"]
                if func.get("arguments"):
                    current["function"]["arguments"] += func["arguments"]

        content = "".join(content_parts)
        final_tool_calls = [tool_call_map[index] for index in tool_call_order] if tool_call_order else None
        final_finish = "tool_calls" if final_tool_calls else (finish_reason or "stop")

        message = {"role": "assistant", "content": content if content else (None if final_tool_calls else "")}
        if final_tool_calls:
            message["tool_calls"] = final_tool_calls

        result = {
            "id": resp_id or str(uuid.uuid4()),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp_model or payload.get("model", "auto"),
            "choices": [{"index": 0, "message": message, "finish_reason": final_finish}],
        }
        if usage_info:
            result["usage"] = usage_info
        return result
    finally:
        await gen.aclose()


router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    _auth=Depends(authenticate),
    x_conversation_id: Optional[str] = Header(None, alias="X-Conversation-ID"),
):
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    messages = body.get("messages", [])
    if not messages or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages required")
    if len(messages) == 0:
        raise HTTPException(status_code=400, detail="at least one message required")

    payload = build_chat_payload(body)

    try:
        if body.get("stream", False):
            return StreamingResponse(
                _stream_with_failover(payload, x_conversation_id, _platform),
                media_type="text/event-stream",
            )

        result = await _non_stream_chat(payload, x_conversation_id, _platform)
        if isinstance(result, dict) and result.get("error"):
            raise HTTPException(
                status_code=503,
                detail=result["error"].get("message", "upstream error"),
            )
        return JSONResponse(content=result)
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Cannot connect to copilot.tencent.com")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream API timed out")


@router.get("/v1/models")
async def list_models(_auth=Depends(authenticate)):
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": int(time.time()), "owned_by": "copilot.tencent.com"}
            for m in AVAILABLE_MODELS
        ],
    }


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/proxy/info")
async def proxy_info():
    st = token_rotator.status()
    return {
        "platform": _platform,
        "proxy_port": _port,
        "upstream": resolve_base_url(),
        "accounts_total": st["total"],
        "accounts_usable": st["usable"],
        "accounts_disabled": st["disabled"],
        "current_account": st["current"],
        "active": st["active"],
        "threshold_switch": st.get("threshold_switch"),
        "models": AVAILABLE_MODELS,
    }


@router.get("/__gw/quota")
async def gw_quota():
    """网关额度（注入 WorkBuddy 用）。完全复刻 app.get_stats 算法，和 GUI 顶部额度条同口径：
    跳过 banned；calc_totals(quota_raw, usage_raw, active_only=False) + account_stats.total_credit。
    每次 fetch 实时算 —— 消耗通过 total_credit（token_rotator 每次请求扣减写 store）实时反映。"""
    from src.storage import store
    from src.api import quota as Q
    accs = store.list_accounts(_platform)
    stats = {s["account_id"]: s for s in store.list_account_stats(_platform)}
    total_used = 0.0
    total_remain = 0.0
    for a in accs:
        if a.status == "banned":
            continue
        try:
            t, u = Q.calc_totals(a.quota_raw, a.usage_raw, active_only=False)
        except Exception:
            t, u = 0.0, 0.0
        credit = float((stats.get(a.id) or {}).get("total_credit") or 0)
        used = u + credit
        total_used += used
        total_remain += max(0.0, t - used)
    return {
        "used": round(total_used, 2),
        "remain": round(total_remain, 2),
        "count": len(accs),
    }


app = FastAPI(title="cbcn2api Proxy", version="0.3.0", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.on_event("startup")
async def preload():
    token_rotator.reload(_platform)


@app.on_event("shutdown")
async def cleanup():
    token_rotator.persist_estimates()
    await _close_http_client()
