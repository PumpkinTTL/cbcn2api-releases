"""切号核心概念回归测试 —— 打真实 sqlite，不 stub 存储层。

与 test_failover.py 的分工：
  test_failover.py    验证代理请求链路（超时、故障转移、防重发）
  本文件              验证调度核心语义（粘性、冷却分级、阈值换号、持久化）

真实成分：store.py 全部（真实 schema / upsert / settings / 冷却持久化）、
quota.py 的 calc_totals、TokenRotator 全部。账号的 quota_raw 按腾讯计费
接口的真实嵌套结构构造。

每个循环都有硬上限，任何一处超过上限即判定为死循环并失败。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)

# 把 DB 指到临时目录，绝不碰用户真实的 ~/.cbcn2api/accounts.db
_TMP = tempfile.mkdtemp(prefix="cbcn2api_test_")
os.environ["HOME"] = _TMP
Path.home = staticmethod(lambda: Path(_TMP))

from src.storage import store
store.DB_DIR = Path(_TMP) / ".cbcn2api"
store.DB_PATH = store.DB_DIR / "accounts.db"

from src.models.account import Account
from src.proxy.token_rotator import TokenRotator, QUOTA_COOLDOWN, AUTH_COOLDOWN, TRANSIENT_COOLDOWN
from src.api.quota import calc_totals, PKG_ACTIVITY

PKG_PRO_MON = "TCACA_code_002_AkiJS3ZHF5"
PLATFORM = "workbuddy"
LOOP_CAP = 200          # 任何循环超过这个次数就认为跑飞了

_results = []
def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


def quota_raw(total, remain):
    """腾讯 get-user-resource 的真实嵌套形状。"""
    return {
        "userResource": {
            "data": {"Response": {"Data": {"Accounts": [{
                "PackageCode": PKG_PRO_MON,
                "PackageName": "Pro 月度套餐",
                "Status": 0,
                "CycleCapacitySizePrecise": float(total),
                "CycleCapacityRemainPrecise": float(remain),
                "CycleStartTime": "2026-07-01 00:00:00",
                "CycleEndTime": "2026-07-31 23:59:59",
            }]}}}
        }
    }


def seed(rows):
    """rows: [(id, nickname, total, remain, status)] 写入真实库"""
    if store.DB_PATH.exists():
        store.DB_PATH.unlink()
    for aid, nick, total, remain, status in rows:
        store.upsert_account(PLATFORM, Account(
            id=aid, email=f"{aid}@t.com", uid=aid, nickname=nick,
            access_token=f"tok_{aid}", refresh_token=f"rt_{aid}",
            status=status, quota_raw=quota_raw(total, remain),
            created_at=Account.now_ts(), last_used=Account.now_ts(),
        ))
    store.save_setting("cooldowns", "{}")
    store.save_setting("priority_account", "")


def fresh(rows, threshold=0.0):
    seed(rows)
    store.save_setting("quota_threshold", str(threshold))
    r = TokenRotator()
    r.reload(PLATFORM)
    return r


# ============================================================ 核心概念 1
def t_sticky():
    print("\n[核心1] 粘性锁定：同一个号连续复用，不是每次请求轮转")
    r = fresh([("s1", "号1", 1000, 1000, "normal"),
               ("s2", "号2", 1000, 1000, "normal"),
               ("s3", "号3", 1000, 1000, "normal")])
    picks = [r.get_next(PLATFORM).id for _ in range(20)]
    check("20 次取号全是同一个", len(set(picks)) == 1, f"取到 {set(picks)}")
    check("锁定的号被记录在 _current_id", r._current_id == picks[0])

    # 该号进冷却后，才切走
    locked = picks[0]
    r.mark_disabled(locked, "quota")
    nxt = r.get_next(PLATFORM)
    check("冷却后切到别的号", nxt.id != locked, f"{locked} → {nxt.id}")
    picks2 = [r.get_next(PLATFORM).id for _ in range(10)]
    check("切过去之后重新粘住新号", set(picks2) == {nxt.id}, f"{set(picks2)}")


# ============================================================ 核心概念 2
def t_cooldown_tiers():
    print("\n[核心2] 冷却分级：quota 1h / auth 10min / transient 1min")
    r = fresh([("c1", "号1", 1000, 1000, "normal"),
               ("c2", "号2", 1000, 1000, "normal")])
    now = time.time()
    for reason, expect in (("quota", QUOTA_COOLDOWN),
                           ("auth", AUTH_COOLDOWN),
                           ("transient", TRANSIENT_COOLDOWN)):
        r.mark_disabled("c1", reason)
        got = r._disabled["c1"]["until"] - now
        check(f"{reason} 冷却 ≈ {expect}s", abs(got - expect) < 5, f"实际 {got:.0f}s")
    check("冷却常量未被改动",
          (QUOTA_COOLDOWN, AUTH_COOLDOWN, TRANSIENT_COOLDOWN) == (3600, 600, 60),
          f"{QUOTA_COOLDOWN}/{AUTH_COOLDOWN}/{TRANSIENT_COOLDOWN}")


# ============================================================ 核心概念 3
def t_cooldown_expiry():
    print("\n[核心3] 冷却到期后账号自动回池")
    r = fresh([("x1", "号1", 1000, 1000, "normal")])
    r.mark_disabled("x1", "transient")
    check("冷却中不可取", r.get_next(PLATFORM) is None)
    # 手动把 until 拨到过去，模拟到期
    r._disabled["x1"]["until"] = time.time() - 1
    acc = r.get_next(PLATFORM)
    check("到期后重新可取", acc is not None and acc.id == "x1")
    check("到期记录被清除", "x1" not in r._disabled)


# ============================================================ 核心概念 4
def t_cooldown_persist():
    print("\n[核心4] 冷却持久化：重启后仍然生效（不会把刚 429 的号又拿去用）")
    r = fresh([("p1", "号1", 1000, 1000, "normal"),
               ("p2", "号2", 1000, 1000, "normal")])
    r.get_next(PLATFORM)
    r.mark_disabled("p1", "quota")
    saved = json.loads(store.get_setting("cooldowns", "{}"))
    check("冷却已写入 settings 表", "p1" in saved, f"库里={list(saved)}")

    r2 = TokenRotator()          # 模拟重启
    r2.reload(PLATFORM)
    check("新实例恢复了冷却", "p1" in r2._disabled, f"恢复={list(r2._disabled)}")
    check("重启后不会取到冷却中的号", r2.get_next(PLATFORM).id == "p2")

    # 过期的冷却不应被恢复
    store.save_setting("cooldowns", json.dumps(
        {"p1": {"reason": "quota", "until": time.time() - 10}}))
    r3 = TokenRotator(); r3.reload(PLATFORM)
    check("过期冷却不恢复", "p1" not in r3._disabled)


# ============================================================ 核心概念 5
def t_permanent_exclusion():
    print("\n[核心5] banned / disabled / token 过期 永久排除")
    r = fresh([("n1", "正常", 1000, 1000, "normal"),
               ("n2", "封禁", 1000, 1000, "banned"),
               ("n3", "禁用", 1000, 1000, "disabled")])
    check("count_usable 只算 normal（3 个号里只有 1 个可用）",
          r.count_usable() == 1, f"count_usable={r.count_usable()}, count={r.count()}")
    ids = set()
    for _ in range(LOOP_CAP):
        a = r.get_next(PLATFORM)
        if a is None: break
        ids.add(a.id)
        r.mark_disabled(a.id, "transient")   # 逐个排掉，看总共能取到谁
    check("只取到 normal 的号", ids == {"n1"}, f"取到 {ids}")

    # token 过期
    r2 = fresh([("e1", "过期", 1000, 1000, "normal")])
    r2._accounts[0].expires_at = int(time.time()) - 100
    check("token 过期的号不可取", r2.get_next(PLATFORM) is None)


# ============================================================ 核心概念 6
def t_threshold_switch():
    print("\n[核心6] 阈值换号：额度不足时持久化禁用并切到额度充足的备选")
    # 真实额度：t1 剩 100，t2 剩 5000，阈值 80
    r = fresh([("t1", "低额度", 1000, 100, "normal"),
               ("t2", "高额度", 6000, 5000, "normal")], threshold=80.0)
    check("从库里算出的估算额度正确",
          r._estimated_remain == {"t1": 100.0, "t2": 5000.0},
          f"{r._estimated_remain}")
    r._current_id = "t1"
    r.deduct_quota("t1", 30)      # 100-30=70 < 80
    check("低于阈值后切到 t2", r._current_id == "t2", f"current={r._current_id}")

    db = store.load_account(PLATFORM, "t1")
    check("t1 的 disabled 已落库（不是只改内存）", db.status == "disabled",
          f"库里 status={db.status}")
    check("切换事件被记录", r._threshold_switch is not None, f"{r._threshold_switch}")


# ============================================================ 核心概念 7
def t_no_thrash():
    print("\n[核心7] 阈值换号不震荡：切换次数不超过账号数（死循环防线）")
    rows = [(f"w{i}", f"号{i}", 1000, 200, "normal") for i in range(1, 6)]
    r = fresh(rows, threshold=150.0)
    r._current_id = "w1"

    switches, steps = 0, 0
    seen_order = []
    while steps < LOOP_CAP:
        steps += 1
        cur = r._current_id
        if cur is None:
            break
        before = cur
        r.deduct_quota(cur, 20)
        if r._current_id != before:
            switches += 1
            seen_order.append((before, r._current_id))
        # 全部低于阈值后应稳定下来，不再切
        if steps > 60:
            break
    print(f"       切换轨迹: {' → '.join(a for a, b in seen_order) or '(无)'}"
          f"{' → ' + seen_order[-1][1] if seen_order else ''}")
    check("步数没有跑飞", steps < LOOP_CAP, f"steps={steps}")
    check(f"切换次数 ≤ 账号数({len(rows)})", switches <= len(rows), f"switches={switches}")
    froms = [a for a, b in seen_order]
    check("没有同一个号被换下两次（A→B→A 震荡）",
          len(froms) == len(set(froms)), f"换下顺序={froms}")
    disabled_in_db = [a.id for a in store.list_accounts(PLATFORM) if a.status == "disabled"]
    check("被换下的号都落库为 disabled", set(disabled_in_db) == set(froms),
          f"库里 disabled={disabled_in_db}")


# ============================================================ 核心概念 8
def t_no_fallback():
    print("\n[核心8] 无额度充足备选时：不切号、不禁用，只提示一次")
    r = fresh([("f1", "当前", 1000, 100, "normal"),
               ("f2", "也不足", 1000, 50, "normal")], threshold=80.0)
    r._current_id = "f1"
    for _ in range(10):
        r.deduct_quota("f1", 5)
    check("没有切号", r._current_id == "f1", f"current={r._current_id}")
    db = store.load_account(PLATFORM, "f1")
    check("f1 没被误禁用", db.status == "normal", f"status={db.status}")
    check("提示带 __nofallback__ 前缀",
          (r._threshold_switch or "").startswith("__nofallback__"),
          f"{r._threshold_switch}")


# ============================================================ 核心概念 9
def t_priority():
    print("\n[核心9] 手动优先账号：持久化且重启后恢复")
    r = fresh([("q1", "号1", 1000, 1000, "normal"),
               ("q2", "号2", 1000, 1000, "normal"),
               ("q3", "号3", 1000, 1000, "normal")])
    r.set_priority("q3")
    check("立即生效", r.get_next(PLATFORM).id == "q3")
    store.save_setting("priority_account", "q3")
    r2 = TokenRotator(); r2.reload(PLATFORM)
    check("重启后恢复优先号", r2.get_next(PLATFORM).id == "q3", f"{r2._current_id}")


# ============================================================ 核心概念 10
def t_exhaustion():
    print("\n[核心10] 全部不可用时立即返回 None（不空转）")
    ids = [f"z{i}" for i in range(1, 4)]
    r = fresh([(i, i, 1000, 1000, "normal") for i in ids])
    for i in ids:
        r.mark_disabled(i, "quota")
    t0 = time.monotonic()
    calls = 0
    for _ in range(LOOP_CAP):
        calls += 1
        if r.get_next(PLATFORM) is None:
            break
    el = time.monotonic() - t0
    check("第一次调用就返回 None", calls == 1, f"calls={calls}")
    check("耗时可忽略（无空转）", el < 1.0, f"{el:.3f}s")
    check("count_usable 归零", r.count_usable() == 0, f"{r.count_usable()}")
    check("count 仍能看到总数", r.count() == 3, f"{r.count()}")


# ============================================================ 核心概念 11
def t_quota_raw_roundtrip():
    """store.py 的 INSERT 只覆盖部分列，曾经悄悄丢字段。
    quota_raw 一旦丢失，calc_totals 全返 0，阈值换号会整体失效 ——
    所以这个字段的落库往返必须锁死。"""
    print("\n[核心11] quota_raw 落库往返：丢了阈值换号会静默失效")
    seed([("r1", "号1", 1234.5, 678.9, "normal")])
    db = store.load_account(PLATFORM, "r1")
    check("quota_raw 从库里读回来不是 None", db.quota_raw is not None)
    from src.api.quota import calc_totals
    total, used = calc_totals(db.quota_raw, db.usage_raw)
    check("往返后额度算得出来", (total, used) == (1234.5, 1234.5 - 678.9),
          f"total={total}, used={used}")
    r = TokenRotator(); r.reload(PLATFORM)
    check("rotator 估算值 = 库里的剩余额度",
          abs(r._estimated_remain["r1"] - 678.9) < 0.01,
          f"{r._estimated_remain}")
    for f in ("access_token", "refresh_token", "status", "nickname", "email"):
        check(f"字段 {f} 未丢失", getattr(db, f), f"{f}={getattr(db, f)!r}")


# ============================================================ 核心概念 12
def t_dirty_quota_isolation():
    """配额接口报错时 quota_raw 可能是 {"userResource": {"data": null}}，
    calc_totals 会抛 AttributeError。修复前整个 _refresh_estimates 裸奔，
    坏账号之后的所有账号都拿不到估算值 → 永不扣减、永不触发阈值。"""
    print("\n[核心12] 脏 quota_raw 不污染其他账号（打真实库）")
    seed([("d1", "正常前", 1000, 900, "normal"),
          ("d2", "脏数据", 1000, 900, "normal"),
          ("d3", "正常后", 1000, 800, "normal")])
    # 直接写入真实库：模拟配额接口返回 data:null
    bad = store.load_account(PLATFORM, "d2")
    bad.quota_raw = {"userResource": {"data": None}}
    store.upsert_account(PLATFORM, bad)
    reread = store.load_account(PLATFORM, "d2")
    check("脏数据确实存进库了", reread.quota_raw == {"userResource": {"data": None}},
          f"{reread.quota_raw}")

    r = TokenRotator()
    r.reload(PLATFORM)          # 不应抛异常
    check("reload 没有因脏数据崩掉", True)
    check("三个账号都拿到了估算值",
          set(r._estimated_remain) == {"d1", "d2", "d3"},
          f"{r._estimated_remain}")
    check("脏账号兜底为 0", r._estimated_remain["d2"] == 0.0)
    check("脏账号之后的 d3 估算值正常（旧代码这里会缺失）",
          r._estimated_remain["d3"] == 800.0, f"d3={r._estimated_remain.get('d3')}")

    # 缺失估算值时 deduct_quota 必须兜底而不是 return
    r2 = TokenRotator(); r2.reload(PLATFORM)
    r2._estimated_remain.pop("d1", None)
    r2._threshold = 1.0
    r2.deduct_quota("d1", 10)
    check("缺失 key 时按 0 兜底而非早退", "d1" in r2._estimated_remain,
          f"{r2._estimated_remain}")


# ============================================================ 核心概念 13
def t_concurrent_safety():
    """网关多请求并发 + GUI 同时读写，调度状态不能错乱、不能卡死。"""
    print("\n[核心13] 并发下调度状态不错乱、不卡死")
    import threading
    r = fresh([(f"m{i}", f"号{i}", 10000, 10000, "normal") for i in range(1, 4)])
    r._threshold = 0.0
    errors = []
    def worker(n):
        try:
            for _ in range(200):
                a = r.get_next(PLATFORM)
                if a: r.deduct_quota(a.id, 1)
                r.count_usable()
                r.status()
        except Exception as e:
            errors.append(repr(e))
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    t0 = time.monotonic()
    for t in ths: t.start()
    for t in ths: t.join(timeout=30)
    el = time.monotonic() - t0
    check("没有线程仍在运行（未死锁）", not any(t.is_alive() for t in ths), f"{el:.2f}s")
    check("无异常抛出", not errors, str(errors[:3]))
    check("扣减总量正确（6 线程 × 200 次 = 1200）",
          abs(sum(10000 - v for v in r._estimated_remain.values()) - 1200) < 1,
          f"已扣 {sum(10000 - v for v in r._estimated_remain.values())}")
    check("耗时正常（无 5s sqlite 阻塞叠加）", el < 20, f"{el:.2f}s")


# ============================================================ 核心概念 14
def t_stale_zero_estimate():
    """修复：DB 持久化的旧估算 0（旧版 Status=3 误计入 used 算成 0），
    reload 时若 DB 快照能算出正额度必须信任 fresh —— 否则 min(0, fresh)=0，
    账号「明明还有额度」，一旦开阈值第一笔请求就触发切号。"""
    print("\n[核心14] 陈旧 0 估算不被 min 卡死（提前切号根因回归）")
    seed([("v1", "旧0", 1000, 800, "normal"),
          ("v2", "正常", 1000, 800, "normal")])
    # 模拟旧版错误压低：estimated_remain 持久化 v1=0
    store.save_setting("estimated_remain", json.dumps({"v1": 0.0, "v2": 800.0}))
    r = TokenRotator(); r.reload(PLATFORM)
    check("旧 0 值被真实额度覆盖（不是 min 卡死）", r._estimated_remain["v1"] == 800.0,
          f"v1={r._estimated_remain.get('v1')}")
    check("正常值保持", r._estimated_remain["v2"] == 800.0, f"v2={r._estimated_remain.get('v2')}")

    # 运行期真实扣减不能被 reload 的陈旧快照抬高（min 防线仍然有效）
    r._estimated_remain["v2"] = 700.0          # 内存已扣到 700
    r.reload(PLATFORM)                          # DB 快照还是 800
    check("运行期扣减后 reload 不被快照抬高", r._estimated_remain["v2"] == 700.0,
          f"v2={r._estimated_remain.get('v2')}")


# ============================================================ 核心概念 15
def t_switch_log_written():
    """修复：mark_disabled 会把 _current_id 置 None，get_next 拿不到旧号；
    现在用 _pending_switch_from 暂存，确保异常/封号切号一定写日志。"""
    print("\n[核心15] 异常/封号切号日志完整（from→to+原因）")
    r = fresh([("l1", "号1", 1000, 1000, "normal"),
               ("l2", "号2", 1000, 1000, "normal")])
    r._current_id = "l1"
    r.get_next(PLATFORM)                 # 锁住 l1
    r.mark_disabled("l1", "quota")       # 429 额度耗尽 → 置空 current
    nxt = r.get_next(PLATFORM)           # 换号到这里才会写日志
    check("换到 l2", nxt and nxt.id == "l2", f"nxt={nxt and nxt.id}")
    conn = store._get_conn()
    try:
        sw = [dict(x) for x in conn.execute(
            "SELECT * FROM proxy_logs WHERE event='switch' ORDER BY id").fetchall()]
    finally:
        conn.close()
    check("写了一条 switch 日志", len(sw) == 1, f"count={len(sw)}")
    # message 展示昵称（"号1 → 号2"）；to 账号的目标 id 在 message 里出现即证明切向 l2
    check("日志 from=l1 → 显示切向 l2", sw and sw[0]["account_id"] == "l1"
          and "号1" in sw[0]["message"] and "号2" in sw[0]["message"],
          str(sw[0] if sw else None))
    check("日志带原因（额度耗尽）", sw and "额度" in (sw[0]["details"] or ""),
          f"details={sw[0].get('details') if sw else None}")

    # 正常请求未切号 → 不写日志
    conn = store._get_conn()
    try:
        before = conn.execute("SELECT COUNT(*) c FROM proxy_logs WHERE event='switch'").fetchone()["c"]
    finally:
        conn.close()
    r.deduct_quota("l2", 1)              # 未到阈值（threshold=0）
    conn = store._get_conn()
    try:
        after = conn.execute("SELECT COUNT(*) c FROM proxy_logs WHERE event='switch'").fetchone()["c"]
    finally:
        conn.close()
    check("未切号不写日志", before == after, f"{before} → {after}")


# ============================================================ 核心概念 16
def t_multipackage_totals():
    """修复：账号裂变包会持续累积，超过单页 100 条时上游分页。
    这里直接构造 150 个包（110 个 Status=3 耗尽 + 40 个 Status=0 有效）。
    验证两种口径：
      - 调度口径 calc_totals()（active_only=True，默认）：只算 Status=0 —— 决定切号
      - 展示口径 calc_totals(active_only=False)：算全量 0+3 —— 给用户看总额度"""
    print("\n[核心16] 多套餐包汇算：调度只算有效 / 展示算全量，不被数量压垮")
    pkgs = []
    # 110 个已耗尽裂变包（Status=3，remain=0）
    for i in range(110):
        pkgs.append({"PackageCode": PKG_ACTIVITY, "Status": 3,
                     "CycleCapacitySizePrecise": 100.0, "CycleCapacityRemainPrecise": 0.0})
    # 40 个有效裂变包（Status=0，各剩 50）
    for i in range(40):
        pkgs.append({"PackageCode": PKG_ACTIVITY, "Status": 0,
                     "CycleCapacitySizePrecise": 100.0, "CycleCapacityRemainPrecise": 50.0})
    qr = {"userResource": {"data": {"Response": {"Data": {"Accounts": pkgs}}}}}

    # 调度口径：只算 Status=0
    total, used = calc_totals(qr)
    check("调度 total 只算 Status=0", abs(total - 40 * 100.0) < 0.01, f"total={total}")
    check("调度 remain 只算 Status=0", abs((total - used) - 40 * 50.0) < 0.01,
          f"remain={total-used}")

    # 展示口径：全量 0+3（已耗尽的也算进总额度）
    total_all, used_all = calc_totals(qr, active_only=False)
    expect_total_all = (110 + 40) * 100.0
    expect_remain_all = 40 * 50.0   # Status=3 的 remain=0 不贡献剩余
    check("展示 total 含已耗尽包", abs(total_all - expect_total_all) < 0.01,
          f"total_all={total_all} 期望={expect_total_all}")
    check("展示 remain 全量正确", abs((total_all - used_all) - expect_remain_all) < 0.01,
          f"remain_all={total_all-used_all} 期望={expect_remain_all}")


# ============================================================ 核心概念 17
def t_calibrate_no_inflate():
    """修复：网关运行中用户点「刷新额度」会 reload(calibrate=True)。
    若上游结算延迟，快照 fresh > 内存已扣减值 prev，直接覆盖会把估算抬高 → 超用。
    现在 calibrate 也取 min(fresh, prev)（prev>0 时），防抬高。
    prev==0（坏值）仍信任 fresh 校准。"""
    print("\n[核心17] calibrate 不被结算延迟的快照抬高（防超用回归）")
    seed([("c1", "运行中", 1000, 800, "normal")])
    r = TokenRotator(); r.reload(PLATFORM)
    # 运行期已扣 100：内存 700
    r._estimated_remain["c1"] = 700.0
    # 用户点刷新：DB 快照还是 800（上游未结算），calibrate 不能抬高回 800
    r.reload(PLATFORM, calibrate=True)
    check("calibrate 不抬高运行期扣减", r._estimated_remain["c1"] == 700.0,
          f"c1={r._estimated_remain.get('c1')}")
    # 但 prev==0（坏值）时 calibrate 要信任 fresh 校准
    r._estimated_remain["c1"] = 0.0
    r.reload(PLATFORM, calibrate=True)
    check("calibrate 校准 prev==0 坏值", r._estimated_remain["c1"] == 800.0,
          f"c1={r._estimated_remain.get('c1')}")


for fn in (t_sticky, t_cooldown_tiers, t_cooldown_expiry, t_cooldown_persist,
           t_permanent_exclusion, t_threshold_switch, t_no_thrash,
           t_no_fallback, t_priority, t_exhaustion,
           t_quota_raw_roundtrip, t_dirty_quota_isolation, t_concurrent_safety,
           t_stale_zero_estimate, t_switch_log_written,
           t_multipackage_totals, t_calibrate_no_inflate):
    fn()

ok = sum(1 for _, c in _results if c)
print("\n" + "=" * 64)
print(f"结果：{ok}/{len(_results)} 通过     临时库：{store.DB_PATH}")
for n, c in _results:
    if not c:
        print(f"  未通过 → {n}")
print("=" * 64)
sys.exit(0 if ok == len(_results) else 1)
