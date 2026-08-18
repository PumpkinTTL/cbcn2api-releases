# 授权系统架构文档

## 概述

cbcn2api（AI Gateway 网关客户端）的授权系统由两部分组成：
- **lic-admin**：授权管理后台（FastAPI + SQLite），管理产品/在线激活码/开关
- **cbcn2api**：网关客户端，启动时查询授权开关，走激活/验证流程

> **离线授权码机制已移除**（安全原因：本地验签密钥进了客户端二进制，
> 存在被提取伪造激活码的风险）。历史实现见 git 分支 `backup/offline-license-v1.1.2`。
> 当前为**纯在线校验**：授权状态完全由服务端裁决，客户端不含任何签发/验签密钥。

## 激活码

| 类型 | 格式 | 特征 | 生成端 |
|---|---|---|---|
| **在线激活码** | `PREFIX-XXXXXXXXXXXX`（前缀-12位hex） | 需联网激活/验证，机器码绑定，服务端管理状态（吊销/禁用/过期即时生效） | lic-admin「激活码」页 |

客户端通过 `POST /api/v1/activate` / `POST /api/v1/verify` 与服务端交互。

## 授权开关（按产品）

lic-admin 的 `products` 表有 `enable_license_check` 字段（0/1）。
客户端启动时查 `GET /api/v1/config?id=<产品ID>` 获取开关：
- `enabled=false` → 免授权直接可用
- `enabled=true` → 走激活/验证流程

**产品 ID 绑定**：`APP_ID`（cbcn2api 硬编码）= lic-admin 的 `products.id`（当前 AI Gateway = 1）。
在线码校验时客户端传 `product_id`，服务端比对激活码所属产品。

## 授权流程

### 启动流程（check_license）

```
cbcn2api 启动
  → _resolve_license_enabled()
    → remote_license_enabled()  查远端 config?id=APP_ID
      ├ 成功 → enabled=true/false
      └ 失败（断网）→ 兜底 True（保守走授权，后续在线校验也会失败，等价拒绝放行）
  → if not enabled → 直接放行（licensed=true）
  → if enabled → status()
    → load_code() 读 license.dat 缓存
      ├ 无缓存 → 未激活，显示激活界面
      └ 有缓存 → verify(code)
        └ 在线码 → _verify_online：调 /api/v1/verify
          ├ 200 → 授权有效
          ├ 403 → 未授权/已禁用/已过期
          └ 断网 → "无法连接授权服务器"（拒绝放行，无离线兜底）
```

校验只发生在两个时点：**启动时**（check_license）与**启动代理时**（proxy_start）。
运行途中不做周期性心跳——代理跑起来之后服务器宕机不影响使用，
只有重启软件/重启代理才会再次触发校验。

### 激活流程（activate）

用户在激活界面输入码 → `doActivate()` → `pywebview.api.activate(code)`：

```
activate(code)
  → _activate_online()
      ├ POST /api/v1/activate（code + machine_code + product_id + device_info）
      ├ 200 → save_code 持久化 license.dat → 前端 reload
      ├ 403/404 → 透传错误消息
      └ 断网 → "无法连接授权服务器"
```

### 机器码生成

优先 Windows MachineGuid（HKLM\SOFTWARE\Microsoft\Cryptography，
系统安装时生成、重装系统才变，不随网卡/VPN/虚拟网卡漂移）：
`MID-` + SHA256("MG-" + MachineGuid)[:16].upper()。
读取失败时退回网卡 MAC 哈希。

## 状态流转

```
unused ──activate──▶ active ──disable──▶ disabled ──enable──▶ active
                         │                                        │
                      到期/verify                              (有绑定→active)
                         │                                  无绑定→unused
                       expired
```

- `disable`：active/disabled/unused → disabled（保留 machine_code）——客户端下次校验即被拒
- `enable`：disabled → 有 machine_code 恢复 active；无 machine_code 恢复 unused
- `expired`：verify 时发现 expires_at 过期自动标记

## 内部豁免版（INTERNAL_BUILD）

`src/build_flags.py` 的 `INTERNAL_BUILD` 常量（默认 False）：

- **正式发行包**（`build_nuitka.bat`）：恒为 False，走完整授权流程
- **内部豁免包**（`build_internal.bat`）：打包前临时翻成 True 编译进二进制，
  跳过全部授权校验（`remote_license_enabled` 直接返回 False），构建完自动还原

豁免由**编译期常量**决定（不是运行期环境变量/配置文件）——正式版 exe
无法通过改环境变量等方式触发豁免。内部包仅限内部使用，严禁外发。

另有一条**开发豁免**（`_dev_bypass`）：仅「从源码运行」且 `GW_DEV=1`
（`.env`）时生效，打包版被编译标志硬卡，永不触发。

## 远端服务器地址

- **开发模式**（非打包）：`http://127.0.0.1:8022`
- **打包版**（frozen）：`https://license.bitlesu.com`
- 环境变量 `LIC_SERVER` 始终可覆盖

## 关键文件

### lic-admin
| 文件 | 职责 |
|---|---|
| `server.py` | 全部 API 端点（管理 + 客户端） |
| `db.py` | SQLite 建表/迁移/查询 |
| `static/index.html` | 管理后台 Vue3 前端 |

### cbcn2api
| 文件 | 职责 |
|---|---|
| `src/license.py` | 授权核心（开关查询/激活/验证，纯在线） |
| `src/build_flags.py` | 打包期构建标志（INTERNAL_BUILD 豁免开关） |
| `src/gui/app.py` | pywebview IPC（check_license/activate/get_machine_code） |
| `src/gui/index.html` | 激活界面 + 关于页授权状态 |

## 响应签名（防中间人伪造）

攻击者控制自己电脑时可解密本机 HTTPS（Fiddler/mitmproxy + 自装 CA）伪造服务端响应
（如 config `enabled:false` 直接免授权）。因此三个客户端接口启用 **Ed25519 响应签名**：

- 每次请求带随机 `nonce`（32 hex），服务端对 `nonce + "|" + 响应体规范JSON` 签名，
  响应附 `_nonce`/`_sig`
- 客户端内嵌公钥（`src/license.py PUBKEY_HEX`）验签：签名无效 / nonce 不匹配 /
  缺签名 → 一律按连接失败拒绝放行（防伪造 + 防重放）
- 公钥进二进制是安全的：逆向提取公钥也伪造不出签名（需服务端私钥）
- 私钥：lic-admin `data/signing_key.hex`（或 `LIC_ADMIN_SIGNING_KEY` 环境变量）；
  **部署新服务器必须同步私钥，否则新客户端全部验签失败**
- 轮换密钥 = 服务端换 seed + 客户端换公钥 + 重发客户端
- heartbeat 响应暂未签名（TODO）：伪造心跳 200 只能掩盖运行途中吊销，
  重启时的签名 verify 必查，非授权旁路

规范JSON = `json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
（不含 `_sig`/`_nonce`，两端一致）。

## 心跳与在线追踪

客户端授权有效后启动后台心跳线程（`src/gui/app.py start_license_heartbeat`），
每 5 分钟调 `POST /api/v1/heartbeat`（带 `code`/`machine_code`/`app_version`）：

- **ok**：服务端刷新 `last_seen_at`/`app_version`（在线状态与版本分布可见），
  响应可带最新 `announcement` → 前端弹公告（同内容只弹一次，localStorage 去重）
- **rejected**（服务端明确拒绝：禁用/过期/版本报废）→ 前端锁回激活界面，
  吊销在运行途中即时生效，不必等重启
- **unreachable**（断网/服务器宕机）→ 只重试不锁定：心跳不做可用性惩罚，
  真正的授权裁决仍在启动时的签名 verify

## 版本上报与门槛

verify/activate/heartbeat 均上报 `app_version`（取 `src/updater.APP_VERSION`）。
服务端按产品配置做硬门槛（`_version_gate`）：

- `min_version`：上报版本低于最低运行版本 → 403 拒绝（强制报废手段）
- `blocked_versions`：版本黑名单精确匹配 → 403
- `block_unversioned`：拦截不上报版本的老客户端（存量旧版一并报废，慎用）

403 消息经授权失败提示透传给用户（"版本过旧…请升级"）。
verify 响应若带回 `update_required`/`latest_version`（软门槛，当前服务端未启用），
前端会弹提示并自动打开检查更新。

## 公告（announcement）

lic-admin 后台可发产品专属或全局公告（`announcements` 表）。
verify 成功响应与心跳响应都会带回当前生效公告，前端 `showAnnouncement`
弹「公告」模态框，同一内容只弹一次。

## 安全要点

- 客户端不含任何签发/验签密钥——伪造激活码必须攻破服务端
- 响应签名（Ed25519）+ 随机 nonce：中间人无法伪造/重放服务端响应
- `LIC_SERVER` 环境变量覆盖仅开发模式生效——打包版硬卡，假服务器秒破无效
- 在线码传输需 HTTPS（生产环境）
- 吊销/禁用/过期由服务端状态裁决：启动时签名 verify + 运行中心跳，即时生效
- 断网 = 启动拒绝放行（无离线兜底）；运行中心跳不可达不锁定（可用性优先）
- 机器码不上报敏感信息
- 旧库中遗留的 `offline_license_records` 表不再读写（无害残留）

