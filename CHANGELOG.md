# Changelog（开发版）

本文件面向开发者，记录每个版本的技术变更（根因、涉及的文件、机制改动）。
面向终端用户的更新日志见应用内「更新日志」弹窗（`src/gui/index.html`）。

## [v1.0.6] — 2026-08-01

账号管理增强 + 封禁检测闭环。从 v1.0.5 之后的 6 个功能提交（`69d4288` ~ `0a962e4`）
汇总。覆盖：OAuth 竞态根治、封禁检测闭环、账号多选批量操作、搜索/标签/回收站、
自定义日历日期筛选、全局用量统计、签到自动刷新额度。

### 账号管理增强

#### 账号搜索（`0a962e4`）
- filter-bar 内回收站按钮旁新增搜索框（`accountSearch`），按昵称/邮箱/ID 实时过滤，
  过滤逻辑在 `filteredAccounts` computed 里做，无后端改动。
- **UI 修复**：全局 `input[type="text"]`（`style.css` 全局样式，specificity 0,1,1）
  后定义覆盖了 `.account-search input`（0,1,1）的 `border:none/background:transparent`，
  导致输入框在容器里出现「双重边框」。修复用 `.filter-bar .account-search input`
  （0,2,1）显式覆盖 `padding/border/background/outline/box-shadow`。

#### 多选批量操作（`b51fd13` 批量框架 + `0a962e4` 补全）
- 多选模式下新增：启用 / 禁用 / 刷新 / 签到 / 导出 / 删除。
- 后端新增 `set_account_statuses`（批量启用/禁用，含最后可用号保护）、
  `refresh_accounts`、`checkin_accounts`、`detect_accounts`（`_detect_lock` 互斥，
  只在自家启动时才清 `running` 标志）。
- 全选三态复选框（未选 / 部分选横线 / 全选勾），选中数合并进「全选」标签。

#### 标签系统（`0a962e4`）
- 复用后端既有 `update_tags`（`app.py`），前端新增：卡片 `tag-pill` 显示（点击设
  `tagFilter`）、「更多→标签」编辑弹窗（`tagEditModal`/`saveTags`/`removeTagChip`）、
  工具栏标签筛选下拉。

#### 回收站（`7f4eb51`）
- 软删除 + 回收站：`list_deleted_accounts` 返回原状态 dict（含 `deleted_at`），
  tombstone 永久；stats LEFT JOIN 过滤 deleted；`reset_account_credit`/
  `reset_account_stats` 拆分。
- 防复活：`find_duplicate`/`revive_account` 显式清理 tombstone，软删号回原状态；
  显式 OAuth 登录同号 = 恢复意图，同时清调度器残留冷却/封禁计数。
- 前端：回收站弹窗（状态筛选/多选/恢复/彻底删除），图标为恢复循环箭头+时钟。

### 封禁检测优化（`9bf772b`，增强既有封禁检测）

> v1.0.2 已有基础封禁检测（检测接口标记 banned、不再使用）。本版是**闭环增强**：
> 批量检测、实时防线、启动阈值检测三块。

- `_detect_one`：refresh 续期 + 拉额度落库 → 真实 chat 请求探测 11140；全 11140 →
  标记 banned；任意 200 → 检测通过（原是 banned 则恢复 normal）；其他 → unknown
  不封不禁。只写状态不 reload（调用方统一做）。
- 批量检测（`detect_accounts`）线程池并发，`_detect_lock` 全程互斥；
  检测出的 banned 号自动禁用并 reload 调度器。
- **实时防线**（调度链路，非检测触发）：`_classify_upstream_error`
  （`proxy_server.py`）单独识别 11140 / request illegal 的顶层 code 结构（区别于
  嵌套 error 包裹的普通错误），返回 `banned` → `token_rotator` 按渐进冷却
  30s→1m→2m→5m→10m 处理，连续 5 次 11140 自动落库封禁（BANNED_COOLDOWNS）。
  普通 403 仍归 `auth`（临时鉴权，渐进冷却，不封号）。
- **启动阈值检测**（新增功能）：网关栏可设「启用阈值」，`detect_and_enable_accounts`
  一键并发（8 线程）检测全部账号：拉最新额度，剩余 >= 阈值的禁用号自动启用
  （阈值=0 不卡门槛），normal 保持并刷新额度，banned 跳过，额度拉取失败但非封禁
  视为可能可用仍启用。`_detect_state` 记录进度，前端 `detect_enable_status` 轮询
  展示进度弹窗；只在自家启动时才清 `running`。

### 签到自动刷新额度（`0a962e4`）

- **问题**：单账号签到后卡片额度条不更新。根因：`doCheckin` 签到后只
  `loadAccounts()` 重载列表（读本地 DB），`quota_raw` 是签到前存的旧数据——签到
  给的是积分，不动 quota，额度条永远不变。
- **方案**：`doCheckin` 签到成功后追加调 `refresh_token`（即 `refresh_full_payload`
  拉最新额度落库），再 `loadAccounts()`。刷新失败静默（`try/catch` 包裹），不影响
  签到结果提示。一键签到 `checkinAll` 未动（批量拉全部账号额度太重）。

### 自定义日历日期筛选（`0a962e4`）

- 原生 `input[type="date"]` 替换为主题化日历弹层：今天 / 昨天 / 近 7 天 / 近 30 天 /
  指定日期。指定日期弹 `.cal-panel`（上一月/下一月导航、今天高亮描边、选中主色
  填充、清除/完成按钮），全部用主题变量样式。
- `filterDate` 支持 `'' | 'YYYY-MM-DD' | 'last7' | 'last30'`，`filteredAccounts`
  按此分支过滤。日历状态（`calYear/calMonth/calWeeks`）与派生函数
  （`calTitle/calPrevMonth/calNextMonth/calPickDay/calIsToday/calIsSelected`）
  前端本地计算，无后端改动。
- 死代码清理：`customDateInput`/`onCustomDateChange`/`showPicker`/`window.prompt`
  全删。

### OAuth 竞态根治（`69d4288`）

- `poll_token` 不再 pop pending，改为只返回 token，由 `complete_oauth_and_save`
  的 `reset_pending()` 统一清理。并发 poll 都能拿到同一 token，互不伤害。
- 新增 `oauth_api.reset_pending()`，登录开始 + 完成时彻底清空 `_pending_oauth`。
- 前端 `_currentOauthLoginId` + `_oauthSettled` 双重 guard 防跨轮竞态，容忍单次
  「没有待处理」误报（连续 2 次才判定失败）。

### 其他

- 全局用量统计卡片（`5335660`）：统计占比 + 卡片高度恒定（常驻占位防跳动）。
- 图标系统换新 + 标题栏重构（`9372087`）。
- 主题持久化刷新闪深色 + 全局开屏 loading 揭幕（`c7d4bb3`）。
- 联营店铺文案还原 + 统计卡片占比 + 卡片高度恒定（`87aca83`）。
- 重复登录同号明确提示 is_update；手机号去重修复（去掉 @ 限制）；upsert 信息字段
  COALESCE 保护（新值为空保留旧值）；列表排序改 created_at DESC 稳定排序
  （`69d4288`）。
- 变更的提交：`69d4288` `c7d4bb3` `9372087` `5335660` `87aca83` `9bf772b`
  `7f4eb51` `0a962e4`。

---

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
