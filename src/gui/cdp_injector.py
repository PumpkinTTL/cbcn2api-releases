"""CDP 注入模块：连 WorkBuddy CDP（9222），把网关额度横条注入 AI 对话输入框上方。

横条每 5s fetch 网关 `/__gw/quota` 刷新（消耗通过 account_stats.total_credit 实时反映），
带版本控制（多次注入自动失效旧 JS）、折叠按钮、MutationObserver（视图切换自动重挂）。
WorkBuddy 未开启 CDP（9222 未监听）时静默返回 ok=False，不抛错。
"""
import json
import socket
import threading

import requests
from websocket import create_connection

CDP_PORT = 9222
_LOCK = threading.Lock()

INJECT_JS_TEMPLATE = r"""
(function(){
  window.__gwJSVer = (window.__gwJSVer || 0) + 1;
  const MY_VER = window.__gwJSVer;  // 旧注入的 interval/observer 版本不匹配自动失效
  function findComposer(){
    let c = document.querySelector('section.wb-home-composer')
      || document.querySelector('[class*="wb-home-composer"]')
      || document.querySelector('[class*="Composer"]');
    if(c) return c;
    const ed = document.querySelector('[contenteditable="true"]') || document.querySelector('textarea');
    if(!ed) return null;
    return ed.closest('section') || ed.closest('[class*="composer"]') || ed.closest('[class*="Composer"]') || ed.closest('[class*="input-box"]') || ed.parentElement;
  }
  function inject(force){
    if(window.__gwJSVer !== MY_VER) return false;
    const composer = findComposer();
    if(!composer || !composer.parentElement) return false;
    let bar = document.getElementById('gw-quota-bar');
    if(bar){
      if(!force && bar.parentNode === composer.parentElement && bar.nextElementSibling === composer) return true;
      bar.remove();
    }
    bar = document.createElement('div');
    bar.id = 'gw-quota-bar';
    let expand = document.getElementById('gw-q-expand');
    if(expand) expand.remove();
    // 胶囊条（内容包裹，不撑满父容器）
    bar.style.cssText = 'display:inline-flex;align-self:flex-start;align-items:center;gap:6px;margin:0 0 8px;padding:4px 10px;background:#fff;border:1px solid #e4e6eb;border-radius:14px;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:11px;color:#1f2329;box-shadow:0 1px 2px rgba(0,0,0,0.05);box-sizing:border-box';
    bar.innerHTML = '<span id="gw-q-dot" style="width:6px;height:6px;border-radius:50%;background:#22c55e;flex-shrink:0"></span>'
      + '<span style="font-weight:600">AI Gateway</span>'
      + '<span style="opacity:.45;font-size:10px">工作中</span>'
      + '<span class="gw-detail" style="display:inline-flex;align-items:center;gap:6px">'
      + '<span style="width:1px;height:12px;background:#e4e6eb;margin:0 2px"></span>'
      + '<span style="opacity:.55">账号</span><b id="gw-q-count" style="font-weight:600;font-family:ui-monospace,Consolas">--</b>'
      + '<span style="width:1px;height:12px;background:#e4e6eb;margin:0 2px"></span>'
      + '<span style="opacity:.55">剩余额度</span><b id="gw-q-remain" style="font-weight:600;font-family:ui-monospace,Consolas">--</b>'
      + '</span>'
      + '<span id="gw-q-toggle" style="cursor:pointer;opacity:.35;margin-left:2px;font-size:12px;user-select:none">›</span>';
    // 折叠态展开按钮（胶囊隐藏时只剩它，节省空间）
    expand = document.createElement('div');
    expand.id = 'gw-q-expand';
    expand.style.cssText = 'display:none;cursor:pointer;align-self:flex-start;align-items:center;gap:5px;margin:0 0 8px;padding:3px 8px;background:#fff;border:1px solid #e4e6eb;border-radius:12px;font-family:inherit;font-size:11px;color:#1f2329;box-shadow:0 1px 2px rgba(0,0,0,0.05);box-sizing:border-box';
    expand.innerHTML = '<span id="gw-q-expand-dot" style="width:6px;height:6px;border-radius:50%;background:#22c55e;display:inline-block"></span><span style="font-weight:600">AI Gateway</span><span style="opacity:.45;margin-left:2px">&#9656;</span>';
    composer.parentElement.insertBefore(expand, composer);
    composer.parentElement.insertBefore(bar, composer);  // 胶囊紧贴输入框上方
    // 交互：› 折叠 → 藏胶囊只留展开按钮；▸ 展开 → 恢复胶囊
    const tg = document.getElementById('gw-q-toggle');
    if(tg){
      tg.onclick = ()=>{
        bar.style.display = 'none';
        expand.style.display = 'inline-flex';
        window.__gwCollapsed = true;
      };
    }
    expand.onclick = ()=>{
      bar.style.display = 'inline-flex';
      expand.style.display = 'none';
      window.__gwCollapsed = false;
    };
    // 恢复记忆的折叠状态
    if(window.__gwCollapsed){
      bar.style.display = 'none';
      expand.style.display = 'inline-flex';
    }
    return true;
  }
  function loadData(){
    if(window.__gwJSVer !== MY_VER) return;
    fetch('__QUOTA_URL__', {cache:'no-store'}).then(r=>r.json()).then(d=>{
      const c = document.getElementById('gw-q-count'), m = document.getElementById('gw-q-remain');
      const dot = document.getElementById('gw-q-dot'), ed = document.getElementById('gw-q-expand-dot');
      if(!c || !m) return;
      c.textContent = d.count;
      m.textContent = Math.round(d.remain).toLocaleString();
      if(dot){ dot.style.background = '#22c55e'; }
      if(ed){ ed.style.background = '#22c55e'; }
    }).catch(()=>{
      const dot = document.getElementById('gw-q-dot'), ed = document.getElementById('gw-q-expand-dot');
      if(dot){ dot.style.background = '#9aa0a8'; }
      if(ed){ ed.style.background = '#9aa0a8'; }
    });
  }
  function tick(){ if(inject()) loadData(); }
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


def _cdp_available() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1)
        s.close()
        return True
    except Exception:
        return False


def inject_quota_bar(proxy_port: int = 8001) -> dict:
    """连 WorkBuddy CDP 注入额度横条。WorkBuddy 未开/未带 CDP 参数时返回 ok=False（静默）。"""
    if not _cdp_available():
        return {"ok": False, "error": "WorkBuddy CDP 未开启（9222 未监听）"}
    with _LOCK:
        ws = None
        try:
            targets = requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3).json()
            page = next((t for t in targets if t["type"] == "page"), None)
            if not page:
                return {"ok": False, "error": "未找到 WorkBuddy 主渲染进程"}
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


def bar_present() -> bool:
    """WorkBuddy 渲染进程里横条是否已注入（轮询保活用，避免重复注入）。"""
    if not _cdp_available():
        return False
    ws = None
    try:
        targets = requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3).json()
        page = next((t for t in targets if t["type"] == "page"), None)
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
