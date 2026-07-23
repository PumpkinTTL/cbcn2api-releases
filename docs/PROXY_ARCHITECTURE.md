# cbcn2api 代理网关 — 架构说明与重大发现

> 本文档记录了 cbcn2api 反向代理网关的核心实现细节，包括通过 MITM 抓包发现的真实 API 架构。

## 一、重大发现：真实的 Chat API 域名

### 背景

原版 [codebuddy2api](https://github.com/xueyue33/codebuddy2api) 项目将所有请求发送到 `https://www.codebuddy.ai`。我们最初沿用了这个设计，但发现：

- `www.codebuddy.ai` 是**国际域名**，CN 账号的 token 在此域名返回 `401 invalid_token`
- `www.codebuddy.cn` 是**中国域名**，但 `/v2/chat/completions` 对所有模型返回 `model service info not found`

### 抓包结果

通过 mitmproxy 对 WorkBuddy IDE（v5.2.6）进行抓包，发现：

**真实的 Chat API 端点是 `copilot.tencent.com`，不是 codebuddy.ai 或 codebuddy.cn。**

WorkBuddy IDE 的所有 API 请求（包括 chat、billing、checkin、auth）都发往 `copilot.tencent.com`。

### 域名对照

| 域名 | 用途 | Chat API |
|------|------|----------|
| `www.codebuddy.ai` | 国际版官网/认证 | codebuddy2api 使用，CN token 无效 |
| `www.codebuddy.cn` | 中国版官网 | 无 chat 端点 |
| **`copilot.tencent.com`** | **真实 API 后端** | **✅ CN token 有效，chat 正常** |
| `download.codebuddy.cn` | 资源/更新下载 | — |
| `www.workbuddy.cn` | WorkBuddy 官网 | — |

## 二、可用模型列表

从 `GET /v3/config` 接口抓取到的官方模型列表：

| 模型 ID | 说明 |
|---------|------|
| `auto` | 自动选择 |
| `hy3` | 腾讯混元 3 |
| `glm-5.2` | 智谱 GLM-5.2 |
| `glm-5.1` | 智谱 GLM-5.1 |
| `glm-5v-turbo` | 智谱 GLM-5V Turbo（视觉） |
| `minimax-m3` | MiniMax M3 |
| `kimi-k3-1` | Kimi K3.1 |
| `kimi-k2.7` | Kimi K2.7 |
| `kimi-k2.6` | Kimi K2.6 |
| `deepseek-v4-flash` | DeepSeek V4 Flash（快速） |
| `deepseek-v4-pro` | DeepSeek V4 Pro（增强） |

> **注意**：模型名全部小写。客户端传 `Deepseek-V4-Flash` 会报 `model service info not found`，必须用 `deepseek-v4-flash`。

## 三、请求格式

### Chat Completions

```
POST https://copilot.tencent.com/v2/chat/completions
```

### 必需 Headers

```
Host: copilot.tencent.com
Authorization: Bearer <access_token>
X-User-Id: <uid>
X-Product: SaaS
X-IDE-Type: WorkBuddy
X-IDE-Name: WorkBuddy
X-IDE-Version: 5.2.6
X-Agent-Intent: craft
X-Agent-Purpose: conversation
X-Private-Data: false
x-codebuddy-request: 1
x-requested-with: XMLHttpRequest
x-stainless-arch: x64
x-stainless-lang: js
x-stainless-os: Windows
x-stainless-runtime: node
Content-Type: application/json
Accept: application/json
User-Agent: WorkBuddy/5.2.6 WorkBuddy/5.2.6 CLI/2.106.4
```

### 可选 Headers（会话追踪）

```
X-Conversation-ID: <uuid>
X-Conversation-Request-ID: <hex32>
X-Conversation-Message-ID: <hex32>
X-Request-ID: <hex32>
```

### Payload 格式

```json
{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "你好"}
  ],
  "stream": true
}
```

> CodeBuddy 后端**强制 stream=true**，即使客户端请求非流式，proxy 也会以 stream 模式请求后端，然后聚合成完整响应返回。

### 响应格式（SSE 流式）

```
data: {"id":"gen-xxx","model":"deepseek-v4-flash","object":"chat.completion.chunk","created":...,"choices":[{"index":0,"delta":{"role":"assistant","content":"你","reasoning_content":""},"finish_reason":""}]}

data: {"id":"gen-xxx","model":"deepseek-v4-flash","object":"chat.completion.chunk","created":...,"choices":[{"index":0,"delta":{"role":"assistant","content":"好"},"finish_reason":""}]}

data: [DONE]
```

## 四、代理架构

### Agent 多轮工具调用兼容

Agent 循环由客户端驱动。网关必须保证第一轮模型返回的 `assistant.tool_calls` 和
`finish_reason: "tool_calls"` 能被客户端识别，客户端执行工具后会在下一轮提交
`role: "tool"`、`tool_call_id` 和工具结果。

网关处理规则：

- 请求体除模型别名和 `stream=true` 外完整透传，不能丢弃 `tools`、`tool_choice`、
  `assistant.tool_calls`、`role: "tool"` 或 `tool_call_id`
- 删除 CodeBuddy 每个空增量中附带的 `tool_calls: []`，避免 AI SDK 提前结束 reasoning
- 真实工具调用按 `tool_calls[].index` 关联增量参数，支持并行工具调用
- 发现真实工具调用时规范化 `finish_reason` 为 `tool_calls`
- SSE 终止标记 `data: [DONE]` 恰好发送一次
- 非流式客户端仍以上游流式请求执行，再聚合成标准 OpenAI JSON

### 上游客户端身份

`api_client.py` 中硬编码保留两套上游请求头：

- `workbuddy`：实际抓包得到的 WorkBuddy 5.2.6 请求头，当前默认
- `codebuddy_cli`：参考 9router 的 `CLI/2.108.1 CodeBuddy/2.108.1` 请求头

通过修改 `DEFAULT_UPSTREAM_CLIENT` 常量切换；当前值为 `workbuddy`。

```
客户端 (OpenAI 兼容)
      │
      ▼
cbcn2api Proxy (FastAPI, 127.0.0.1:8001)
      │
      ├── /v1/chat/completions  → 转发到 copilot.tencent.com
      ├── /v1/models            → 返回模型列表
      ├── /health               → 健康检查
      ├── /proxy/info           → 代理状态
      │
      ▼
copilot.tencent.com (腾讯云代码助手后端)
```

### 文件结构

```
src/proxy/
├── __init__.py
├── api_client.py      # API 客户端：域名、headers、payload 构建
├── proxy_server.py     # FastAPI 服务：路由、流式转发、非流式聚合
└── token_rotator.py    # 凭证轮换：从 SQLite 加载账号，round-robin
```

### 关键配置

- **上游域名**：`copilot.tencent.com`（硬编码在 `api_client.py`）
- **httpx 客户端**：`trust_env=False`（直连，不走系统代理；`copilot.tencent.com` 可直接 DNS 解析）
- **凭证来源**：SQLite 数据库（`~/.cbcn2api/accounts.db`），通过 `store.list_accounts()` 加载
- **认证**：Bearer token，密码从环境变量 `CBCN_PROXY_PASSWORD` 读取

## 五、Token 说明

### Token 来源

cbcn2api 通过 `www.codebuddy.cn` 的 plugin auth 流程获取 token：

1. `POST /v2/plugin/auth/state?platform=CLI` → 获取 state 和登录链接
2. 用户浏览器登录授权
3. `GET /v2/plugin/auth/token?state=xxx` → 获取 accessToken

### Token 有效性

- 此 token 对 `copilot.tencent.com` 的所有 API 有效（chat、billing、checkin）
- 此 token 对 `www.codebuddy.ai` **无效**（返回 `invalid_token`，不同 Keycloak realm）
- Token issuer：`https://www.codebuddy.cn/auth/realms/copilot`

### Token 刷新

```
POST https://www.codebuddy.cn/v2/plugin/auth/token/refresh
Headers:
  Authorization: Bearer <access_token>
  X-Refresh-Token: <refresh_token>
```

## 六、与原版 codebuddy2api 的差异

| 对比项 | codebuddy2api（原版） | cbcn2api（本项目） |
|--------|----------------------|-------------------|
| 目标用户 | 国际版 | 中国版 |
| Chat API 域名 | `www.codebuddy.ai` | `copilot.tencent.com` |
| 认证域名 | `www.codebuddy.ai` | `www.codebuddy.cn` |
| Host header | `www.codebuddy.ai` | `copilot.tencent.com` |
| X-Domain header | `www.codebuddy.ai` | 无（不需要） |
| 模型名 | `claude-4.0, gpt-5, auto-chat...` | `deepseek-v4-flash, hy3, glm-5.2...` |
| IDE 标识 | `CLI` | `WorkBuddy` |
| 凭证存储 | JSON 文件 | SQLite 数据库 |
| 管理界面 | Web UI | pywebview 桌面 GUI |

## 七、使用方法

### 启动代理

在 GUI 面板中设置端口和密码，点击"启动"。

### OpenAI 客户端配置

```python
from openai import OpenAI

client = OpenAI(
    api_key="your_proxy_password",
    base_url="http://127.0.0.1:8001/v1"
)

response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)
```

### curl 示例

```bash
# 非流式
curl -X POST http://127.0.0.1:8001/v1/chat/completions \
  -H "Authorization: Bearer your_proxy_password" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -X POST http://127.0.0.1:8001/v1/chat/completions \
  -H "Authorization: Bearer your_proxy_password" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 八、故障排除

| 错误 | 原因 | 解决 |
|------|------|------|
| `model service info not found` | 模型名大写或不存在 | 用小写模型名，如 `deepseek-v4-flash` |
| `401 invalid_token` | token 过期或域名不对 | 确保走 `copilot.tencent.com`，检查 token 是否有效 |
| `403 request illegal` | 缺少必需 headers | 确保 `x-codebuddy-request: 1` 等 headers 齐全 |
| 端口被占用 | 其他进程占用端口 | 换一个端口，或在 GUI 中看到友好提示 |
| DNS 解析失败 | 系统代理干扰 | proxy 使用 `trust_env=False` 直连，不受系统代理影响 |
