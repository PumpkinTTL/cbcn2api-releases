# CodeBuddy / WorkBuddy 请求指纹完整参考文档

> 更新时间：2026-08-13  
> 数据来源：公开反代/代理项目源码逆向整理  
> 主要参考项目：
> - Sliverkiss/workbuddy2api（headers.go）
> - Sliverkiss/cpa-plugin（workbuddy/main.go）
> - xueyue33/codebuddy2api（codebuddy_api_client.py）
> - router-for-me/CLIProxyAPIPlus（codebuddy_auth.go）
> - liubaicai/workbuddy2api 等社区实现

**重要说明：**
1. 官方未公开完整请求指纹规范，本文全部来自社区逆向与实际可用反代实现。
2. **CodeBuddy CLI**、**CodeBuddy IDE/插件/Desktop**、**WorkBuddy** 是三个不同产品形态，后端高度共用但客户端标识不完全相同。
3. 目前活跃反代几乎全部用 **CLI 指纹** 去打后端，成功率最高。
4. 版本号会随官方更新变化，建议定期用真实客户端抓包校准。

---

## 一、产品与后端关系总览

| 产品 | 形态 | 包名/入口 | 主要后端（国内） | 主要后端（国际） | 社区反代主流指纹 |
|------|------|-----------|------------------|------------------|------------------|
| **CodeBuddy CLI** | 命令行工具 | `@tencent-ai/codebuddy-code` / `codebuddy` / `cbc` | `https://copilot.tencent.com` | `https://www.codebuddy.ai` | CLI 指纹（最成熟） |
| **CodeBuddy IDE / 插件 / Desktop** | 独立 IDE / VS Code 插件 / Electron | CodeBuddy IDE、VS Code 插件 | 同上 | 同上 | 公开完整指纹极少，多数套用 CLI |
| **WorkBuddy** | 桌面智能体工作台 | WorkBuddy 桌面端 | `https://copilot.tencent.com` | `https://www.workbuddy.ai` | 几乎全部复用 CLI 指纹 |

### 关键域名对照

| 用途 | 国内（CN） | 国际（Global） |
|------|------------|----------------|
| 主站 / Origin / Referer | `https://www.codebuddy.cn` | `https://www.workbuddy.ai` 或 `https://www.codebuddy.ai` |
| API 网关 | `https://copilot.tencent.com` | `https://www.workbuddy.ai` |
| 登录相关 | `copilot.tencent.com` | `workbuddy.ai` 体系 |

> **注意**：Global 账号的 JWT 发到 `copilot.tencent.com` 会被 APISIX 直接 401，必须走 `www.workbuddy.ai`。

---

## 二、CodeBuddy CLI 完整请求指纹

这是目前社区验证最多、反代最常用的一套。

### 2.1 核心常量

```text
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
X-Product: SaaS
```

常见版本写法：
- `CLI/2.63.2 CodeBuddy/2.63.2`（当前主流）
- `CLI/1.0.7 CodeBuddy/1.0.7`
- `CLI/1.0.8 CodeBuddy/1.0.8`

建议：尽量贴近你本机官方 CLI 的真实版本。

### 2.2 Common Headers（所有接口共用基础头）

```http
Content-Type: application/json
Accept: application/json, text/plain, */*
X-Requested-With: XMLHttpRequest
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
Origin: https://www.codebuddy.cn
Referer: https://www.codebuddy.cn/
```

国际版把 Origin/Referer 换成：
```http
Origin: https://www.workbuddy.ai
Referer: https://www.workbuddy.ai/
```

### 2.3 Chat Headers（对话请求，最重要）

在 Common 基础上增加账号相关头。官方 CLI 在字段缺失时使用 **X-No-*** 约定。

```http
# === Common ===
Content-Type: application/json
Accept: application/json, text/plain, */*
X-Requested-With: XMLHttpRequest
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
Origin: https://www.codebuddy.cn
Referer: https://www.codebuddy.cn/

# === 账号认证 ===
Authorization: Bearer <access_token>
# 若无 token：X-No-Authorization: 1

X-User-Id: <uid>
# 若无：X-No-User-Id: 1

X-Enterprise-Id: <enterprise_id>
# 若无：X-No-Enterprise-Id: 1

X-Domain: <domain>
# 若无：X-No-Department-Info: 1

X-Product: SaaS

# === 推荐额外字段 ===
X-Request-ID: <uuid>
```

**安全红线（多项目反复强调）：**
- **Chat 请求中绝对不要携带 `X-Refresh-Token`**
- 否则长效凭证会进入上游请求日志，极易触发风控/封号

### 2.4 增强版 CLI 指纹（部分项目使用，提高真实度）

```http
# 在 Chat Headers 基础上再加：

X-IDE-Type: CLI
X-IDE-Name: CLI
X-IDE-Version: 1.0.7
X-Agent-Intent: craft

X-Conversation-ID: <uuid>
X-Conversation-Request-ID: <32位hex>
X-Conversation-Message-ID: <去掉横线的uuid>
X-Request-ID: <去掉横线的uuid>

# Stainless 风格（模拟 Node SDK）
x-stainless-arch: x64
x-stainless-lang: js
x-stainless-os: Windows          # 或 Darwin / Linux
x-stainless-package-version: 5.10.1
x-stainless-retry-count: 0
x-stainless-runtime: node
x-stainless-runtime-version: v22.13.1

X-Domain: www.codebuddy.ai       # 国际示例
Host: www.codebuddy.ai
```

来源参考：`xueyue33/codebuddy2api` 的 `generate_codebuddy_headers()`。

### 2.5 Refresh Headers（Token 刷新专用）

仅用于 `/v2/plugin/auth/token/refresh`。

```http
# Common 头 +
X-Refresh-Token: <refresh_token>           # 只允许出现在这里
X-Auth-Refresh-Source: workbuddy           # 或 plugin / CLI
X-Enterprise-Id: <enterprise_id>           # 有则带
Authorization: Bearer <当前 access_token>
X-User-Id: <uid>
X-Product: SaaS
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
X-Request-ID: <uuid>
```

不同项目 `X-Auth-Refresh-Source` 取值：
- `workbuddy`
- `plugin`
- `CLI`

建议与你模拟的客户端类型保持一致。

### 2.6 Auth / Login 相关 Headers

用于 `/v2/plugin/auth/state`、`/v2/plugin/auth/token`、`/v2/plugin/login/account` 等。

```http
Accept: application/json, text/plain, */*
Content-Type: application/json
X-Requested-With: XMLHttpRequest
X-Domain: copilot.tencent.com
X-No-Authorization: true
X-No-User-Id: true
X-No-Enterprise-Id: true
X-No-Department-Info: true
X-Product: SaaS
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
X-Request-ID: <uuid>
```

常见接口路径：
```text
/v2/plugin/auth/state?platform=CLI
/v2/plugin/auth/token?state=...
/v2/plugin/login/account?state=...
/v2/plugin/auth/token/refresh
/v2/chat/completions
/console/enterprises/personal/models
```

### 2.7 Billing / 积分相关 Headers（部分项目）

```http
Authorization: Bearer <access_token>
Accept: application/json
Content-Type: application/json
X-User-Id: <uid>
X-Enterprise-Id: <enterprise_id>
X-Tenant-Id: <enterprise_id>          # 有的实现会带
X-Domain: <domain>
```

---

## 三、WorkBuddy 请求指纹

### 3.1 结论先行

目前公开的反代实现中，**WorkBuddy 与 CodeBuddy CLI 使用同一套核心指纹**。

原因：
1. WorkBuddy 与 CodeBuddy 共享同一套后端体系与账号积分。
2. 桌面端登录态最终打到的仍是 `copilot.tencent.com` / `workbuddy.ai` 的 v2 接口。
3. 社区项目（workbuddy2api、cpa-plugin 等）直接复用 `CLI/x.x.x CodeBuddy/x.x.x` + `X-Product: SaaS`。

### 3.2 WorkBuddy 推荐完整头（与 CLI 对齐）

**Chat：**

```http
Content-Type: application/json
Accept: application/json, text/plain, */*
X-Requested-With: XMLHttpRequest
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
Origin: https://www.codebuddy.cn                 # 国内
Referer: https://www.codebuddy.cn/
X-Product: SaaS

Authorization: Bearer <access_token>             # 或 X-No-Authorization: 1
X-User-Id: <uid>                                 # 或 X-No-User-Id: 1
X-Enterprise-Id: <enterprise_id>                 # 或 X-No-Enterprise-Id: 1
X-Domain: <domain>                               # 或 X-No-Department-Info: 1
X-Request-ID: <uuid>
```

**国际版：**
```http
Origin: https://www.workbuddy.ai
Referer: https://www.workbuddy.ai/
```
后端 base 使用：`https://www.workbuddy.ai`

**Refresh：**
```http
# Common + 
X-Refresh-Token: <refresh_token>
X-Auth-Refresh-Source: workbuddy
X-Enterprise-Id: <id>
Authorization: Bearer <access_token>
X-Product: SaaS
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
```

### 3.3 WorkBuddy 桌面端登录态来源

反代通常读取本机已登录的桌面端凭证，而不是重新模拟完整登录流：

| 平台 | 常见凭证路径 |
|------|----------------|
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/` |
| Windows | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\` |
| Linux | `~/.local/share/CodeBuddyExtension/Data/Public/auth/` |

凭证中通常包含：`access_token` / `refresh_token` / `uid` / `enterpriseId` / `domain` 等。

### 3.4 与 CLI 的细微差异点

| 项目 | CLI | WorkBuddy 反代常见做法 |
|------|-----|------------------------|
| User-Agent | `CLI/x.x.x CodeBuddy/x.x.x` | 同左 |
| X-Auth-Refresh-Source | plugin / CLI | 常写 `workbuddy` |
| Origin/Referer | codebuddy.cn / codebuddy.ai | 国内 codebuddy.cn，国际 workbuddy.ai |
| 是否带 X-IDE-* | 可选增强 | 多数不加，保持简单 CLI 头 |

---

## 四、CodeBuddy IDE / 插件 / Desktop 请求指纹

### 4.1 现状说明

截至目前公开资料：

- **没有**像 CLI 那样被广泛逆向并验证的完整独立指纹集。
- IDE / 插件 / Desktop 是 Electron 或 VS Code Extension 形态，真实 UA 可能包含 Electron / VS Code 特征，但社区几乎没有完整公开抓包结果。
- 现有反代项目在需要模拟「CodeBuddy 非 CLI 形态」时，仍大量直接使用 **CLI 指纹**，且可正常工作。

### 4.2 社区可参考的「偏 IDE 风格」尝试（未充分验证）

如果一定要区分，可在 CLI 头基础上做如下调整（风险自负）：

```http
User-Agent: CodeBuddy/x.x.x          # 或带 Electron 特征
X-IDE-Type: IDE                      # 或 VSCode / Plugin
X-IDE-Name: CodeBuddy
X-IDE-Version: <版本>
X-Product: SaaS
X-Agent-Intent: craft
```

**不建议**作为生产反代首选。当前最稳妥的仍然是完整复用 **第二节的 CLI 指纹**。

### 4.3 官方本地 Gateway 相关头（仅本地服务，不是上游指纹）

CodeBuddy CLI 的 `--serve` / Gateway 模式有自己的安全头（这是本地 HTTP API，不是打腾讯上游的指纹）：

```http
X-CodeBuddy-Request: 1
Authorization: Bearer <gateway_password>
# 或 X-Access-Token: <password>
```

这与上游 API 指纹无关，不要混淆。

---

## 五、按场景整理的完整 Headers 模板

### 场景 A：Chat 对话（最常用）

```http
Content-Type: application/json
Accept: application/json, text/plain, */*
X-Requested-With: XMLHttpRequest
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
Origin: https://www.codebuddy.cn
Referer: https://www.codebuddy.cn/
X-Product: SaaS
Authorization: Bearer <access_token>
X-User-Id: <uid>
X-Enterprise-Id: <enterprise_id>
X-Domain: <domain>
X-Request-ID: <uuid>
```

无某字段时改用：
```http
X-No-Authorization: 1
X-No-User-Id: 1
X-No-Enterprise-Id: 1
X-No-Department-Info: 1
```

### 场景 B：Token Refresh

```http
Content-Type: application/json
Accept: application/json, text/plain, */*
X-Requested-With: XMLHttpRequest
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
Origin: https://www.codebuddy.cn
Referer: https://www.codebuddy.cn/
X-Product: SaaS
Authorization: Bearer <access_token>
X-User-Id: <uid>
X-Enterprise-Id: <enterprise_id>
X-Refresh-Token: <refresh_token>
X-Auth-Refresh-Source: workbuddy
X-Request-ID: <uuid>
```

### 场景 C：Auth State / 登录初始化

```http
Accept: application/json, text/plain, */*
Content-Type: application/json
X-Requested-With: XMLHttpRequest
User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
X-Domain: copilot.tencent.com
X-No-Authorization: true
X-No-User-Id: true
X-No-Enterprise-Id: true
X-No-Department-Info: true
X-Product: SaaS
X-Request-ID: <uuid>
```

### 场景 D：增强真实度（可选）

在场景 A 基础上追加：

```http
X-IDE-Type: CLI
X-IDE-Name: CLI
X-IDE-Version: 1.0.7
X-Agent-Intent: craft
X-Conversation-ID: <uuid>
X-Conversation-Request-ID: <32hex>
X-Conversation-Message-ID: <uuid无横线>
x-stainless-arch: x64
x-stainless-lang: js
x-stainless-os: Windows
x-stainless-package-version: 5.10.1
x-stainless-retry-count: 0
x-stainless-runtime: node
x-stainless-runtime-version: v22.13.1
```

---

## 六、关键接口路径速查

| 用途 | 路径 | 备注 |
|------|------|------|
| 对话 | `POST /v2/chat/completions` | 核心 |
| 刷新 Token | `POST /v2/plugin/auth/token/refresh` | 必须带 X-Refresh-Token |
| 获取 Auth State | `/v2/plugin/auth/state?platform=CLI` | 登录流 |
| 换 Token | `/v2/plugin/auth/token?state=...` | 登录流 |
| 账号信息 | `/v2/plugin/login/account?state=...` | 登录流 |
| 模型列表 | `/console/enterprises/personal/models` | 部分实现 |

---

## 七、反代落地建议（降低识别/封号风险）

1. **最低必需指纹**
   - `User-Agent: CLI/<ver> CodeBuddy/<ver>`
   - `X-Product: SaaS`
   - 正确的 Origin / Referer（匹配区域）
   - 正确的 Authorization / X-User-Id 等账号头或 X-No-* 约定

2. **必须隔离 Refresh**
   - `X-Refresh-Token` 只允许出现在 refresh 接口
   - Chat 请求严禁携带

3. **区域匹配**
   - 国内账号：`codebuddy.cn` + `copilot.tencent.com`
   - 国际账号：`workbuddy.ai`（不要把 Global JWT 打到 copilot.tencent.com）

4. **版本号**
   - 尽量与当前官方 CLI 版本接近
   - 过旧的 `CLI/1.0.x` 长期使用可能增加异常概率

5. **行为层**
   - 控制频率、多账号轮换、错误冷却
   - 仅对齐 Headers 不够，行为异常同样会触发风控

6. **最稳妥做法**
   - 用官方 CLI 真实抓一次包，完整复制 Headers
   - 再与本文模板交叉校验

---

## 八、三平台指纹对照简表

| 指纹项 | CodeBuddy CLI | WorkBuddy | CodeBuddy IDE/Desktop |
|--------|---------------|-----------|------------------------|
| User-Agent | `CLI/x.x.x CodeBuddy/x.x.x` | 同 CLI（社区主流） | 公开资料不足，多数套 CLI |
| X-Product | `SaaS` | `SaaS` | 预期 `SaaS` |
| Origin/Referer | codebuddy.cn / codebuddy.ai | codebuddy.cn / workbuddy.ai | 同左 |
| X-IDE-Type | `CLI`（增强时） | 通常不特别强调 | 可能为 IDE / VSCode（未验证） |
| X-Auth-Refresh-Source | plugin / CLI | workbuddy | 未知 |
| X-No-* 约定 | 支持 | 支持 | 预期支持 |
| 社区验证程度 | ★★★★★ | ★★★★☆（复用 CLI） | ★☆☆☆☆ |

---

## 九、参考源码位置（便于自行核对）

1. **Sliverkiss/workbuddy2api**  
   `internal/upstream/headers.go`  
   - CommonHeaders / ChatHeaders / BillingHeaders / RefreshHeaders  
   - 最干净的 CLI/WorkBuddy 头实现

2. **Sliverkiss/cpa-plugin**  
   `workbuddy/main.go`  
   - clientUA、originReferer、backendHeaders、refresh 逻辑

3. **xueyue33/codebuddy2api**  
   `src/codebuddy_api_client.py`  
   - `generate_codebuddy_headers()`  
   - 含 X-IDE-*、X-Conversation-*、x-stainless-* 增强头

4. **router-for-me/CLIProxyAPIPlus**  
   `internal/auth/codebuddy/codebuddy_auth.go`  
   - UserAgent 常量、Auth/Refresh 请求头

5. **liubaicai/workbuddy2api**  
   - 桌面端凭证读取 + 上游转发逻辑说明

---

## 十、免责与更新说明

- 本文仅整理公开逆向与社区反代实现，**不保证**与官方最新版本 100% 一致。
- 官方可能随时调整校验逻辑，请以真实抓包为准。
- 版本号、域名、额外自定义头都可能变化，建议把「抓包校准」纳入日常维护。
- 本文不提供任何绕过官方限制、滥用服务的指导，仅作技术参考。

---

**文档结束。**

如需针对某一语言（Python / Go / Node）生成可直接复制的 headers 构造函数，或只要「最小可用头」精简版，可再说明需求。
