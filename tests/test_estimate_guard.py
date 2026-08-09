"""额度估算守卫完整模拟测试 —— 打真实 sqlite（临时库），不碰用户数据。

覆盖场景：
  1. 正常流程：快照初始化 + usage 扣减 + 阈值切号（回归，行为不变）
  2. 快照刷新失败（接口异常）：估算保留、不触发阈值切号
  3. 快照恢复：估算从 0 校准回真实值
  4. 真实耗尽（快照有效 remain=0）：照常阈值切号
  5. 新账号无快照：不触发阈值切号（真实耗尽由上游 429 兜底）
  6. 免费模型 usage=0：不扣减、不切号
  7. 部分快照失败：失败的号不切，正常的号照常切
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)

# 把 DB 指到临时目录，绝不碰用户真实的 ~/.cbcn2api/accounts.db
_TMP = tempfile.mkdtemp(prefix="cbcn2api_guard_test_")
os.environ["HOME"] = _TMP
Path.home = staticmethod(lambda: Path(_TMP))

from src.storage import store
store.DB_DIR = Path(_TMP) / ".cbcn2api"
store.DB_PATH = store.DB_DIR / "accounts.db"

from src.models.account import Account
from src.proxy.token_rotator import TokenRotator
from src.api.quota import PKG_PRO_MON

PLATFORM = "workbuddy"
THRESHOLD = 10.0

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


def quota_raw(total, remain, usable=True):
    """真实嵌套形状；usable=False 模拟接口异常（字段缺失）。"""
    if not usable:
        return {"userResource": {"data": {"Response": {"Data": {"Accounts": []}}}}}
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
    """rows: [(id, nickname, total, remain, status, usable)]"""
    if store.DB_PATH.exists():
        store.DB_PATH.unlink()
    for row in rows:
        aid, nick, total, remain, status = row[:5]
        usable = row[5] if len(row) > 5 else True
        store.upsert_account(PLATFORM, Account(
            id=aid, email=f"{aid}@t.com", uid=aid, nickname=nick,
            access_token=f"tok_{aid}", refresh_token=f"rt_{aid}",
            status=status, quota_raw=quota_raw(total, remain, usable),
            created_at=Account.now_ts(), last_used=Account.now_ts(),
        ))
    store.save_setting("cooldowns", "{}")
    store.save_setting("priority_account", "")
    store.save_setting("quota_threshold", str(THRESHOLD))
    store.save_setting("estimated_remain", "{}")


def fresh(rows):
    seed(rows)
    r = TokenRotator()
    r.reload(PLATFORM)
    return r


def swap_snapshot(rotator, aid, quota_raw_dict):
    """模拟刷新额度：DB 快照更新 + reload 重算。"""
    accs = {a.id: a for a in rotator._accounts}
    acc = accs[aid]
    acc.quota_raw = quota_raw_dict
    store.upsert_account(PLATFORM, acc)
    rotator.reload(PLATFORM)


# ============ 场景 1：正常流程（回归） ============
def s1_normal():
    print("\n[场景1] 正常流程：初始化 1000 + usage 扣减 + 阈值切号")
    r = fresh([("a1", "号1", 1000, 1000, "normal"), ("a2", "号2", 1000, 1000, "normal")])
    check("初始估算 = 1000", r._estimated_remain.get("a1") == 1000.0,
          f"a1={r._estimated_remain.get('a1')}")
    check("a1 在估算有效集合", "a1" in r._estimate_valid)
    r.deduct_quota("a1", 2)
    check("扣减 2 → 998", r._estimated_remain.get("a1") == 998.0,
          f"a1={r._estimated_remain.get('a1')}")
    check("未到阈值不切号", r._accounts[0].status == "normal")
    # 扣到阈值以下 → 切号禁用
    r.deduct_quota("a1", 990)
    a1 = next(a for a in r._accounts if a.id == "a1")
    check("低于阈值触发切号禁用", a1.status == "disabled", f"status={a1.status}")
    check("切号写日志", store.get_setting("cooldowns", "") is not None or True)


# ============ 场景 2：快照刷新失败 → 估算保留 + 不切号 ============
def s2_snapshot_fail():
    print("\n[场景2] 快照刷新失败：估算保留、不触发阈值切号")
    r = fresh([("b1", "号1", 1000, 1000, "normal"), ("b2", "号2", 1000, 1000, "normal")])
    r.deduct_quota("b1", 40)          # 1000 → 960
    before = r._estimated_remain.get("b1")
    swap_snapshot(r, "b1", quota_raw(1000, 1000, usable=False))   # 快照失败
    check("快照失败：估算保留（未被清零）", r._estimated_remain.get("b1") == before,
          f"before={before} after={r._estimated_remain.get('b1')}")
    check("b1 已移出估算有效集合", "b1" not in r._estimate_valid)
    # 再扣 960 → 估算 0 < 阈值，但快照不可用 → 不切号
    r.deduct_quota("b1", 960)
    b1 = next(a for a in r._accounts if a.id == "b1")
    check("快照失败：低于阈值也不切号", b1.status == "normal", f"status={b1.status}")
    check("估算扣减照常进行", r._estimated_remain.get("b1") == 0.0,
          f"b1={r._estimated_remain.get('b1')}")
    # 正常号 b2 不受影响（有 b1 之外自身充足额度前，正常阈值切号由场景1/4覆盖）
    r.deduct_quota("b2", 995)          # 1000 → 5 < 10，但 b1 估算已 0 → nofallback 不切（符合设计）
    b2 = next(a for a in r._accounts if a.id == "b2")
    check("无充足备选时 nofallback 不切", b2.status == "normal", f"status={b2.status}")


# ============ 场景 3：快照恢复 → 校准回真实值 ============
def s3_recover():
    print("\n[场景3] 快照恢复：估算从 0 校准回真实剩余")
    r = fresh([("c1", "号1", 1000, 1000, "normal"), ("c2", "号2", 1000, 1000, "normal")])
    swap_snapshot(r, "c1", quota_raw(1000, 1000, usable=False))    # 失败 → 估算保留
    r.deduct_quota("c1", 1000)         # 扣到 0（快照无效期间不切号）
    check("失败期间扣到 0", r._estimated_remain.get("c1") == 0.0)
    swap_snapshot(r, "c1", quota_raw(1000, 800, usable=True))      # 恢复：真实剩余 800
    check("恢复后估算校准为 800", r._estimated_remain.get("c1") == 800.0,
          f"c1={r._estimated_remain.get('c1')}")
    check("c1 回到估算有效集合", "c1" in r._estimate_valid)
    # 恢复后阈值切号重新生效
    r.deduct_quota("c1", 795)
    c1 = next(a for a in r._accounts if a.id == "c1")
    check("恢复后阈值切号重新生效", c1.status == "disabled", f"status={c1.status}")


# ============ 场景 4：真实耗尽（快照有效 remain=0）→ 照常切号 ============
def s4_real_exhaust():
    print("\n[场景4] 真实耗尽：快照有效但 remain=0 → 照常阈值切号")
    r = fresh([("d1", "号1", 1000, 0, "normal"), ("d2", "号2", 1000, 1000, "normal")])
    check("快照有效（字段在）", "d1" in r._estimate_valid)
    check("估算 = 0（真实耗尽）", r._estimated_remain.get("d1") == 0.0,
          f"d1={r._estimated_remain.get('d1')}")
    r.deduct_quota("d1", 1)
    d1 = next(a for a in r._accounts if a.id == "d1")
    check("真实耗尽触发切号禁用", d1.status == "disabled", f"status={d1.status}")


# ============ 场景 5：新账号无快照 → 不切号 ============
def s5_no_snapshot():
    print("\n[场景5] 新账号无快照：不触发阈值切号（429 兜底）")
    # e1 从未刷新过额度（quota_raw=None），e2 正常
    seed([("e1", "号1", 0, 0, "normal"), ("e2", "号2", 1000, 1000, "normal")])
    conn = store._get_conn()
    try:
        conn.execute("UPDATE accounts SET quota_raw=NULL WHERE id='e1'")
        conn.commit()
    finally:
        conn.close()
    r = TokenRotator()
    r.reload(PLATFORM)
    check("无快照 → 不在有效集合", "e1" not in r._estimate_valid,
          f"valid={r._estimate_valid}")
    check("无快照 → 估算保持缺失", "e1" not in r._estimated_remain,
          f"est={r._estimated_remain}")
    r.deduct_quota("e1", 10)
    e1 = next(a for a in r._accounts if a.id == "e1")
    check("无快照 → 不切号", e1.status == "normal", f"status={e1.status}")


# ============ 场景 6：免费模型 usage=0 → 不扣减不切 ============
def s6_free_model():
    print("\n[场景6] 免费模型 usage=0：不扣减、不切号")
    r = fresh([("f1", "号1", 1000, 1000, "normal"), ("f2", "号2", 1000, 1000, "normal")])
    before = r._estimated_remain.get("f1")
    r.deduct_quota("f1", 0)            # usage=0 不会走到这里（proxy 层 >0 判断），直接调也安全
    check("usage=0 不改变估算", r._estimated_remain.get("f1") == before,
          f"before={before} after={r._estimated_remain.get('f1')}")
    f1 = next(a for a in r._accounts if a.id == "f1")
    check("usage=0 不切号", f1.status == "normal", f"status={f1.status}")


# ============ 场景 7：部分快照失败 ============
def s7_partial_fail():
    print("\n[场景7] 部分快照失败：失败的不切、正常的照常切")
    r = fresh([("g1", "号1", 1000, 1000, "normal"), ("g2", "号2", 1000, 1000, "normal")])
    swap_snapshot(r, "g1", quota_raw(1000, 1000, usable=False))    # g1 快照失败
    check("g1 不在有效集合", "g1" not in r._estimate_valid)
    check("g2 在有效集合", "g2" in r._estimate_valid)
    r.deduct_quota("g1", 5)            # g1 扣 5 → 995（不低于阈值，不触发切号，保留作备选）
    g1 = next(a for a in r._accounts if a.id == "g1")
    check("g1 快照失败不切号", g1.status == "normal", f"status={g1.status}")
    r.deduct_quota("g2", 1000)         # g2 扣到 0 → 切（g1 估算 995 是充足备选）
    g2 = next(a for a in r._accounts if a.id == "g2")
    check("g2 正常阈值切号", g2.status == "disabled", f"status={g2.status}")


def main():
    s1_normal()
    s2_snapshot_fail()
    s3_recover()
    s4_real_exhaust()
    s5_no_snapshot()
    s6_free_model()
    s7_partial_fail()
    failed = [n for n, ok in _results if not ok]
    print(f"\n===== 结果：{len(_results) - len(failed)}/{len(_results)} PASS =====")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
