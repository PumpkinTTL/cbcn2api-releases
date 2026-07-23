# 代理网关修复记录

## 问题

代理网关运行 2~3 次请求后整体冻结，WorkBuddy/opencode 等客户端连接失败或无限思考。

## 根因与修复

### 1. 子进程管道堵塞（致命）
`app.py` 用 `subprocess.PIPE` 接收 uvicorn 的 stdout/stderr，但从不读取。管道缓冲区写满（~4KB）后整个代理进程阻塞在 I/O 写入上，事件循环冻死。

**修复**：`stdout=DEVNULL`，`stderr` 重定向到临时日志文件，`proxy_stop` 时自动清理。

### 2. `resp.aiter_text()` 重复调用（致命）
`_stream_with_failover` 的 peek 阶段和 `_combined()` 各调了一次 `resp.aiter_text()`，httpx 抛 `StreamConsumed` 崩溃，客户端收到 Connection reset。

**修复**：只创建一个 `text_iter`，peek 和后续流共用。

### 3. 上游连接泄漏
成功路径的 `resp` 从未调用 `aclose()`，连接池耗尽后新请求卡死。

**修复**：成功路径加 `try/finally: await resp.aclose()`；`_non_stream_chat` 加 `try/finally: await gen.aclose()`。

### 4. SSE 规整（对照 9router passthrough）
- 删除空 `tool_calls: []`（CodeBuddy 每块都带，AI SDK 误判）
- 补全 `object`/`created` 字段
- `hasValuableContent` 过滤无实质内容的空块
- 其余字段（reasoning_content、finish_reason、role 等）原样透传

### 5. 超时与 keepalive
- 超时从 300s 降至 60s，上游异常时快速释放
- `max_keepalive_connections` 保持 20

## 涉及文件
- `src/gui/app.py` — 子进程管道修复 + 日志清理
- `src/proxy/proxy_server.py` — StreamConsumed 修复 + 连接泄漏修复 + SSE 规整
- `src/proxy/api_client.py` — reasoning_effort 处理（对照 9router）
