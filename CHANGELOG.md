# Changelog（开发版）

本文件面向开发者，记录每个版本的技术变更（根因、涉及的文件、机制改动）。
面向终端用户的更新日志见应用内「更新日志」弹窗（`src/gui/index.html`）。

## [v1.0.9] — 2026-08-12

离线激活码永久码修复 + 窗口最大化跨线程修复 + 托盘交互重构 + 设置中心 + 积分区间筛选 + 工具栏/对话框 UI 统一。

### 离线激活码永久码修复（`lic-admin/server.py` 已部署 + 客户端清理）

- **双层根因**：
  1. 永久授权 `exp` 原填 `0xFFFFFFFF`，超过 32 位无符号上限，编码时被截断成 1970 年附近时间戳，验签时 `exp` 校验异常。
  2. 离线码算法确定性（80bit = `exp:32` + `HMAC sig:48`，见 `src/license_core.py`），同一 `exp` 永远生成同一码字符串；永久码若共用一个 `exp`，跨批次生成完全相同的码，命中客户端 `offline_license_records` 防重用表，第二次即被判「该码已用过」。
- **修复**：永久码 `base_exp` 改为 `0xFFFFFFFF - secrets.randbelow(0x10000000)` 随机起算——既不溢出 32 位，又让每个永久码 `exp` 各不相同，避免码字符串碰撞。
- **客户端**：清理本地 `offline_license_records`（`src/storage/store.py`）中因旧算法残留的失效记录。

### 窗口最大化跨线程修复（`main.py`）

- **根因**：pywebview 窗口事件处理器跑在后台线程（`pywebview/event.py` 每个 handler 新起 `Thread`），而 `SetWindowLongPtrW` 子类化（`src/gui/win_chrome.py` / `tray.py`）必须在创建窗口的 UI 线程执行；跨线程调用导致 WM_GETMINMAXINFO 处理失效，最大化时边框/客户区计算错位。
- **修复**：`_apply_window_chrome(window)` 改用 `form.Invoke(Func[Type](cb))` 把 `apply_system_chrome` + `tray.ensure` 编组回 UI 线程。pythonnet 虚方法覆盖不可行（`BrowserForm` 在 `webview/platforms/winforms.py:190` 硬编码，必须在类定义时注册），故采用运行时 Invoke 编组方案。

### 托盘交互重构（`main.py` + `src/gui/app.py`）

- **行为改为业界标准**：关闭按钮（X）→ 最小化到托盘（首次弹选择框：最小化到托盘 / 退出，带「不再询问」，Shift+点击 X 强制重弹）；最小化按钮 → 任务栏（不再进托盘）。
- **实现**：删除 `_on_window_minimized`；`app.py` 新增 `set_tray_config(path, on_restore)` 与 `win_minimize_to_tray()` API（均经 `Form.Invoke` 编组到 UI 线程）；`showCloseChoice()` 复用 `confirm-overlay` 样式。

### 设置中心（`src/gui/index.html` + `style.css`）

- 侧边栏新增「设置」视图，三区块：常规（关闭行为 + 主题）、运行（日志记录 + 启动/切号阈值）、关于（版本 + 检查更新 + 授权状态）。
- 主题与下拉复用 `status-dropdown` 组件（统一非 native select 风格）；标题栏齿轮按钮经 `window.__goSettings` 桥接跳设置页。

### 积分区间筛选（`src/gui/index.html`）

- 主筛选栏新增按「剩余积分区间」过滤：`lowQuotaMin` / `lowQuotaMax` / `lowQuotaActive` 三个 ref；`filteredAccounts` computed 在所有筛选条件（状态/日期/搜索/标签/地区/置顶）之后，按 `getCardQuota().adjRemain` 落在 `[min, max]` 过滤；留空 = 该侧不限，无额度数据的账号开启时排除。
- 控件复用 `detect-field` 外观（`range-field` 变体），双输入 + 分隔符 + 漏斗切换按钮，开启时主色高亮。

### 工具栏与对话框 UI 优化（`src/gui/index.html` + `style.css`）

- **启动阈值输入框移除**：主栏不再放启动阈值输入（设置页已有），验活功能改为工具栏独立图标按钮（`filter-icon-btn`），复用设置页 `enableThreshold`。
- **对话框图标**：`showConfirm` / `showCloseChoice` 按钮增加语义图标（取消 / 确定 / 危险 / 退出 / 最小化到托盘）。
- **筛选下拉统一**：状态 / 标签 / 地区筛选由 native `select` 改为 `status-dropdown` 组件。
- **细节**：全局隐藏 `input[type=number]` 浏览器自带 spinner；积分区间输入居中对齐，占位「最小/最大」对称。

> v1.0.9 早期已发布内容（机器码稳定、回收站批次备注、在线更新断点续传、回收站布局优化）技术细节见此前提交，用户向更新条目见应用内更新日志。

## [v1.0.8] — 2026-08-09

（技术变更细节待补；用户向更新条目见应用内「更新日志」弹窗：额度快照异常误切号修复、回收站按批次分组、验活结果弹窗优化、香港号 8 位本地号识别。）

## [v1.0.7] — 2026-08-07

授权系统上线 + 导入模型双平台支持 + 额度双口径汇算 + 调度稳定性加固 + UI 优化。
（`ab01775` 已提交；授权系统、双口径汇算、切号留痕、UI 优化为构建前未提交改动，详见下。）

### 授权系统上线（未提交，对应 `docs/LICENSE_SYSTEM.md`）

- **lic-admin 授权后台**：独立部署（FastAPI + SQLite），管理产品/在线激活码/离线授权码/授权开关。
- **授权开关按产品**：客户端启动查 `GET /api/v1/config?id=<APP_ID>` 的 `enable_license_check`：
  `false` 直接放行，`true` 走激活/验证；断网兜底保守走授权。
- **两类激活码**（`src/license.py`）：
  - 在线激活码（`PREFIX-12位hex`）：联网激活/验证，机器码绑定（MAC 哈希 `MID-`），
    服务端管理状态（unused/active/disabled/expired），禁用/删除即时生效。
  - 离线授权码（`XXXX-XXXX-XXXX-XXXX` Crockford base32）：纯本地 `license_core` 验签，
    无需联网；防重用落库到新表 `offline_license_records`（`src/storage/store.py`），
    软件重装/清缓存记录仍在。
- **远程验证**：在线码 `_verify_online` 每次启动调 `/api/v1/verify`，403 = 未授权/已禁用/已过期；
  断网返回「无法连接授权服务器」。远端开启授权后，未激活用户启动即见激活界面（pywebview IPC：
  `check_license` / `activate` / `get_machine_code`，`src/gui/app.py`）。
- **开发/生产分流**：非打包连 `http://127.0.0.1:8022`，打包版（frozen）连
  `https://license.bitlesu.com`；环境变量 `LIC_SERVER` 始终可覆盖。

### 额度双口径汇算 + 分页拉全（未提交，对应 `docs/QUOTA_ESTIMATE.md`）

- **双口径分离**（`src/api/quota.py`）：`parse_resources(accounts, active_only)`。
  - 调度口径 `active_only=True`：只统计 `Status=0` 有效包 → 估算剩余、阈值切号。
  - 展示口径 `active_only=False`：`Status=0` + `Status=3` 全量 → UI 总额度/已用/详情。
  - 根因：`Status=3`（已耗尽）裂变包计入 used 会把估算剩余压成 ≈0，「有额度却提前切号」。
- **分页拉取全量套餐包**：`get-user-resource` 按 `TotalCount` 分页拉全（上限 200 页兜底），
  修复套餐包超过 100 个时额度计算截断。

### 调度稳定性加固（未提交，`src/proxy/token_rotator.py` + `store.py`）

- **估算无效账号不触发阈值切号**：最近一次容量快照解析失败的账号，`deduct_quota` 不用它触发
  阈值切号——宁可多用一个号，也不把「剩余额度=0」的解析失败误判成耗尽。真实耗尽由上游
  429/14008 → `mark_disabled("quota")` 兜底。
- **切号日志锁外写**：`add_switch_log`（store 同步 sqlite）参数在锁内收集、锁外落库，
  不卡网关事件循环；原因优先取 `_pending_switch_reason`（on_disable 等不走
  `mark_disabled` 的路径），否则从冷却记录推断。不受 `log_enabled` 开关影响——切号必须留痕。
- **token 刷新失败提示**（`src/api/account_api.py`）：刷新 token 不再静默吞异常，
  失败原因落 `quota_query_last_error` 提示用户（额度失败可能是 token 失效连锁反应）。

### UI 优化（`21a028c` / `36d5fb5` + 未提交）

- **工具栏文字标签**：按钮加文字，操作更直观；筛选栏排版优化，日期/状态筛选归组右侧
  （`36d5fb5`）。
- **默认深色主题**：`theme.js` 默认 `light` → `dark`（未提交，用户仍可手动切换）。
- **窗口尺寸加大**：1340 → 1407 宽（未提交）。
- **侧边栏品牌图标**：控制台换成 WorkBuddy 官方图标（`21a028c`）。

### 导入 CodeBuddy + 导入模型下拉合并（`ab01775`）

- **新增 CodeBuddy 导入**：写入 `~/.codebuddy/models.json`，顶层用
  `{"models": [...]}` 包裹（区别于 WorkBuddy 的裸数组）；APPDATA 备用路径
  `%APPDATA%\CodeBuddy\.codebuddy\models.json`。
- **后端合并**：`export_to_workbuddy` / `export_to_codebuddy` 合并为单一
  `export_config(target, port, password)`，profiles 映射表统一控制 folder / app 名 /
  wrap 结构（裸数组 vs models 包裹），消除重复代码。
- **前端合并**：两个导入按钮合并为「导入模型」下拉组（`.sync-group`），
  展开选 WorkBuddy / CodeBuddy，点外自动关闭；`exportToIDE(ide)` 统一函数 +
  两个一行包装，共享 loading/confirm 逻辑。

### 配置字段对齐（修复图标不显示，`ab01775`）

- **问题**：导入 WorkBuddy 的自定义模型不显示图标。
- **根因**：`vendor` 为 `"Custom"`，WorkBuddy 内部按 vendor 匹配图标资源，
  `"Custom"` 不在映射表。
- **修复**：`vendor` 改 `"Gateway"`；`reasoning` 删冗余 `canDisableThinking`，
  加 `defaultEffort: "high"`（与官方配置对齐）。已验证图标正常显示。

### 额度估算防陈旧快照覆盖（`b732956`，并入 v1.0.7 发布）

> 该提交实为 v1.0.6 构建后修复，技术细节已记入 v1.0.6 段。此处仅标注发布归属。

---

## [v1.0.6] — 2026-08-01

账号管理增强 + 封禁验活闭环。从 v1.0.5 之后的 13 个提交（`69d4288` ~ `2881d51`）
汇总，分两阶段：

- **构建前**（`69d4288` ~ `0a962e4`）：OAuth 竞态根治、封禁验活闭环、账号多选
  批量操作、搜索/标签/回收站、自定义日历日期筛选、全局用量统计、签到自动刷新额度。
- **构建后稳定化与补强**（`2061b95` ~ `2881d51`）：打包版资源加载三连修复、
  账号客户端指纹配置、日历筛选交互打磨、账号地区筛选、回收站日期筛选/时间分组、
  「检测」文案统一改「验活」、额度估算防陈旧快照覆盖。

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

### 打包版资源加载三连修复（`b880c03` `033b47c` `2061b95`）

PyInstaller 打包后三处致命资源加载问题，均集中在 `main.py` / `build.bat`：

- **主题闪变根治**（`b880c03`）：原主题持久化靠运行时加载 `theme.js` 文件设置，
  frozen 版该文件在 `_MEIPASS` 临时解压目录下无法可靠写入/加载，首帧用默认主题
  渲染后切换造成「深色一闪」。方案：`main.py` 新增 `_build_theme_inline`（从
  settings/localStorage 读主题生成内联 `<script>`）+ `_prepare_frozen_html`（把
  `theme.js` 标签替换为内联脚本，注入 `<base>` 指向解压目录），绕开 theme.js
  文件机制。
- **base href 指向错误**（`033b47c`）：`_prepare_frozen_html` 的 `<base href>` 此前
  指向 `_MEIPASS` 根目录，而 index.html 实际在 `src/gui/` 子目录，相对路径资源
  （style.css / animations.css / vue.prod.js / icons）全 404。修复：base 改为指向
  `src.parent`（index.html 所在目录）。
- **animations.css 漏打包**（`2061b95`）：`build.bat` 的 PyInstaller spec 漏加
  `animations.css`，运行时全部 modal 无过渡动画，Vue 过渡类残留导致弹窗同时显示
  无法操作。修复：`build.bat` 补进该文件。updater 版本号同步升 v1.0.6。

### 账号客户端指纹配置（`80b2260`）

- **背景**：所有账号共用同一套客户端请求头（IDE 版本 / User-Agent / stainless 包
  版本），大批同指纹请求易被风控聚类。
- **数据层**：`Account` 新增 `fingerprint` 字段（`models/account.py`）；
  `store.py` 迁移加 `fingerprint TEXT` 列，新增 `save_fingerprint` 独立 UPDATE
  （不与 upsert 快照并发回写耦合，避免被覆盖）。
- **请求层**：`build_headers`（`api_client.py`）接受 `fingerprint` 参数，仅按白名单
  `FINGERPRINT_FIELDS`（`X-IDE-Version`/`User-Agent`/`x-stainless-package-version`/
  `x-stainless-runtime-version`）覆盖，其余头一律不接受覆盖防乱写。
  `generate_fingerprint` 仅从合法版本号池随机（IDE/CLI/stainless/Node-runtime 各
  8/8/6/8 个版本），平台/架构/语言固定，保证组合合法。
- **UI**：单账号「更多→指纹」弹窗 + 批量操作区「指纹」按钮，支持一键随机（每账号
  一套）或手动填同一套；批量操作区 `flex-basis:100%` 独立换行防溢出。

### 日历筛选交互打磨（`1801778`）

- 年份切换：日历头新增上一年/下一年双箭头（`calPrevYear`/`calNextYear`），原仅月
  切换，跨年选日期要点 12 次。
- 年份显示：`dateFilterLabel` 指定日期从「M月D日」补全为「YYYY年M月D日」。
- 菜单/日历互斥：`toggleDateFilter` 开下拉收日历；`pickCustomDate` 改显式开/关
  （原 `calOpen = !calOpen` 在菜单打开时逻辑反转）。
- 清除按钮：筛选激活时触发器内出 `cal-clear-btn`（圆形 ×，hover 变红）。
- 点外关闭：全局 click 监听收日期下拉与日历。

### 账号地区筛选 + 回收站日期筛选/时间分组（`2881d51`）

- **地区筛选**：`filteredAccounts` 新增 `regionFilter` 分支，按账号 ID 前缀判定
  （`852*`=香港号、11 位纯数字非 852=中国号），纯前端无后端改动。
- **回收站日期筛选**：弹窗新增日期下拉（全部时间/今天/昨天/近 7 天/指定日期），
  复用主日历组件（`calYear/calMonth/calWeeks`），独立选中态
  `rCalPickDay`/`rCalIsSelected`，不污染主筛选 `filterDate`。
- **时间分组**：`groupedDeletedAccounts` 按删除时间分今天/昨天/更早，带分隔线与
  计数，便于批量恢复同批删除的账号。
- **文案统一**：全链路（`app.py` docstring + `index.html` 按钮/弹窗/toast +
  `style.css` 注释）「检测」统一改「验活」，语义更准确。函数名（`detect_*`）保持
  不变避免破坏 API 契约。

### 额度估算持久化与防陈旧快照覆盖（`b732956`）

- **问题**：额度估算 `_estimated_remain` 纯内存，被动 reload（如别的账号封号连带
  触发）时用 DB 的 `quota_raw`（陈旧快照）重算，可能比内存已扣减值高，估算被抬高
  → 阈值不触发 → 用超。
- **calibrate 开关**：`_refresh_estimates(calibrate)` 被动 reload 取
  `min(新算值, 内存已有值)` 防抬高；手动刷新额度后的 reload（`calibrate=True`）才
  直接覆盖校准。`app.py` 所有主动刷新路径（导入/OAuth/refresh/detect）改传
  `calibrate=True`。
- **持久化**：新增 `persist_estimates`（网关 shutdown 时内存估算一次性落库 settings
  表）+ `_restore_estimates`（重启 reload 恢复）。运行期间仍纯内存零 IO，重启不丢
  扣减记录、不被陈旧 DB 抬高。
- **UI 修复**：工作态卡片（`.card.active`）激光束 `z-index` 提升原先对 `> *` 生效，
  把 absolute 定位的复选框卷进相对定位导致错位；改 `> *:not(.card-checkbox)` 排除，
  复选框单独 `z-index:3`。

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
