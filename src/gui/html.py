HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CB/WorkBuddy Manager</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --bg: #0a0e1a;
  --surface: rgba(255,255,255,0.04);
  --surface-hover: rgba(255,255,255,0.08);
  --surface-active: rgba(255,255,255,0.12);
  --border: rgba(255,255,255,0.08);
  --border-hover: rgba(255,255,255,0.15);
  --text: #e8edf5;
  --text-secondary: rgba(232,237,245,0.55);
  --text-muted: rgba(232,237,245,0.35);
  --primary: #3b82f6;
  --primary-hover: #2563eb;
  --primary-glow: rgba(59,130,246,0.25);
  --accent: #f97316;
  --accent-glow: rgba(249,115,22,0.25);
  --success: #22c55e;
  --success-glow: rgba(34,197,94,0.2);
  --warning: #eab308;
  --danger: #ef4444;
  --radius: 12px;
  --radius-sm: 8px;
  --radius-lg: 16px;
  --shadow: 0 8px 32px rgba(0,0,0,0.4);
  --transition: 0.2s cubic-bezier(0.4,0,0.2,1);
}
[data-theme="light"] {
  --bg: #f0f2f5;
  --surface: rgba(255,255,255,0.9);
  --surface-hover: #fff;
  --surface-active: #e8ebf0;
  --border: rgba(0,0,0,0.12);
  --border-hover: rgba(0,0,0,0.2);
  --text: #111318;
  --text-secondary: rgba(17,19,24,0.7);
  --text-muted: rgba(17,19,24,0.5);
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --primary-glow: rgba(37,99,235,0.2);
  --accent: #ea580c;
  --accent-glow: rgba(234,88,12,0.2);
  --success: #16a34a;
  --success-glow: rgba(22,163,74,0.15);
  --warning: #ca8a04;
  --danger: #dc2626;
  --shadow: 0 8px 32px rgba(0,0,0,0.08);
  --modal-overlay: rgba(0,0,0,0.3);
}
[data-theme="light"] .modal { background: #fff; }
[data-theme="light"] .modal-overlay { background: var(--modal-overlay); }
[data-theme="light"] input[type="date"] { color-scheme: light; }
[data-theme="light"] .quota-bar { background: rgba(0,0,0,0.06); }
[data-theme="light"] .spinner-sm { border-color: rgba(0,0,0,0.15); border-top-color: var(--primary); }
html, body { height: 100%; overflow: hidden; }
body {
  font-family: 'Inter', -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  display: flex;
  flex-direction: column;
}

/* ===== Scrollbar ===== */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.2); }

/* ===== Header ===== */
.header {
  padding: 20px 28px 0;
  flex-shrink: 0;
}
.header-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.header-title {
  font-size: 22px;
  font-weight: 700;
  background: linear-gradient(135deg, #3b82f6, #8b5cf6);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.header-title span { font-weight: 300; -webkit-text-fill-color: var(--text-secondary); }
.header-actions { display: flex; gap: 8px; align-items: center; }

/* ===== Tabs ===== */
.tabs {
  display: flex;
  gap: 4px;
  background: var(--surface);
  border-radius: var(--radius);
  padding: 4px;
  border: 1px solid var(--border);
}
.tab {
  padding: 8px 20px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: var(--transition);
  font-family: inherit;
  position: relative;
}
.tab:hover { color: var(--text); background: var(--surface-hover); }
.tab.active {
  color: #fff;
  background: var(--primary);
  box-shadow: 0 0 20px var(--primary-glow);
}

/* ===== Buttons ===== */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: var(--transition);
  white-space: nowrap;
}
.btn-primary {
  background: var(--primary);
  color: #fff;
}
.btn-primary:hover { background: var(--primary-hover); box-shadow: 0 0 20px var(--primary-glow); }
.btn-accent { background: var(--accent); color: #fff; }
.btn-accent:hover { box-shadow: 0 0 20px var(--accent-glow); }
.btn-ghost { background: var(--surface); color: var(--text); border: 1px solid var(--border); }
.btn-ghost:hover { background: var(--surface-hover); border-color: var(--border-hover); }
.btn-danger { background: rgba(239,68,68,0.15); color: var(--danger); }
.btn-danger:hover { background: rgba(239,68,68,0.25); }
.btn-sm { padding: 5px 10px; font-size: 12px; }
.btn-icon { padding: 8px; min-width: 36px; justify-content: center; }
.btn:disabled { opacity: 0.5; pointer-events: none; }
.btn-loading { pointer-events: none; }
.spinner-sm { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.25); border-top-color: #fff; border-radius: 50%; animation: spin 0.6s linear infinite; }

/* ===== Main Content ===== */
.main {
  flex: 1;
  padding: 16px 28px 28px;
  overflow-y: auto;
}

/* ===== Stats Bar ===== */
.stats {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
}
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 20px;
  flex: 1;
  backdrop-filter: blur(12px);
}
.stat-label { font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
.stat-value { font-size: 24px; font-weight: 700; }

/* ===== Account Grid ===== */
.account-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 12px;
}

/* ===== Account Card ===== */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  backdrop-filter: blur(12px);
  transition: var(--transition);
}
.card:hover { border-color: var(--border-hover); background: var(--surface-hover); }
.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 10px;
}
.card-avatar {
  width: 38px; height: 38px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--primary), #8b5cf6);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 600;
  color: #fff;
  flex-shrink: 0;
}
.card-info { flex: 1; min-width: 0; padding-left: 12px; }
.card-name { font-size: 14px; font-weight: 600; }
.card-email { font-size: 12px; color: var(--text-secondary); margin-top: 1px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-meta { display: flex; gap: 8px; align-items: center; margin-top: 8px; flex-wrap: wrap; }
.badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
}
.badge-pro { background: linear-gradient(135deg, rgba(59,130,246,0.2), rgba(139,92,246,0.2)); color: #93c5fd; border: 1px solid rgba(59,130,246,0.2); }
.badge-free { background: rgba(148,163,184,0.15); color: var(--text-secondary); border: 1px solid var(--border); }
.badge-enterprise { background: rgba(249,115,22,0.15); color: #fdba74; border: 1px solid rgba(249,115,22,0.2); }
.badge-trial { background: rgba(34,197,94,0.15); color: #86efac; border: 1px solid rgba(34,197,94,0.2); }

.card-actions { display: flex; gap: 4px; flex-shrink: 0; }

/* ===== Quota ===== */
.quota-section { margin-top: 10px; }
.quota-header { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }
.quota-header span:last-child { font-weight: 600; color: var(--text); }
.quota-bar { height: 4px; background: rgba(255,255,255,0.06); border-radius: 4px; overflow: hidden; margin-top: 4px; }
.quota-fill { height: 100%; border-radius: 4px; transition: width 0.6s cubic-bezier(0.4,0,0.2,1); background: linear-gradient(90deg, var(--primary), #8b5cf6); }
.quota-fill.warning { background: linear-gradient(90deg, var(--warning), var(--accent)); }
.quota-fill.danger { background: linear-gradient(90deg, var(--danger), #f43f5e); }

/* ===== Check-in ===== */
.checkin-status { margin-top: 10px; display: flex; align-items: center; gap: 8px; }
.checkin-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
}
.checkin-badge.done { background: rgba(34,197,94,0.25); color: #86efac; border: 1px solid rgba(34,197,94,0.3); }
.checkin-badge.pending { background: rgba(234,179,8,0.25); color: #fde047; border: 1px solid rgba(234,179,8,0.3); }
.checkin-badge.inactive { background: rgba(148,163,184,0.2); color: var(--text-secondary); border: 1px solid var(--border); }
[data-theme="light"] .checkin-badge.done { background: rgba(34,197,94,0.15); color: #15803d; border-color: rgba(34,197,94,0.3); }
[data-theme="light"] .checkin-badge.pending { background: rgba(234,179,8,0.15); color: #a16207; border-color: rgba(234,179,8,0.3); }
[data-theme="light"] .checkin-badge.inactive { background: rgba(0,0,0,0.06); color: var(--text-muted); border-color: var(--border); }
.streak-badge { font-size: 11px; color: var(--text-secondary); }

/* ===== Empty State ===== */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px 20px;
  color: var(--text-muted);
  grid-column: 1 / -1;
}
.empty-state svg { width: 64px; height: 64px; margin-bottom: 16px; opacity: 0.3; }
.empty-state h3 { font-size: 16px; font-weight: 600; margin-bottom: 8px; color: var(--text-secondary); }
.empty-state p { font-size: 13px; }

/* ===== Grid Loading ===== */
.grid-loading {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  padding: 80px 20px; grid-column: 1 / -1;
}
.grid-loading .spinner-sm { width: 24px; height: 24px; border-width: 3px; }

/* ===== Modal ===== */
.modal-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  backdrop-filter: blur(8px);
  z-index: 1000;
  align-items: center;
  justify-content: center;
}
.modal-overlay.show { display: flex; }
.modal {
  background: #131827;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 28px;
  width: 100%;
  max-width: 520px;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: var(--shadow);
}
.modal h2 { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
.modal p { font-size: 13px; color: var(--text-secondary); margin-bottom: 20px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 20px; }

/* ===== Form Elements ===== */
textarea, input[type="text"], input[type="url"], select {
  width: 100%;
  padding: 10px 14px;
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  transition: var(--transition);
  outline: none;
}
textarea:focus, input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-glow); }
textarea { resize: vertical; min-height: 120px; font-family: 'SF Mono', 'Fira Code', monospace; }
label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; color: var(--text-secondary); }
.form-group { margin-bottom: 16px; }

/* ===== OAuth Modal ===== */
.oauth-url-box {
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  word-break: break-all;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 16px;
}
.oauth-loading {
  text-align: center;
  padding: 40px 0;
}
.spinner {
  width: 36px; height: 36px;
  border: 3px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin: 0 auto 12px;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ===== Toast ===== */
.toast {
  position: fixed;
  top: 16px;
  left: 50%;
  transform: translateX(-50%) translateY(-20px);
  padding: 10px 24px;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 500;
  z-index: 9999;
  opacity: 0;
  transition: all 0.25s cubic-bezier(0.4,0,0.2,1);
  pointer-events: none;
  white-space: nowrap;
  box-shadow: 0 4px 20px rgba(0,0,0,0.3);
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.toast.error { background: #ef4444; color: #fff; }
.toast.success { background: #22c55e; color: #fff; }
.toast.info { background: #3b82f6; color: #fff; }
.toast.warning { background: #eab308; color: #000; }

/* ===== Quota Detail ===== */
.quota-list { margin-top: 10px; }
.quota-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.quota-item:last-child { border-bottom: none; }
.quota-item .label { color: var(--text-secondary); }
.quota-item .value { font-weight: 600; }

/* ===== Filter Bar ===== */
.filter-bar { display: flex; gap: 8px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
.filter-bar select, .filter-bar input[type="date"] { padding: 6px 10px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text); font-size: 12px; font-family: inherit; outline: none; cursor: pointer; }
.filter-bar select:focus, .filter-bar input[type="date"]:focus { border-color: var(--primary); }
.filter-bar input[type="date"] { color-scheme: dark; }

/* ===== Responsive ===== */
@media (max-width: 720px) {
  .header { padding: 16px; }
  .main { padding: 12px 16px; }
  .header-top { flex-direction: column; gap: 12px; align-items: flex-start; }
  .header-actions { width: 100%; flex-wrap: wrap; }
  .account-grid { grid-template-columns: 1fr; }
  .stats { flex-direction: column; }
}
</style>
</head>
<body>

<div class="header">
  <div class="header-top">
    <div class="header-title">CB/WB <span>Manager</span></div>
    <div class="header-actions">
      <button class="btn btn-ghost btn-sm" onclick="importFromLocal()">本地导入</button>
      <button class="btn btn-ghost btn-sm" onclick="showImportModal()">JSON粘贴</button>
      <button class="btn btn-ghost btn-sm" onclick="document.getElementById('fileInput').click()">选择文件</button>
      <input type="file" id="fileInput" accept=".json" style="display:none" onchange="handleFileImport(event)">
      <button class="btn btn-ghost btn-sm" onclick="showOAuthModal()">OAuth登录</button>
      <button class="btn btn-accent btn-sm" onclick="checkinAll()">一键签到</button>
      <button class="btn btn-primary btn-sm" onclick="refreshAll()">全部刷新</button>
      <button class="btn btn-ghost btn-sm btn-icon" onclick="toggleTheme()" id="themeBtn" title="切换主题"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg></button>
    </div>
  </div>
  <div class="tabs" id="tabs">
    <button class="tab active" data-platform="workbuddy" onclick="switchPlatform('workbuddy')">WorkBuddy</button>
    <button class="tab" data-platform="codebuddy_cn" onclick="switchPlatform('codebuddy_cn')">CodeBuddy CN</button>
  </div>
</div>

<div class="main" id="main">
  <div class="stats" id="stats"></div>
  <div class="filter-bar" id="filterBar" style="display:none">
    <button class="btn btn-ghost btn-sm" onclick="setFilterDate(-2)">前天</button>
    <button class="btn btn-ghost btn-sm" onclick="setFilterDate(-1)">昨天</button>
    <button class="btn btn-ghost btn-sm" onclick="setFilterDate(0)">今天</button>
    <input type="date" id="filterDate" onchange="applyFilter()">
    <button class="btn btn-ghost btn-sm" onclick="clearFilter()">清除</button>
  </div>
  <div class="account-grid" id="accountGrid"></div>
</div>

<!-- OAuth Modal -->
<div class="modal-overlay" id="oauthModal">
  <div class="modal">
    <h2>OAuth 登录</h2>
    <p>请在浏览器中打开以下链接完成授权</p>
    <div class="oauth-url-box" id="oauthUrl">加载中...</div>
    <div id="oauthStatus">
      <div class="oauth-loading">
        <div class="spinner"></div>
        <div style="color:var(--text-secondary);font-size:13px;">等待授权完成...</div>
      </div>
    </div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="cancelOAuth();closeModal('oauthModal')">取消</button>
    </div>
  </div>
</div>

<!-- Import Modal -->
<div class="modal-overlay" id="importModal">
  <div class="modal">
    <h2>导入账号</h2>
    <p>粘贴 JSON 内容或上传文件</p>
    <div class="form-group">
      <label>JSON 内容</label>
      <textarea id="importJson" placeholder='粘贴 JSON 内容...&#10;&#10;支持格式:&#10;1. { "access_token": "...", "email": "..." }&#10;2. [{ "access_token": "...", ... }]&#10;3. { "accounts": [...] }'></textarea>
    </div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal('importModal')">取消</button>
      <button class="btn btn-primary" onclick="doImport()">导入</button>
    </div>
  </div>
</div>

<!-- Quota Detail Modal -->
<div class="modal-overlay" id="quotaModal">
  <div class="modal">
    <h2>额度详情</h2>
    <p id="quotaAccountName"></p>
    <div id="quotaContent"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal('quotaModal')">关闭</button>
      <button class="btn btn-accent" onclick="refreshQuota()">刷新</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
let currentPlatform = 'workbuddy';
let currentAccounts = [];
let oauthTimer = null;
let quotaAccountId = null;

// ===== Platform Switch =====
function switchPlatform(platform) {
  currentPlatform = platform;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.platform === platform));
  loadAccounts();
}

// ===== Toast =====
function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast ${type === 'warning' ? 'warning' : type === 'error' ? 'error' : type === 'success' ? 'success' : 'info'} show`;
  setTimeout(() => el.classList.remove('show'), 3000);
}

// ===== Button Loading =====
function btnLoading(btn) {
  if (!btn || btn.tagName !== 'BUTTON') return;
  btn._origHTML = btn.innerHTML;
  btn.disabled = true;
  btn.classList.add('btn-loading');
  btn.innerHTML = '<span class="spinner-sm"></span>';
}
function btnDone(btn) {
  if (!btn || btn.tagName !== 'BUTTON') return;
  btn.disabled = false;
  btn.classList.remove('btn-loading');
  if (btn._origHTML) btn.innerHTML = btn._origHTML;
}

// ===== Modal Helpers =====
function showModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) {
  document.getElementById(id).classList.remove('show');
  if (id === 'oauthModal') cancelOAuth();
}

// ===== Load Accounts =====
async function loadAccountsLight() {
  try {
    const raw = await pywebview.api.list_accounts(currentPlatform);
    currentAccounts = JSON.parse(raw);
    renderStats();
    buildFilter();
    renderAccounts();
  } catch (e) {}
}

async function loadAccounts() {
  try {
    const raw = await pywebview.api.list_accounts(currentPlatform);
    currentAccounts = JSON.parse(raw);
    renderStats();
    buildFilter();
    renderAccounts();
    if (currentAccounts.length > 0) {
      pywebview.api.batch_checkin_status(currentPlatform).then(() => loadAccountsLight()).catch(() => {});
    }
  } catch (e) {
    currentAccounts = [];
    renderStats();
    buildFilter();
    renderAccounts();
  }
}

function renderStats() {
  const el = document.getElementById('stats');
  el.innerHTML = `
    <div class="stat-card"><div class="stat-label">总账号</div><div class="stat-value" id="statTotal">-</div></div>
    <div class="stat-card"><div class="stat-label">已用/总量</div><div class="stat-value" id="statQuota">-</div></div>
    <div class="stat-card"><div class="stat-label">今日签到</div><div class="stat-value" id="statCheckin" style="color:var(--success)">-</div></div>
  `;
  pywebview.api.get_stats(currentPlatform).then(raw => {
    const d = JSON.parse(raw);
    document.getElementById('statTotal').textContent = d.total_accounts;
    document.getElementById('statQuota').textContent = `${d.total_used} / ${d.total_quota}`;
    document.getElementById('statCheckin').textContent = d.checked_in_today;
  }).catch(() => {});
}

function dateStr(d) {
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}

function buildFilter() {
  for (const a of currentAccounts) {
    if (a.created_at) {
      document.getElementById('filterBar').style.display = 'flex';
      return;
    }
  }
  document.getElementById('filterBar').style.display = 'none';
}

function setFilterDate(offset) {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  document.getElementById('filterDate').value = dateStr(d);
  applyFilter();
}

function applyFilter() {
  renderAccounts();
}

function clearFilter() {
  document.getElementById('filterDate').value = '';
  renderAccounts();
}

function getFilteredAccounts() {
  const val = document.getElementById('filterDate').value;
  if (!val) return currentAccounts;
  return currentAccounts.filter(a => {
    if (!a.created_at) return false;
    const d = new Date(a.created_at * 1000);
    return dateStr(d) === val;
  });
}

function renderAccounts() {
  const filtered = getFilteredAccounts();
  const el = document.getElementById('accountGrid');
  if (!filtered.length) {
    if (currentAccounts.length) {
      el.innerHTML = `
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>
          <h3>无匹配账号</h3>
          <p>尝试调整筛选条件</p>
        </div>
      `;
    } else {
      el.innerHTML = `
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M15.75 6a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0ZM4.501 20.118a7.5 7.5 0 0 1 14.998 0A17.933 17.933 0 0 1 12 21.75c-2.676 0-5.216-.584-7.499-1.632Z"/></svg>
          <h3>暂无账号</h3>
          <p>使用 OAuth 登录或导入 JSON 来添加账号</p>
        </div>
      `;
    }
    return;
  }

  el.innerHTML = filtered.map(a => {
    const avatarChar = (a.nickname || a.email || '?')[0].toUpperCase();
    const planBadge = getPlanBadge(a);
    const quotaData = parseQuota(a);
    const checkinData = parseCheckin(a);

    return `
      <div class="card">
        <div class="card-header">
          <div style="display:flex;align-items:center;flex:1;min-width:0">
            <div class="card-avatar">${avatarChar}</div>
            <div class="card-info">
              <div class="card-name">${escapeHtml(a.nickname || a.email || '未命名')}</div>
              <div class="card-email">${escapeHtml(a.email)}</div>
              <div class="card-meta">
                ${planBadge}
                ${a.enterprise_name ? `<span style="font-size:11px;color:var(--text-muted)"><svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" style="vertical-align:-1px"><rect x="4" y="2" width="16" height="20" rx="1"/><path d="M9 6h2M13 6h2M9 10h2M13 10h2M9 14h2M13 14h2"/></svg> ${escapeHtml(a.enterprise_name)}</span>` : ''}
              </div>
            </div>
          </div>
          <div class="card-actions">
            <button class="btn btn-ghost btn-sm btn-icon" onclick="showQuotaDetail('${a.id}')" title="额度详情"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 20h20M4 16l4-8 4 4 4-6 4 8"/></svg></button>
            <button class="btn btn-ghost btn-sm btn-icon" onclick="doCheckin('${a.id}')" title="签到"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg></button>
            <button class="btn btn-ghost btn-sm btn-icon" onclick="refreshOne('${a.id}')" title="刷新"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2v6h-6M3 12a9 9 0 0115-6.5L21 8M3 22v-6h6M21 12a9 9 0 01-15 6.5L3 16"/></svg></button>
            <button class="btn btn-danger btn-sm btn-icon" onclick="deleteOne('${a.id}')" title="删除"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg></button>
          </div>
        </div>

        ${quotaData ? `
        <div class="quota-section">
          <div class="quota-header">
            <span>已用 ${quotaData.used} / 剩余 ${quotaData.total - quotaData.used}</span>
            <span>${quotaData.total}</span>
          </div>
          <div class="quota-bar"><div class="quota-fill ${quotaData.pct >= 80 ? 'danger' : quotaData.pct >= 50 ? 'warning' : ''}" style="width:${Math.min(quotaData.pct, 100)}%"></div></div>
        </div>` : ''}

        <div class="checkin-status">
          ${checkinData !== null ? `
            <span class="checkin-badge ${checkinData.done ? 'done' : 'pending'}">
              ${checkinData.done
                ? '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" style="vertical-align:-1px"><path d="M5 13l4 4L19 7"/></svg> 已签到'
                : '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> 未签到'}
            </span>
            ${checkinData.streak > 0 ? `<span class="streak-badge"><svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" style="vertical-align:-1px"><path d="M8 2c0 4-3 6-3 9 0 4 3 7 7 7s7-3 7-7c0-3-2-5-3-7 0 3-2 5-4 5s-4-3-4-7z"/><path d="M12 22v-4"/></svg> 连续 ${checkinData.streak} 天</span>` : ''}
          ` : `<span class="checkin-badge inactive">签到不可用</span>`}
        </div>
      </div>
    `;
  }).join('');
}

// ===== Helpers =====
function escapeHtml(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function getPlanBadge(a) {
  const code = a.dosage_notify_code || a.plan_type || '';
  const pkg = a.quota_raw?.userResource?.data?.Resources;
  let isPro = false, isEnterprise = false, isTrial = false;
  if (pkg) {
    for (const r of pkg) {
      const pc = r.PackageCode || '';
      if (['TCACA_code_002_AkiJS3ZHF5', 'TCACA_code_003_FAnt7lcmRT'].includes(pc)) isPro = true;
      if (pc === 'TCACA_code_007_nzdH5h4Nl0') isTrial = true;
    }
  }
  if (a.enterprise_id || a.enterprise_name) isEnterprise = true;

  if (isEnterprise) return '<span class="badge badge-enterprise">企业版</span>';
  if (isPro) return '<span class="badge badge-pro">Pro</span>';
  if (isTrial) return '<span class="badge badge-trial">试用</span>';
  return '<span class="badge badge-free">Free</span>';
}

function _n(v) {
  if (v == null) return 0;
  if (typeof v === 'number') return v;
  const n = Number(v);
  return isNaN(n) ? 0 : n;
}

function parseQuota(a) {
  try {
    const qr = a.quota_raw;
    if (!qr) return null;
    const ur = qr.userResource || a.usage_raw;
    if (!ur) return null;
    const accts = ur.data?.Response?.Data?.Accounts || ur.data?.Resources || [];
    if (!accts.length) return null;
    let total = 0, used = 0;
    for (const r of accts) {
      const t = _n(r.CycleCapacitySizePrecise) || _n(r.CycleCapacitySize) || _n(r.CapacitySizePrecise) || _n(r.CapacitySize) || 0;
      const remain = _n(r.CycleCapacityRemainPrecise) || _n(r.CycleCapacityRemain) || _n(r.CapacityRemainPrecise) || _n(r.CapacityRemain) || 0;
      const u = _n(r.CapacityUsedPrecise) || _n(r.CapacityUsed) || (t - remain);
      total += t;
      used += u;
    }
    if (total === 0) return null;
    return { total, used, pct: Math.round((used / total) * 100) };
  } catch { return null; }
}

function parseCheckin(a) {
  const today = Math.floor(Date.now() / 1000);
  const dayStart = today - (today % 86400);
  const lt = a.last_checkin_time;
  const done = lt ? lt >= dayStart : false;
  return { done, streak: a.checkin_streak || 0 };
}

// ===== Actions =====
async function refreshAll() {
  const btn = event?.currentTarget;
  btnLoading(btn);
  try {
    const raw = await pywebview.api.refresh_all(currentPlatform);
    const r = JSON.parse(raw);
    showToast(`刷新完成: ${r.success}/${r.total}`, r.success > 0 ? 'success' : 'info');
    await loadAccounts();
  } catch (e) { showToast('刷新失败: ' + e, 'error'); }
  finally { btnDone(btn); }
}

async function refreshOne(id) {
  const btn = event?.currentTarget;
  btnLoading(btn);
  try {
    const raw = await pywebview.api.refresh_token(currentPlatform, id);
    const r = JSON.parse(raw);
    if (r.error) { showToast(r.error, 'error'); return; }
    showToast('刷新成功', 'success');
    await loadAccountsLight();
  } catch (e) { showToast('刷新失败: ' + e, 'error'); }
  finally { btnDone(btn); }
}

async function deleteOne(id) {
  if (!confirm('确认删除该账号？')) return;
  const btn = event?.currentTarget;
  btnLoading(btn);
  try {
    await pywebview.api.delete_account(currentPlatform, id);
    showToast('已删除', 'success');
    await loadAccountsLight();
  } catch (e) { showToast('删除失败: ' + e, 'error'); }
  finally { btnDone(btn); }
}

// ===== OAuth =====
async function showOAuthModal() {
  showModal('oauthModal');
  document.getElementById('oauthUrl').textContent = '正在获取登录链接...';
  document.getElementById('oauthStatus').innerHTML = `
    <div class="oauth-loading"><div class="spinner"></div><div style="color:var(--text-secondary);font-size:13px;">等待授权完成...</div></div>
  `;

  try {
    const raw = await pywebview.api.oauth_start(currentPlatform);
    const r = JSON.parse(raw);
    if (r.error) { showToast(r.error, 'error'); closeModal('oauthModal'); return; }

    document.getElementById('oauthUrl').textContent = r.verification_uri;

    document.getElementById('oauthStatus').innerHTML = `
      <div style="margin-bottom:12px;">
        <button class="btn btn-primary btn-sm" onclick="window.open('${r.verification_uri}', '_blank')">打开链接</button>
        <button class="btn btn-ghost btn-sm" onclick="navigator.clipboard.writeText('${r.verification_uri}')">复制链接</button>
      </div>
      <div class="oauth-loading">
        <div class="spinner"></div>
        <div style="color:var(--text-secondary);font-size:13px;">等待授权完成...</div>
      </div>
    `;

    startOAuthPolling(r.login_id);
  } catch (e) {
    showToast('启动 OAuth 失败: ' + e, 'error');
    closeModal('oauthModal');
  }
}

function startOAuthPolling(loginId) {
  if (oauthTimer) clearInterval(oauthTimer);
  oauthTimer = setInterval(async () => {
    try {
      const raw = await pywebview.api.oauth_poll(loginId);
      const r = JSON.parse(raw);
      if (r.status === 'completed') {
        clearInterval(oauthTimer);
        oauthTimer = null;
        await completeOAuth(r.data);
      } else if (r.error) {
        clearInterval(oauthTimer);
        oauthTimer = null;
        showToast(r.error, 'error');
        closeModal('oauthModal');
      }
    } catch (e) {}
  }, 1500);
}

async function completeOAuth(data) {
  try {
    const raw = await pywebview.api.complete_oauth_and_save(currentPlatform, JSON.stringify(data));
    const r = JSON.parse(raw);
    if (r.error) { showToast(r.error, 'error'); return; }
    showToast(`登录成功: ${r.email}`, 'success');
    closeModal('oauthModal');
    await loadAccounts();
  } catch (e) {
    showToast('保存账号失败: ' + e, 'error');
  }
}

function cancelOAuth() {
  if (oauthTimer) { clearInterval(oauthTimer); oauthTimer = null; }
  try { pywebview.api.oauth_cancel(); } catch(e) {}
}

// ===== Import =====
function showImportModal() {
  document.getElementById('importJson').value = '';
  showModal('importModal');
}

async function doImport() {
  const json = document.getElementById('importJson').value.trim();
  if (!json) { showToast('请输入 JSON 内容', 'error'); return; }
  const btn = event?.currentTarget;
  btnLoading(btn);
  try {
    const raw = await pywebview.api.import_from_json(currentPlatform, json);
    const r = JSON.parse(raw);
    if (r.error) { showToast(r.error, 'error'); return; }
    showToast(`导入成功: ${r.accounts?.length || 0} 个账号`, 'success');
    closeModal('importModal');
    await loadAccounts();
  } catch (e) { showToast('导入失败: ' + e, 'error'); }
  finally { btnDone(btn); }
}

// ===== File Import =====
async function handleFileImport(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  showToast('正在读取文件...', 'info');
  try {
    const text = await file.text();
    const raw = await pywebview.api.import_from_json(currentPlatform, text);
    const r = JSON.parse(raw);
    if (r.error) { showToast(r.error, 'error'); return; }
    showToast(`文件导入成功: ${r.accounts?.length || 0} 个账号`, 'success');
    await loadAccounts();
  } catch (e) { showToast('文件导入失败: ' + e, 'error'); }
  event.target.value = '';
}

// ===== Local Import =====
async function importFromLocal() {
  const btn = event?.currentTarget;
  btnLoading(btn);
  try {
    const raw = await pywebview.api.import_from_local(currentPlatform);
    const r = JSON.parse(raw);
    if (r.error) { showToast(r.error, 'error'); return; }
    showToast(`本地导入成功: ${r.email || r.id}`, 'success');
    await loadAccounts();
  } catch (e) { showToast('本地导入失败: ' + e, 'error'); }
  finally { btnDone(btn); }
}

// ===== Check-in =====
async function checkinAll() {
  const btn = event?.currentTarget;
  btnLoading(btn);
  try {
    const raw = await pywebview.api.checkin_all(currentPlatform);
    const r = JSON.parse(raw);
    showToast(`签到完成: ${r.success}成功 / ${r.already}已签 / ${r.failed}失败`, r.failed > 0 ? 'warning' : 'success');
    await loadAccountsLight();
  } catch (e) { showToast('批量签到失败: ' + e, 'error'); }
  finally { btnDone(btn); }
}

async function doCheckin(id) {
  const btn = event?.currentTarget;
  btnLoading(btn);
  try {
    const raw = await pywebview.api.checkin(currentPlatform, id);
    const r = JSON.parse(raw);
    if (r.error) { showToast(r.error, 'error'); return; }
    if (r.success) {
      showToast(`签到成功! ${r.credit ? '+'+r.credit+' 积分' : ''}`, 'success');
    } else {
      showToast(r.message || '签到失败(可能已签到)', 'info');
    }
    await loadAccountsLight();
  } catch (e) { showToast('签到失败: ' + e, 'error'); }
  finally { btnDone(btn); }
}

// ===== Quota Detail =====
async function showQuotaDetail(id) {
  quotaAccountId = id;
  const account = currentAccounts.find(a => a.id === id);
  document.getElementById('quotaAccountName').textContent = account?.email || '额度详情';
  document.getElementById('quotaContent').innerHTML = '<div style="text-align:center;padding:30px 0"><div class="spinner-sm"></div><div style="color:var(--text-muted);font-size:13px;margin-top:8px">加载中...</div></div>';
  showModal('quotaModal');

  try {
    const raw = await pywebview.api.get_quota(currentPlatform, id);
    const r = JSON.parse(raw);
    if (r.error) {
      document.getElementById('quotaContent').innerHTML = `<p style="color:var(--danger)">${r.error}</p>`;
    } else {
      renderQuotaContent(r);
    }
  } catch (e) {
    document.getElementById('quotaContent').innerHTML = `<p style="color:var(--danger)">查询失败: ${e}</p>`;
  }
}

function renderQuotaContent(data) {
  let html = '';

  if (data.dosage_notify_zh || data.dosage_notify_en) {
    html += `<div style="background:rgba(234,179,8,0.1);border:1px solid rgba(234,179,8,0.2);border-radius:8px;padding:12px;margin-bottom:12px;font-size:13px;color:#fde047;">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><path d="M12 9v4M12 17h.01M10.3 3.86l-8.1 14c-.6 1.1.1 2.5 1.3 2.5h16.8c1.2 0 1.9-1.4 1.3-2.5l-8.1-14c-.6-1.1-2.1-1.1-2.7 0z"/></svg> ${data.dosage_notify_zh || data.dosage_notify_en || ''}
    </div>`;
  }

  if (data.payment_type) {
    html += `<div class="quota-item"><span class="label">付费方式</span><span class="value">${data.payment_type}</span></div>`;
  }

  if (data.resources?.length) {
    let allTotal = 0, allUsed = 0, allRemain = 0;
    for (const r of data.resources) {
      allTotal += _n(r.total);
      allUsed += _n(r.used);
      allRemain += _n(r.remain);
    }
    const allPct = allTotal > 0 ? Math.round((allUsed / allTotal) * 100) : 0;
    const allCls = allPct >= 80 ? 'danger' : allPct >= 50 ? 'warning' : '';
    html += `<div style="margin-bottom:12px;">
      <div class="quota-item" style="border-bottom:none;padding-bottom:4px;">
        <span class="label">总计</span>
        <span class="value">已用 ${allUsed} / 剩余 ${allRemain}</span>
      </div>
      <div class="quota-bar"><div class="quota-fill ${allCls}" style="width:${Math.min(allPct, 100)}%"></div></div>
    </div>`;

    html += '<div class="quota-list">';
    for (const r of data.resources) {
      const total = _n(r.total);
      const used = _n(r.used);
      const remain = _n(r.remain);
      const pct = total > 0 ? Math.round((used / total) * 100) : 0;
      const cls = pct >= 80 ? 'danger' : pct >= 50 ? 'warning' : '';
      const isBase = r.packageCode === 'TCACA_code_009_0XmEQc2xOf';
      html += `
        <div style="padding:8px 0;border-bottom:1px solid var(--border);">
          <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:2px;">
            <span style="color:var(--text-secondary)">${r.packageName || r.packageCode || '资源包'}${isBase ? ' (基础包)' : ''}</span>
            <span style="font-weight:600">${remain}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-muted);margin-bottom:2px;">
            <span>已用 ${used}</span>
            <span>${total}</span>
          </div>
          <div class="quota-bar"><div class="quota-fill ${cls}" style="width:${Math.min(pct, 100)}%"></div></div>
        </div>`;
    }
    html += '</div>';
  }

  if (!html) {
    html = '<p style="color:var(--text-muted);text-align:center;padding:20px 0;">暂无配额数据</p>';
  }

  document.getElementById('quotaContent').innerHTML = html;
}

async function refreshQuota() {
  if (quotaAccountId) await showQuotaDetail(quotaAccountId);
}

// ===== Theme =====
function setThemeUI(theme) {
  const html = document.documentElement;
  const isLight = theme === 'light';
  html.setAttribute('data-theme', isLight ? 'light' : '');
  const btn = document.getElementById('themeBtn');
  btn.innerHTML = isLight
    ? '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>'
    : '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>';
}

async function toggleTheme() {
  const html = document.documentElement;
  const isDark = html.getAttribute('data-theme') !== 'light';
  const theme = isDark ? 'light' : 'dark';
  setThemeUI(theme);
  await pywebview.api.set_theme(theme);
}

// ===== Init =====
function init() {
  const tryTheme = () => {
    try {
      pywebview.api.get_theme().then(raw => { if (raw === 'light') setThemeUI('light'); }).catch(() => {});
    } catch(e) { setTimeout(tryTheme, 200); }
  };
  tryTheme();
  const tryLoad = (attempt = 0) => {
    try {
      loadAccounts().catch(() => { if (attempt < 10) setTimeout(() => tryLoad(attempt + 1), 500); });
    } catch(e) { if (attempt < 10) setTimeout(() => tryLoad(attempt + 1), 500); }
  };
  tryLoad();
}
document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>
"""
