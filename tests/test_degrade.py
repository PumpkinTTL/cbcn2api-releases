"""模型降级链（6004/6020）逻辑测试：用假上游按「账号 × 模型」回放，驱动真实 _stream_inner。

不触发真实限流 —— 把上游响应脚本化（6004/6020/14018/200），验证：
  - 同号降级：撞 6004 后同一账号按降级顺序换下一个模型重试
  - 链尽换号复位：整条链都失败才换号，新号从用户请求的原模型开始
  - 总开关关闭 / 模型不在链中 → 不降级直接换号
  - 账号级错误（quota 14018 / banned / transient）→ 不降级直接换号
  - 排队 6020 同样走降级
  - 全失败有界退出（防死循环：请求数 = 账号数 × 链长，绝不无限重试）

只 stub 外部依赖（httpx / fastapi / sqlite store / 配额解析），
_stream_inner、_classify_upstream_error、_normalize_text_stream、TokenRotator 全是真实代码。
"""
import asyncio
import json
import sys
import types
import time

REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)

# ---------------------------------------------------------------- stub httpx
class _TimeoutException(Exception): pass
class _ConnectError(Exception): pass

class FakeResponse:
    def __init__(self, status_code=200, body="", chunks=None):
        self.status_code = status_code
        self._body = body
        self._chunks = chunks or []
        self.closed = False

    async def aread(self):
        return self._body.encode()

    async def aclose(self):
        self.closed = True

    async def aiter_text(self, chunk_size=8192):
        for c in self._chunks:
            yield c
            await asyncio.sleep(0)

class FakeClient:
    """按 (账号, 模型) 回放上游行为；未脚本化的组合走 default（默认 6004 仍限流）。"""
    def __init__(self, script, default=None):
        self.script = script
        self.default = default
        self.calls = []          # [(acct, model), ...]

    def build_request(self, method, url, json=None, headers=None):
        return types.SimpleNamespace(url=url, headers=headers, json=json or {})

    async def send(self, request, stream=True):
        acct = request.headers.get("__acct__")
        model = (request.json or {}).get("model", "?")
        self.calls.append((acct, model))
        item = self.script.get((acct, model))
        if item is None:
            item = self.default
        if item is None:
            raise AssertionError(f"未脚本化的请求: {acct} model={model}")
        if isinstance(item, Exception):
            raise item
        return item

httpx = types.ModuleType("httpx")
httpx.TimeoutException = _TimeoutException
httpx.ConnectError = _ConnectError
httpx.AsyncClient = lambda **kw: None
httpx.Timeout = lambda *a, **k: None
httpx.Limits = lambda *a, **k: None
sys.modules["httpx"] = httpx

# -------------------------------------------------------------- stub fastapi
def _deco(*a, **k):
    def wrap(fn): return fn
    return wrap
class _Router:
    def post(self, *a, **k): return _deco()
    def get(self, *a, **k): return _deco()
class _App:
    def add_middleware(self, *a, **k): pass
    def include_router(self, *a, **k): pass
    def on_event(self, *a, **k): return _deco()

fa = types.ModuleType("fastapi")
fa.FastAPI = lambda **k: _App()
fa.APIRouter = lambda **k: _Router()
fa.Request = object
fa.HTTPException = type("HTTPException", (Exception,), {})
fa.Depends = lambda x=None: None
fa.Header = lambda *a, **k: None
sys.modules["fastapi"] = fa
for name, attrs in [
    ("fastapi.middleware.cors", {"CORSMiddleware": object}),
    ("fastapi.responses", {"StreamingResponse": object, "JSONResponse": object}),
    ("fastapi.security", {"HTTPBearer": lambda **k: None,
                          "HTTPAuthorizationCredentials": object}),
]:
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m
sys.modules["fastapi.middleware"] = types.ModuleType("fastapi.middleware")

# ------------------------------------------------------- stub store / quota
LOGS = []
SETTINGS = {}
store_stub = types.ModuleType("src.storage.store")
store_stub.add_log = lambda *a, **k: LOGS.append(a)
store_stub.add_switch_log = lambda *a, **k: None
store_stub.update_account_stats = lambda *a, **k: None
store_stub.list_accounts = lambda p: []
store_stub.get_setting = lambda k, d="": SETTINGS.get(k, d)
store_stub.save_setting = lambda k, v: SETTINGS.__setitem__(k, v)
store_stub.upsert_account = lambda p, a: None
sys.modules["src.storage.store"] = store_stub
storage_pkg = types.ModuleType("src.storage")
storage_pkg.store = store_stub
sys.modules["src.storage"] = storage_pkg

quota_stub = types.ModuleType("src.api.quota")
quota_stub.calc_totals = lambda a, b: (1000, 0)
sys.modules["src.api.quota"] = quota_stub

# --------------------------------------------------------------- 真实代码
import src.proxy.proxy_server as ps
from src.proxy.token_rotator import TokenRotator
from src.models.account import Account

# 让 build_headers 带上账号标识，方便假 client 按账号回放
_orig_build_headers = ps.build_headers
def tagged_headers(token, uid, conv, fingerprint=None, enterprise_id=None, domain=None):
    h = dict(_orig_build_headers(token, uid, conv, fingerprint=fingerprint,
                                 enterprise_id=enterprise_id, domain=domain))
    h["__acct__"] = token          # token 里塞的是 account id
    return h
ps.build_headers = tagged_headers


def make_rotator(ids):
    r = TokenRotator()
    r._accounts = [Account(id=i, access_token=i, uid=i, nickname=i, status="normal")
                   for i in ids]
    r._estimated_remain = {i: 1000.0 for i in ids}
    r._estimate_valid = set(ids)
    r._platform = "wb"
    r._current_id = ids[0]
    return r


def set_degrade(enabled=True, order=None):
    """配置降级顺序（写入 store stub 的 settings，_load_degrade_config 会读）。"""
    order = order or ["glm-5.3", "glm-5.2", "glm-5.1"]
    from src.proxy.api_client import DEGRADE_CONFIG_KEY
    SETTINGS[DEGRADE_CONFIG_KEY] = json.dumps({"enabled": enabled, "order": order})


def resp_6004():
    return FakeResponse(429, body='{"code":6004,"msg":"频率限制"}')

def resp_6020():
    return FakeResponse(429, body='{"code":6020,"msg":"排队等待"}')

def resp_14018():
    return FakeResponse(429, body='{"error":{"data":{"code":14018}}}')

def resp_ok(text):
    return FakeResponse(200, chunks=[
        'data: {"id":"r1","choices":[{"delta":{"role":"assistant"}}]}\n\n',
        'data: {"id":"r1","choices":[{"delta":{"content":"%s"}}]}\n\n' % text,
        'data: {"id":"r1","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"credit":2}}\n\n',
        'data: [DONE]\n\n',
    ])


async def drain(payload, rot, script, default=None):
    ps.token_rotator = rot
    client = FakeClient(script, default=default)
    ps._get_http_client = lambda: client
    out = []
    async for piece in ps._stream_inner(payload, None, "wb"):
        out.append(piece)
    return out, client


def sse_texts(frames):
    s = ""
    for f in frames:
        for line in f.split("\n"):
            if line.startswith("data: ") and "[DONE]" not in line:
                try:
                    o = json.loads(line[6:])
                except Exception:
                    continue
                for ch in o.get("choices", []):
                    s += ch.get("delta", {}).get("content", "") or ""
    return s


def errors_of(frames):
    out = []
    for f in frames:
        for line in f.split("\n"):
            if line.startswith("data: ") and "[DONE]" not in line:
                try:
                    o = json.loads(line[6:])
                except Exception:
                    continue
                if isinstance(o.get("error"), dict):
                    out.append(o["error"])
    return out


def models_disabled(rot, aid):
    st = rot._disabled.get(aid)
    if not st:
        return []
    return list(st.get("models") or [])


PASS = []
def check(name, cond, detail=""):
    PASS.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def t1():
    print("\n测试1  同号降级成功：glm-5.3 撞 6004 → 同号降 glm-5.2 → 200")
    set_degrade()
    rot = make_rotator(["a1", "a2"])
    script = {
        ("a1", "glm-5.3"): resp_6004(),
        ("a1", "glm-5.2"): resp_ok("来自a1/glm-5.2"),
    }
    frames, client = asyncio.run(drain({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    check("a1 先 glm-5.3 后 glm-5.2（同号降级）",
          client.calls == [("a1", "glm-5.3"), ("a1", "glm-5.2")], f"calls={client.calls}")
    check("没有换号（a2 未被调用）", ("a2", "glm-5.3") not in client.calls, f"calls={client.calls}")
    check("正文来自降级后的 glm-5.2", sse_texts(frames) == "来自a1/glm-5.2", repr(sse_texts(frames)))
    check("a1 只标记了 glm-5.3（glm-5.2 成功不清它）", models_disabled(rot, "a1") == ["glm-5.3"],
          f"models={models_disabled(rot, 'a1')}")
    check("客户端没收到错误帧", errors_of(frames) == [], str(errors_of(frames)))


def t2():
    print("\n测试2  链尽才换号，新号从原模型复位：A 整链 6004 → B 从 glm-5.3 开始")
    set_degrade()
    rot = make_rotator(["a1", "a2"])
    script = {
        ("a1", "glm-5.3"): resp_6004(),
        ("a1", "glm-5.2"): resp_6004(),
        ("a1", "glm-5.1"): resp_6004(),
        ("a2", "glm-5.3"): resp_ok("来自a2"),
    }
    frames, client = asyncio.run(drain({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    check("A 号走完整条链（3 个模型）", client.calls[:3] == [("a1", "glm-5.3"), ("a1", "glm-5.2"), ("a1", "glm-5.1")],
          f"calls={client.calls}")
    check("B 号从原模型 glm-5.3 开始（换号复位）", client.calls[3] == ("a2", "glm-5.3"),
          f"calls={client.calls}")
    check("正文来自 a2", sse_texts(frames) == "来自a2", repr(sse_texts(frames)))
    check("a1 累积 3 个被限模型", models_disabled(rot, "a1") == ["glm-5.3", "glm-5.2", "glm-5.1"],
          f"models={models_disabled(rot, 'a1')}")


def t3():
    print("\n测试3  总开关关闭 → 不降级，直接换号")
    set_degrade(enabled=False)
    rot = make_rotator(["a1", "a2"])
    script = {
        ("a1", "glm-5.3"): resp_6004(),
        ("a2", "glm-5.3"): resp_ok("来自a2"),
    }
    frames, client = asyncio.run(drain({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    check("关闭降级后 glm-5.3 撞限直接换号", client.calls == [("a1", "glm-5.3"), ("a2", "glm-5.3")],
          f"calls={client.calls}")
    check("正文来自 a2", sse_texts(frames) == "来自a2", repr(sse_texts(frames)))


def t4():
    print("\n测试4  账号级错误（quota 14018）不降级，直接换号")
    set_degrade()
    rot = make_rotator(["a1", "a2"])
    script = {
        ("a1", "glm-5.3"): resp_14018(),   # 账号级额度耗尽
        ("a2", "glm-5.3"): resp_ok("来自a2"),
    }
    frames, client = asyncio.run(drain({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    check("quota 不降级（没有 glm-5.2 尝试）", client.calls == [("a1", "glm-5.3"), ("a2", "glm-5.3")],
          f"calls={client.calls}")
    check("正文来自 a2", sse_texts(frames) == "来自a2", repr(sse_texts(frames)))
    check("a1 按 quota 整体标记", rot._disabled.get("a1", {}).get("reason") == "quota",
          str(rot._disabled.get("a1")))


def t5():
    print("\n测试5  排队 6020 也走降级（排队是模型级，切号无用，降级是活路）")
    set_degrade()
    rot = make_rotator(["a1", "a2"])
    script = {
        ("a1", "glm-5.3"): resp_6020(),
        ("a1", "glm-5.2"): resp_ok("来自a1/glm-5.2"),
    }
    frames, client = asyncio.run(drain({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    check("6020 排队 → 同号降级 glm-5.2", client.calls == [("a1", "glm-5.3"), ("a1", "glm-5.2")],
          f"calls={client.calls}")
    check("正文来自降级后的 glm-5.2", sse_texts(frames) == "来自a1/glm-5.2", repr(sse_texts(frames)))
    check("a1 按 queue 标记 glm-5.3", rot._disabled.get("a1", {}).get("reason") == "queue"
          and models_disabled(rot, "a1") == ["glm-5.3"], str(rot._disabled.get("a1")))


def t6():
    print("\n测试6  全部账号 × 全部链模型都 6004 → 有界退出（防死循环核心）")
    set_degrade()
    ids = ["a1", "a2", "a3"]
    rot = make_rotator(ids)
    script = {}
    for a in ids:
        for m in ("glm-5.3", "glm-5.2", "glm-5.1"):
            script[(a, m)] = resp_6004()
    t0 = time.monotonic()
    frames, client = asyncio.run(drain({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    el = time.monotonic() - t0
    check("请求数 = 账号数 × 链长 = 9，绝无重复轰炸",
          len(client.calls) == 9, f"calls={client.calls}")
    check("每个号恰好各试 3 个模型",
          sorted(set(a for a, _ in client.calls)) == ids
          and all(sum(1 for a, _ in client.calls if a == i) == 3 for i in ids),
          f"calls={client.calls}")
    check("有界退出（耗时 < 5s）", el < 5, f"{el:.2f}s")
    errs = errors_of(frames)
    check("返回 no_account", errs and errs[0].get("type") == "no_account", str(errs))
    check("以 [DONE] 收尾", "[DONE]" in frames[-1])


def t7():
    print("\n测试7  模型不在降级链中 → 不降级直接换号")
    set_degrade(order=["kimi-k3-1", "glm-5.2"])   # glm-5.3 不在链里
    rot = make_rotator(["a1", "a2"])
    script = {
        ("a1", "glm-5.3"): resp_6004(),
        ("a2", "glm-5.3"): resp_ok("来自a2"),
    }
    frames, client = asyncio.run(drain({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    check("glm-5.3 不在链中 → 撞限直接换号", client.calls == [("a1", "glm-5.3"), ("a2", "glm-5.3")],
          f"calls={client.calls}")
    check("正文来自 a2", sse_texts(frames) == "来自a2", repr(sse_texts(frames)))


def t9():
    print("\n测试9  跨厂商链：Kimi 限 → 降 glm-5.3 也限 → 链尽切号 → 新号从 Kimi 复位（用户场景）")
    set_degrade(order=["kimi-k3-1", "glm-5.3"])   # 链 = kimi-k3-1 → glm-5.3
    rot = make_rotator(["a1", "a2"])
    script = {
        ("a1", "kimi-k3-1"): resp_6004(),
        ("a1", "glm-5.3"): resp_6004(),
        ("a2", "kimi-k3-1"): resp_ok("来自a2/kimi"),
    }
    frames, client = asyncio.run(drain({"model": "kimi-k3-1",
                                        "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    check("完整调用序列：a1/kimi → a1/glm-5.3 → a2/kimi（同号跨厂商降级 + 链尽换号复位）",
          client.calls == [("a1", "kimi-k3-1"), ("a1", "glm-5.3"), ("a2", "kimi-k3-1")], f"calls={client.calls}")
    check("链尽换号，B 号从原模型 kimi-k3-1 开始（不是 glm）",
          client.calls[2] == ("a2", "kimi-k3-1"), f"calls={client.calls}")
    check("正文来自 a2/kimi（用户拿到的还是原模型）", sse_texts(frames) == "来自a2/kimi", repr(sse_texts(frames)))
    check("a1 累积 kimi-k3-1 + glm-5.3 两个被限模型",
          models_disabled(rot, "a1") == ["kimi-k3-1", "glm-5.3"], f"models={models_disabled(rot, 'a1')}")

    # 模型感知联动：a1 被限 kimi+glm，但 deepseek 没限 → 请求 deepseek 应照常调度 a1
    rot._current_id = "a1"
    script2 = {("a1", "deepseek-v4-pro"): resp_ok("来自a1/deepseek")}
    frames2, client2 = asyncio.run(drain({"model": "deepseek-v4-pro",
                                          "messages": [{"role": "user", "content": "hi"}]}, rot, script2))
    check("模型感知：a1 的 kimi/glm 被限不影响 deepseek 调度",
          client2.calls == [("a1", "deepseek-v4-pro")], f"calls={client2.calls}")
    check("deepseek 正文来自 a1（未浪费其他号额度）", sse_texts(frames2) == "来自a1/deepseek", repr(sse_texts(frames2)))


def t8():
    print("\n测试8  降级后模型也 200 内联 6004 → 继续降级（内联错误同样走链）")
    set_degrade()
    rot = make_rotator(["a1", "a2"])
    script = {
        ("a1", "glm-5.3"): resp_6004(),
        # glm-5.2 返回 200 但 SSE 里内联 6004 错误（切在 chunk 边界）
        ("a1", "glm-5.2"): FakeResponse(200, chunks=[
            'data: {"error":{"message":"rate","code":6004}}\n\n'[:20],
            'data: {"error":{"message":"rate","code":6004}}\n\n'[20:],
        ]),
        ("a1", "glm-5.1"): resp_ok("来自a1/glm-5.1"),
    }
    frames, client = asyncio.run(drain({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}, rot, script))
    check("内联 6004 也继续降级到 glm-5.1",
          client.calls == [("a1", "glm-5.3"), ("a1", "glm-5.2"), ("a1", "glm-5.1")],
          f"calls={client.calls}")
    check("正文来自 glm-5.1", sse_texts(frames) == "来自a1/glm-5.1", repr(sse_texts(frames)))
    check("a1 累积 glm-5.3、glm-5.2 两个限流模型",
          models_disabled(rot, "a1") == ["glm-5.3", "glm-5.2"], f"models={models_disabled(rot, 'a1')}")


if __name__ == "__main__":
    for fn in (t1, t2, t3, t4, t5, t6, t7, t8, t9):
        fn()

    print("\n" + "=" * 60)
    print(f"结果：{sum(PASS)}/{len(PASS)} 通过")
    print("=" * 60)
    sys.exit(0 if all(PASS) else 1)
