# Changelog（开发版）

本文件面向开发者，记录每个版本的技术变更（根因、涉及的文件、机制改动）。
面向终端用户的更新日志见应用内「更新日志」弹窗（`src/gui/index.html`）。

## [v1.0.5] — 2026-07-27

从 v1.0.3 直接跃迁（跳过 v1.0.4）。本次聚焦三类问题：frameless 窗口健壮性、
账号调度与 UI 的数据同步闭环、运行时账号增删的并发安全。另含 v1.0.4 阶段的
代理链路可靠性加固（11 项审计修复）。

### 窗口与交互

#### frameless 窗口 resize 重写（`3755d04`）
- **问题**：`frameless=True` 下系统原生 sizing 失效；曾尝试补 `WS_THICKFRAME` +
  发 `WM_NCLBUTTONDOWN(HT*)` 让系统进 sizing loop，但「光标变双向箭头却拖不动」。
- **根因**：WebView2 子控件铺满客户区，鼠标消息被它吃掉，系统对顶层窗口的
  边缘命中测试收不到；`WM_NCLBUTTONDOWN` 的 sizing loop 也起不来。
- **方案**：彻底绕开 Win32 sizing。前端 JS 在边缘放透明触发条（`.rz`），mousedown
  后用 `screenX/screenY` 算 delta，`requestAnimationFrame` 节流，每帧调后端
  `resize_delta` → `GetWindowRect` + `SetWindowPos` 直接落尺寸。方向锚定对边不动。
- **文件**：`src/gui/win_chrome.py`（`resize_delta`）、`src/gui/app.py`（API）、
  `src/gui/index.html`（前端拖拽循环）、`src/gui/style.css`（`.rz` 触发条）。

#### 删除 WS_THICKFRAME 装饰机制，消除首次白边（`7d498b7`）
- **问题**：首次打开窗口四周闪一圈白边，重新 resize 或重开后消失。
- **根因**：曾为 resize 给窗口补 `WS_THICKFRAME`（带来非客户区），再用子类化拦
  `WM_NCCALCSIZE` 消除它。两步在后台线程顺序执行，中间帧 DWM 用框架色填了那圈
  非客户区 → 竞态白边。改用前端 delta resize 后 `WS_THICKFRAME` 不再被任何功能依赖。
- **方案**：删除 `enable_resize_border` / `suppress_nc_frame` / 整套窗口子类化机制
  （`_make_wndproc` / `NCCALCSIZE_PARAMS` / `WNDPROC` / `CallWindowProcW` 等）。
  frameless 窗口保持原生 `FormBorderStyle.None`，无非客户区即无白边。保留图标 +
  圆角。`win_chrome.py` 从 310 行精简到 ~180 行。

#### 关闭确认弹窗 + 资源清理（`cdff6b6`）
- **改动**：`winClose` 先 `showConfirm('确定退出应用？')`（带 `_closingConfirmed`
  防重复点）；`win_close` 销毁窗口前调 `cleanup()` 停 uvicorn；`cleanup` 对齐
  `proxy_stop`（补 `_active_count` 归零）。

#### 文件导入 loading 提示（`876dc8e`）
- **问题**：「正在读取文件」用 info toast，3 秒自动消失，但导入异步耗时，提示早早
  闪掉。
- **方案**：`showToast` 加 `loading` 类型（旋转图标）+ `persistent` 参数（不自动
  消失），由调用方在异步结束后用新 toast 覆盖。新增 `hideToast()`。

### 账号调度与 UI 同步闭环

这是本次的核心——建立「后端数据变化 → 前端及时反映」的完整闭环。

#### 账号增删同步调度器（`cdff6b6`）—— 幽灵调度修复
- **问题**：`delete_account` 只删 DB 不通知调度器，删除当前调度号后，内存
  `_accounts` 仍持有该 Account，`_current_id` 仍指向它，`get_next` 继续返回这个
  「幽灵账号」发请求。
- **方案**：`delete_account` / `delete_accounts` / `import_from_json` /
  `complete_oauth_and_save` 末尾加 `token_rotator.reload(platform)`。reload 的既有
  逻辑（`token_rotator.py:68-87`）会清掉无效 `_current_id` 并自动选下一个可用号。
- **并发安全**：`token_rotator` 全程 `threading.RLock`，reload / get_next 互斥，内存
  不会撕裂；js_api 线程调 reload 与 uvicorn 线程 get_next 竞争锁最多阻塞毫秒级。

#### 阈值切号后 UI 状态刷新（`93c9d72`）
- **问题**：`deduct_quota` 已把 `acc.status` 改 disabled 并持久化，但卡片仍显示「正常」。
- **根因**：① `pollProxyInfo` 只有当前号变化才 `loadAccountsLight`；②
  `updateCardsInPlace` 只 patch 配额/统计，不碰状态徽章和按钮区。
- **方案**：`pollProxyInfo` 在 `threshold_switch` 时无条件 `loadAccountsLight`；
  `updateCardsInPlace` 扩展为替换 `.badge-status-*` 徽章 + 重建 `.card-actions`。
  抽取 `cardActionsHtml(a)` 供 `renderAccounts` 和 `updateCardsInPlace` 共用。

#### 删除账号卡片不消失（`ce94052`）
- **问题**：删除提示成功但卡片还在，要手动刷新。
- **根因**：`deleteOne` 调 `loadAccountsLight` → `updateCardsInPlace` 只更新现有卡片
  DOM，不移除已删账号的卡片。
- **方案**：`deleteOne` 改用 `loadAccounts()` 全量重建；给 `updateCardsInPlace` 加
  增删兜底——DOM 卡片集合与 `currentAccounts` 的 ID/数量不一致时降级为
  `renderAccounts()` 全量重建，保护所有调用点。

#### 签到徽章即时更新（`1b821b3`）
- **问题**：签到成功后卡片签到徽章仍显示「未签到」。
- **根因**：`doCheckin`/`checkinAll` 调 `loadAccountsLight`，签到徽章不在
  `updateCardsInPlace` 的 patch 范围。
- **方案**：签到是低频操作，改用 `loadAccounts()` 全量重建。

### 数据刷新与告警

#### 导入自动刷新额度（`8d64a29`）
- **问题**：导入裸 token 后额度不显示，要手动点刷新。
- **根因**：导入依赖 `build_payload_from_token` 拉额度，该函数可能失败/不全
  （`quota_raw` 为 None）。手动刷新走 `refresh_token` → `refresh_full_payload`，更可靠。
- **方案**：`import_from_json` 循环里每个号落库后复用 `self.refresh_token`，导入即
  拉额度。失败不影响账号已导入（fallback 返回未刷新数据）。

#### 网关状态/额度运行时刷新（`ce94052`）
- **网关崩溃检测**：`pollProxyInfo` 的 `fetch('/proxy/info')` 失败被空 catch 吞掉，
  uvicorn 崩溃后前端永远显示「运行中」。加连续失败计数（≥3），判定网关已死后 toast
  并 `refreshProxyStatus` 同步。
- **额度统计定时刷新**：网关运行时额度不刷新（只有切号才拉）。`pollProxyInfo` 每
  ~10s（5 次轮询）`refreshAccountStats`。
- **可用账号归 0 告警**：`pollProxyInfo` 读取 `accounts_usable`，归 0 时 toast（带
  去重标志避免重复），恢复时清标志。

### 代理链路可靠性（v1.0.4 阶段，`9dc7d7b` 等）

11 项审计修复，含「请求永不返回」三处、故障转移失效、阈值切换备选账号额度校验
（防止切换死循环）。详见提交 `9dc7d7b`、`27bb92d`、`701a594`。

### 调度保障（已验证，无需改动）

确认 `get_next`（`token_rotator.py:120-144`）在所有路径下都能保证分配：
池空自动 reload；当前号失效遍历整个池找可用；401/403/429 经 `mark_disabled` 跳过；
阈值耗尽自动切号；重启 reload 恢复。返回 None 的唯一合法情况是池中所有账号均处于
disabled / banned / 过期 / 冷却 / 无 token 状态。

### 新增功能

#### 账号卡片运行统计
- 每张账号卡片底部新增统计行（`cardStatsHtml`）：请求次数、输入/输出 Token、
  累计消耗积分、缓存命中率。数据来自 `account_stats` 表（`update_account_stats`
  每次请求累加），前端通过 `get_all_stats` 拉取，`refreshAccountStats` 刷新。
- 网关运行时每 ~10s 自动刷新（见「数据刷新与告警」段）。

#### 应用内自动更新（`src/updater.py`）
- 检查更新：通过 GitHub API / gh CLI 拉最新 release，比对版本号。
- 下载更新：流式下载 exe 到临时目录。
- 一键重启更新：生成 bat 脚本，等待当前进程退出后覆盖 exe 并重启。
- 启动时自动检查，有新版本在「检查更新」旁显示下载图标 badge。

#### 商店视图
- 侧边栏新增商店入口（`showShopCards`），展示订阅方案。

### 其它

- 账号卡片显示导入日期（`created_at`，重复导入保留首次值）（`d28da1b`）
- 检查更新发现新版本时显示下载图标 badge，已是最新不自动关闭弹窗（`ce94052`）
- 网关栏标签改为「端口 / Key / 切号阈值」（`b2f7868`）

---

## [v1.0.3] — 2026-07-26

额度计算修复（个人体验版等套餐额度为 0）、运行日志面板、调度冷却持久化、当前账号
高亮与优先账号、封禁检测。详见应用内更新日志 v1.0.3 / v1.0.2 段。
