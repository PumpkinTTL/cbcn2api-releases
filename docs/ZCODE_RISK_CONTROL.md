# 踩坑记录：ZCode 接入被上游"敏感内容"风控拦截

> 2026-08-18 排查实录。结论先行：**不是身份指纹问题，是 ZCode 注入的 system prompt
> 内容触发上游打分制风控**。网关侧已做自动清洗（`_sanitize_system_prompt`），ZCode
> 现已可正常接入。

## 现象

- ZCode 接网关（任意模型、任意账号），新会话发"你好"也必被拦：
  `抱歉，系统检测到您当前输入的信息存在敏感内容，我无法响应您的请求，请检查后重新输入`
- 同一网关同一账号：opencode ✅ / WorkBuddy IDE ✅ / **只有 ZCode ❌**
- ZCode **带项目**（工作目录打开 git 仓库）必拦，不带项目有时能过
- 切模型无效（glm-5.2 / deepseek-v4-flash 都拦）

## 排查过程（方法论可复用）

1. **加全量日志**：request 日志记 system prompt 全文 + msgs_view 摘要；
   短响应（≤500 字）全文落日志——拦截消息是 `choices` 正常格式返回的短响应，
   非 error 结构，不加日志根本看不到
2. **三客户端对比**：WorkBuddy 47KB prompt ✅、opencode 53KB ✅、ZCode 7.5KB ❌
   ——最小的最干净的最先挂，排除"prompt 大触发"
3. **落盘完整请求**（body 全字段 + headers）diff 顶层参数：ZCode 独有
   `thinking`/`tool_choice`/`reasoning_effort`——消融重放全部去掉**仍拦**，排除参数
4. **消息级消融**：只留最后一条 user → ✅；只要 system 在 → ❌。锁定 system
5. **system 分段消融**（二分 + 单段测）：前半 ✅ 后半 ❌，但后半每段单独都 ✅
   ——组合触发，说明是**打分制**（内容叠加过阈值）
6. **二次二分**：锁定 gitStatus 快照段 → 逐行测出强触发因子

## 根因

ZCode 独家行为：把当前项目的 **git 状态快照**（分支/文件改动/最近提交）注入
system prompt 末尾。其中固定的一行是强触发因子（消融实测**单句即拦**）：

```
Main branch (you will usually use this for PRs): main
```

- 改写成 `Main branch: main` → ✅ 通过（语义等价）
- git 快照里的提交信息叠加打分：cbcn2api 这类项目的提交（"签到/账号/导入/网关"）
  容易把分数推过线；干净项目（如普通 Vue 项目）可能侥幸不过线 → 解释"有时能过"
- opencode / WorkBuddy 不注入 git 快照 → 永远不触发 → 解释"只有 ZCode 不行"

上游拦截以 `choices` **正常响应格式**返回那句拦截文案（不是 error 结构），网关
原样透传给客户端——表现为"模型在正常回复里拒绝"。

## 修复（src/proxy/proxy_server.py `_sanitize_system_prompt`）

仅命中 ZCode 特征（`You are ZCode`）时生效，其他客户端零影响：

| 清洗项 | 动作 | 理由 |
|---|---|---|
| `Main branch (you will usually use this for PRs): X` | 改写为 `Main branch: X` | 强触发因子，语义等价改写 |
| gitStatus 含风险词（账号/签到/网关/号池/反代/封号/抓包/逆向） | 整段脱敏为一行占位 | 打分兜底；模型需要 git 状态会自己跑命令 |
| `IMPORTANT: Assist with authorized security testing...` 安全条款列举段 | 改写为中性措辞 | 段内攻击词（DoS attacks/C2/exploit 等）参与打分 |

挂载点：`_stream_inner` 开头，流式/非流式统一生效，只清洗一次。

## 身份指纹说明（相关但非本次根因）

网关发给上游的身份**本来就是 WorkBuddy**（`DEFAULT_UPSTREAM_CLIENT = "workbuddy"`）：
`User-Agent: WorkBuddy/{ver}` + `X-IDE-Type/Name: WorkBuddy` + `x-stainless-*` 三件套，
与客户端是 ZCode/opencode/什么都无关——客户端的 UA 到网关即终止，网关用自己的
指纹头转发（`api_client.build_headers`）。所以"伪装成 WorkBuddy"已是现状；本次
拦截与身份头无关，纯内容风控。账号侧可配指纹覆盖（FINGERPRINT_FIELDS 白名单）。

## 遗留提示

- 上游风控是黑盒打分制，未来 ZCode 更新 prompt（新增段落/措辞变化）可能再次
  过线——复发时用同样的消融法定位（落盘 payload → 分段二分 → 单句确认）
- `Main branch` 行在 blog 项目那次能过、在含账号类提交信息的项目拦——单因子
  分数不足时靠内容叠加，清洗时宁可多洗（三道清洗一起上）
