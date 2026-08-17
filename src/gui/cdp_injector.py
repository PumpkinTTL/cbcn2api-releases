"""CDP 注入模块：连 WorkBuddy（9222）/ ZCode（9223）CDP，把网关额度横条注入 AI 对话输入框上方。

横条每 5s fetch 网关 `/__gw/quota` 刷新（消耗通过 account_stats.total_credit 实时反映），
带版本控制（多次注入自动失效旧 JS）、折叠按钮、MutationObserver（视图切换自动重挂）、
主题跟随（WorkBuddy=data-theme/vscode-dark；ZCode=html.dark/theme-zai-dark）。
目标应用未开 CDP 端口时静默返回 ok=False，不抛错。
"""
import json
import socket
import threading

import requests
from websocket import create_connection

# 注入目标：端口 + 深色模式检测（各家标记不同：WorkBuddy 是 vscode 系，ZCode 是 zai 系）
INJECT_TARGETS = {
    9222: "WorkBuddy",
    9223: "ZCode",
}

_LOCK = threading.Lock()

INJECT_JS_TEMPLATE = r"""
(function(){
  window.__gwJSVer = (window.__gwJSVer || 0) + 1;
  const MY_VER = window.__gwJSVer;  // 旧注入的 interval/observer 版本不匹配自动失效
  function findComposer(){
    // 优先按各家已知锚点
    let c = document.querySelector('section.wb-home-composer')
      || document.querySelector('[class*="wb-home-composer"]')
      || document.querySelector('.chat-composer-input-surface')
      || document.querySelector('[class*="Composer"]');
    // 通用精确定位：从编辑器向上找第一个「有边框/圆角」的真实外框
    // （透明容器不算——横条要贴在视觉输入框卡片上，不是外层布局容器上）
    const ed = document.querySelector('[contenteditable="true"]') || document.querySelector('textarea');
    if (ed) {
      let el = ed.parentElement, best = null;
      for (let i = 0; i < 8 && el && el !== document.body; i++) {
        const cs = getComputedStyle(el);
        const hasBorder = parseFloat(cs.borderTopWidth) > 0 || parseFloat(cs.borderLeftWidth) > 0;
        const hasRadius = parseFloat(cs.borderTopLeftRadius) > 0;
        if (hasBorder || (hasRadius && cs.backgroundColor !== 'rgba(0, 0, 0, 0)')) { best = el; break; }
        el = el.parentElement;
      }
      if (best) return best;
    }
    if (c) return c;
    if (!ed) return null;
    return ed.closest('section') || ed.closest('[class*="composer"]') || ed.closest('[class*="Composer"]') || ed.closest('[class*="input-box"]') || ed.parentElement;
  }
  // 主题跟随：WorkBuddy 用 data-theme / vscode-dark class；ZCode 用 html.dark / theme-zai-dark
  function isDark(){
    const h = document.documentElement;
    return h.getAttribute('data-theme') === 'dark'
      || h.classList.contains('vscode-dark') || h.classList.contains('cb-dark')
      || h.classList.contains('dark') || h.classList.contains('theme-zai-dark');
  }
  // 两套配色（bar/expand 共用）：浅色=白底深字，深色=#2b2b2b 底浅字
  function themeCss(){
    const dark = isDark();
    return {
      bg: dark ? '#2b2b2b' : '#fff',
      border: dark ? '#3c3c3c' : '#e4e6eb',
      text: dark ? '#cccccc' : '#1f2329',
      divider: dark ? '#3c3c3c' : '#e4e6eb'
    };
  }
  function restyleBar(){
    const t = themeCss();
    const bar = document.getElementById('gw-quota-bar');
    // 只换文字色与分隔线色；背景保持透明（bar 融入输入框卡片，不涂底色）
    if(bar) bar.style.color = t.text;
    document.querySelectorAll('.gw-q-divider').forEach(s=>{ s.style.background = t.divider; });
  }
  function inject(force){
    if(window.__gwJSVer !== MY_VER) return false;
    const composer = findComposer();
    if(!composer) return false;
    let bar = document.getElementById('gw-quota-bar');
    if(bar){
      if(!force && bar.parentNode === composer && composer.firstElementChild === bar) return true;
      bar.remove();
    }
    bar = document.createElement('div');
    bar.id = 'gw-quota-bar';
    // 清理旧版残留的折叠按钮（已废弃）
    let _oldEx = document.getElementById('gw-q-expand');
    if(_oldEx) _oldEx.remove();
    // 卡内顶条（真·贴边）：横条直接插进输入框卡片内部第一行——零缝隙、
    // 撑满容器宽度（width:100% + 负 margin 抵消父容器 padding，分隔线横贯卡片），
    // 自身无背景无边框（融入卡片），只用底部分隔线区分状态区与输入区
    const T = themeCss();
    const barDivider = `<span class="gw-q-divider" style="width:1px;height:12px;background:${T.divider};margin:0 2px"></span>`;
    bar.style.cssText = `display:flex;width:100%;align-items:center;gap:6px;margin:0;padding:5px 12px 4px;background:transparent;border:none;border-bottom:1px solid ${T.divider};border-radius:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:11px;color:${T.text};box-sizing:border-box`;
    try {  // 父容器有 padding 时：bar 显式铺满父 border-box 宽（offsetWidth），
      // 负 margin 抵消左 padding 贴住卡片左缘 → 分隔线横贯整张卡片
      const _pc = getComputedStyle(composer);
      const _pl = parseFloat(_pc.paddingLeft) || 0;
      bar.style.width = (composer.offsetWidth || bar.offsetWidth) + 'px';
      bar.style.marginLeft = (-_pl) + 'px';
      bar.style.marginRight = '0';
    } catch(e){}
    bar.innerHTML = '<span id="gw-q-dot" style="width:6px;height:6px;border-radius:50%;background:#22c55e;flex-shrink:0"></span>'
      + '<span style="font-weight:600">AI Gateway</span>'
      + '<span style="opacity:.45;font-size:10px">工作中</span>'
      + '<span class="gw-detail" style="display:inline-flex;align-items:center;gap:6px">'
      + barDivider
      + '<span style="opacity:.55">账号</span><b id="gw-q-count" style="font-weight:600;font-family:ui-monospace,Consolas">--</b>'
      + barDivider
      + '<span style="opacity:.55">剩余额度</span><b id="gw-q-remain" style="font-weight:600;font-family:ui-monospace,Consolas">--</b>'
      + '</span>';
    composer.insertBefore(bar, composer.firstChild);  // 横条 = 卡片内部第一行，零缝隙贴边
    return true;
  }
  function loadData(){
    if(window.__gwJSVer !== MY_VER) return;
    fetch('__QUOTA_URL__', {cache:'no-store'}).then(r=>r.json()).then(d=>{
      const c = document.getElementById('gw-q-count'), m = document.getElementById('gw-q-remain');
      const dot = document.getElementById('gw-q-dot');
      if(!c || !m) return;
      c.textContent = d.count;
      m.textContent = Math.round(d.remain).toLocaleString();
      if(dot){ dot.style.background = '#22c55e'; }
    }).catch(()=>{
      const dot = document.getElementById('gw-q-dot');
      if(dot){ dot.style.background = '#9aa0a8'; }
    });
  }
  function tick(){ if(inject()){ loadData(); } }
  // 主题切换跟随：WorkBuddy 切深/浅色时 html 的 class 与 data-theme 同步变化，
  // 检测到即重刷横条配色。必须幂等注册（首次注入与重复注入都要有），
  // 否则升级 JS 后旧实例占着 __gwInited、新实例拿不到监听。
  if(!window.__gwThemeObs){
    try {
      window.__gwThemeObs = new MutationObserver(()=>{ restyleBar(); });
      window.__gwThemeObs.observe(document.documentElement, {attributes:true, attributeFilter:['class','data-theme']});
    } catch(e){}
  }
  if(!window.__gwInited){
    window.__gwInited = true;
    tick();
    window.__gwInterval = setInterval(tick, 5000);
    try {
      const obs = new MutationObserver(()=>{ if(!document.getElementById('gw-quota-bar')) inject(); });
      obs.observe(document.body, {childList:true, subtree:true});
    } catch(e){}
    return 'injected';
  }
  if(window.__gwInterval) clearInterval(window.__gwInterval);
  tick();
  inject(true); loadData();
  window.__gwInterval = setInterval(tick, 5000);
  return 'reinjected';
})()
"""


def _cdp_available(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except Exception:
        return False


def _first_page(port: int):
    """取目标应用的第一个 page 渲染进程。"""
    targets = requests.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
    return next((t for t in targets if t["type"] == "page"), None)


def inject_quota_bar(proxy_port: int = 8001, cdp_port: int = 9222) -> dict:
    """连目标应用 CDP 注入额度横条。未开 CDP 端口时返回 ok=False（静默）。"""
    name = INJECT_TARGETS.get(cdp_port, f"port {cdp_port}")
    if not _cdp_available(cdp_port):
        return {"ok": False, "error": f"{name} CDP 未开启（{cdp_port} 未监听）"}
    with _LOCK:
        ws = None
        try:
            page = _first_page(cdp_port)
            if not page:
                return {"ok": False, "error": f"未找到 {name} 主渲染进程"}
            js = INJECT_JS_TEMPLATE.replace("__QUOTA_URL__",
                                            f"http://127.0.0.1:{proxy_port}/__gw/quota")
            ws = create_connection(page["webSocketDebuggerUrl"], timeout=5)
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": js, "returnByValue": True}}))
            r = json.loads(ws.recv())
            return {"ok": True, "result": r.get("result", {}).get("result", {}).get("value")}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass


def inject_all(proxy_port: int = 8001) -> dict:
    """向所有已开 CDP 的目标应用注入（WorkBuddy 9222 / ZCode 9223）。返回 {应用名: 结果}。"""
    out = {}
    for port, name in INJECT_TARGETS.items():
        out[name] = inject_quota_bar(proxy_port, port)
    return out


def bar_present(cdp_port: int = 9222) -> bool:
    """目标渲染进程里横条是否已注入（轮询保活用，避免重复注入）。"""
    if not _cdp_available(cdp_port):
        return False
    ws = None
    try:
        page = _first_page(cdp_port)
        if not page:
            return False
        ws = create_connection(page["webSocketDebuggerUrl"], timeout=5)
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": "!!document.getElementById('gw-quota-bar')",
                                       "returnByValue": True}}))
        r = json.loads(ws.recv())
        return bool(r.get("result", {}).get("result", {}).get("value"))
    except Exception:
        return False
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
