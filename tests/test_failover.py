"""用假上游驱动真实的 _stream_inner，验证故障转移与防死循环改动。

只 stub 外部依赖（httpx / fastapi / sqlite store / 配额解析），
_stream_inner、_first_event_kind、_normalize_text_stream、TokenRotator
全部是仓库里的真实代码。
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
    """按脚本回放上游行为。"""
    def __init__(self, status_code=200, body="", chunks=None, raise_after=None, heartbeat=False):
        self.status_code = status_code
        self._body = body
        self._chunks = chunks or []
        self._raise_after = raise_after   # 吐完第 N 块后抛超时
        self._heartbeat = heartbeat
        self.closed = False

    async def aread(self):
        return self._body.encode()

    async def aclose(self):
        self.closed = True

    async def aiter_text(self, chunk_size=8192):
        if self._heartbeat:
            while True:                       # 只发心跳，永不结束
                yield ": ping\n"
                await asyncio.sleep(0.005)
        for i, c in enumerate(self._chunks):
            if self._raise_after is not None and i == self._raise_after:
                raise _TimeoutException("upstream stalled")
            yield c
            await asyncio.sleep(0)

class FakeClient:
    def __init__(self, script):
        self.script = script      # account_id -> FakeResponse | Exception
        self.calls = []
    def build_request(self, method, url, json=None, headers=None):
        return types.SimpleNamespace(url=url, headers=headers)
    async def send(self, request, stream=True):
        aid = request.headers.get("__acct__")
        self.calls.append(aid)
        item = self.script[aid]
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
UPSERTS = []
SETTINGS = {}
store_stub = types.ModuleType("src.storage.store")
store_stub.add_log = lambda *a, **k: LOGS.append(a)
store_stub.update_account_stats = lambda *a, **k: None
store_stub.list_accounts = lambda p: []
store_stub.get_setting = lambda k, d="": SETTINGS.get(k, d)
store_stub.save_setting = lambda k, v: SETTINGS.__setitem__(k, v)
store_stub.upsert_account = lambda p, a: UPSERTS.append(a.id)
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
def tagged_headers(token, uid, conv):
    h = dict(_orig_build_headers(token, uid, conv))
    h["__acct__"] = token          # token 里塞的是 account id
    return h
ps.build_headers = tagged_headers


def make_rotator(ids):
    r = TokenRotator()
    r._accounts = [Account(id=i, access_token=i, uid=i, nickname=i, status="normal")
                   for i in ids]
    r._estimated_remain = {i: 1000.0 for i in ids}
    r._platform = "wb"
    r._current_id = ids[0]
    return r


async def drain(payload, rot, script):
    ps.token_rotator = rot
    client = FakeClient(script)
    ps._get_http_client = lambda: client
    out = []
    async for piece in ps._stream_inner(payload, None, "wb"):
        out.append(piece)
    return out, client


def sse_texts(frames):
    """从产出帧里还原正文。"""
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


def ok_chunks(text):
    return [
        'data: {"id":"r1","choices":[{"delta":{"role":"assistant"}}]}\n\n',
        'data: {"id":"r1","choices":[{"delta":{"content":"%s"}}]}\n\n' % text,
        'data: {"id":"r1","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"credit":2}}\n\n',
        'data: [DONE]\n\n',
    ]


PASS = []
def check(name, cond, detail=""):
    PASS.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def t1():
    print("\n测试1  429 → 标记冷却 → 切下一个号 → 成功")
    rot = make_rotator(["a1", "a2"])
    script = {
        "a1": FakeResponse(429, body='{"error":{"data":{"code":14018}}}'),
        "a2": FakeResponse(200, chunks=ok_chunks("来自a2")),
    }
    frames, client = asyncio.run(drain({"model": "auto"}, rot, script))
    check("上游被调用两次", client.calls == ["a1", "a2"], f"calls={client.calls}")
    check("a1 已进冷却", "a1" in rot._disabled, f"disabled={list(rot._disabled)}")
    check("正文来自 a2", sse_texts(frames) == "来自a2", repr(sse_texts(frames)))
    check("客户端没收到错误帧", errors_of(frames) == [])


def t2():
    print("\n测试2  200 内联错误被切在 chunk 边界（原 bug：检测不到，不换号）")
    err = 'data: {"error":{"message":"quota","data":{"code":14018}}}\n\n'
    mid = len(err) // 2
    rot = make_rotator(["b1", "b2"])
    script = {
        "b1": FakeResponse(200, chunks=[err[:mid], err[mid:]]),   # 故意切两半
        "b2": FakeResponse(200, chunks=ok_chunks("来自b2")),
    }
    frames, client = asyncio.run(drain({"model": "auto"}, rot, script))
    check("跨 chunk 错误被识别并换号", client.calls == ["b1", "b2"], f"calls={client.calls}")
    check("b1 已进冷却", "b1" in rot._disabled)
    check("正文来自 b2", sse_texts(frames) == "来自b2", repr(sse_texts(frames)))
    check("错误体没有透传给客户端", errors_of(frames) == [])


def t3():
    print("\n测试3  全部账号 429 → 有界退出，返回 no_account")
    ids = ["c1", "c2", "c3"]
    rot = make_rotator(ids)
    script = {i: FakeResponse(429, body='{"error":{"data":{"code":14018}}}') for i in ids}
    t0 = time.monotonic()
    frames, client = asyncio.run(drain({"model": "auto"}, rot, script))
    el = time.monotonic() - t0
    errs = errors_of(frames)
    check("每个号只试一次，共 3 次", client.calls == ids, f"calls={client.calls}")
    check("循环有界退出", el < 5, f"{el:.2f}s")
    check("返回 no_account", errs and errs[0].get("type") == "no_account", str(errs))
    check("以 [DONE] 收尾", "[DONE]" in frames[-1])


def t4():
    print("\n测试4  已发出内容后上游超时 → 禁止换号重发（原 bug：内容重复）")
    rot = make_rotator(["d1", "d2"])
    script = {
        # 先正常吐 2 块，第 3 块时抛超时
        "d1": FakeResponse(200, chunks=ok_chunks("前半段"), raise_after=2),
        "d2": FakeResponse(200, chunks=ok_chunks("完整回答")),
    }
    frames, client = asyncio.run(drain({"model": "auto"}, rot, script))
    body = sse_texts(frames)
    check("没有换号重发", client.calls == ["d1"], f"calls={client.calls}")
    check("正文只出现一次，无拼接", body == "前半段", repr(body))
    check("d2 的内容没混进来", "完整回答" not in body)
    errs = errors_of(frames)
    check("补了超时错误帧", errs and errs[0].get("type") == "upstream_timeout", str(errs))
    check("以 [DONE] 收尾", "[DONE]" in frames[-1])


def t5():
    print("\n测试5  上游只发心跳 → peek 超时放行，不永久挂起")
    orig_peek, orig_dl = ps.PEEK_TIMEOUT, ps.STREAM_DEADLINE
    ps.PEEK_TIMEOUT, ps.STREAM_DEADLINE = 0.3, 1.0
    try:
        rot = make_rotator(["e1"])
        script = {"e1": FakeResponse(200, heartbeat=True)}
        t0 = time.monotonic()
        frames, client = asyncio.run(drain({"model": "auto"}, rot, script))
        el = time.monotonic() - t0
        check("有界返回，未永久挂起", el < 4, f"{el:.2f}s")
        check("以 [DONE] 收尾", any("[DONE]" in f for f in frames))
    finally:
        ps.PEEK_TIMEOUT, ps.STREAM_DEADLINE = orig_peek, orig_dl


def t6():
    print("\n测试6  连接失败 → 换号（原 bug：break 放弃整轮）")
    rot = make_rotator(["f1", "f2"])
    script = {
        "f1": _ConnectError("connection refused"),
        "f2": FakeResponse(200, chunks=ok_chunks("来自f2")),
    }
    frames, client = asyncio.run(drain({"model": "auto"}, rot, script))
    check("连不上就换号，不放弃", client.calls == ["f1", "f2"], f"calls={client.calls}")
    check("正文来自 f2", sse_texts(frames) == "来自f2", repr(sse_texts(frames)))


def t7():
    print("\n测试7  额度扣减与阈值换号在真实请求路径上生效")
    rot = make_rotator(["g1", "g2"])
    rot._threshold = 999.0                    # 一次请求后必然低于阈值
    rot._estimated_remain = {"g1": 1000.0, "g2": 1000.0}
    script = {"g1": FakeResponse(200, chunks=ok_chunks("hi")),
              "g2": FakeResponse(200, chunks=ok_chunks("hi"))}
    frames, client = asyncio.run(drain({"model": "auto"}, rot, script))
    check("g1 扣减了 usage.credit=2", rot._estimated_remain["g1"] == 998.0,
          f"remain={rot._estimated_remain}")
    check("低于阈值后切到 g2", rot._current_id == "g2", f"current={rot._current_id}")
    check("g1 被持久化禁用", "g1" in UPSERTS, f"upserts={UPSERTS}")


for fn in (t1, t2, t3, t4, t5, t6, t7):
    fn()

print("\n" + "=" * 60)
print(f"结果：{sum(PASS)}/{len(PASS)} 通过")
print("=" * 60)
sys.exit(0 if all(PASS) else 1)
