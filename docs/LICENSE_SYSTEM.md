# 授权系统架构文档

## 概述

cbcn2api（AI Gateway 网关客户端）的授权系统由两部分组成：
- **lic-admin**：授权管理后台（FastAPI + SQLite），管理产品/激活码/离线码/开关
- **cbcn2api**：网关客户端，启动时查询授权开关，走激活/验证流程

## 核心概念

### 两类激活码

| 类型 | 格式 | 特征 | 生成端 |
|---|---|---|---|
| **在线激活码** | `PREFIX-XXXXXXXXXXXX`（前缀-12位hex） | 需联网激活/验证，机器码绑定，服务端管理状态 | lic-admin「激活码」页 |
| **离线授权码** | `XXXX-XXXX-XXXX-XXXX`（4段4位 Crockford base32） | 纯本地 license_core 算法验签，无需联网 | lic-admin「离线授权码」页 |

客户端通过格式自动识别：4 段 4 位 = 离线码；否则 = 在线码。

### 授权开关（按产品）

lic-admin 的 `products` 表有 `enable_license_check` 字段（0/1）。
客户端启动时查 `GET /api/v1/config?id=<产品ID>` 获取开关：
- `enabled=false` → 免授权直接可用
- `enabled=true` → 走激活/验证流程

### 产品 ID 绑定

- **APP_ID**（cbcn2api 硬编码）= lic-admin 的 `products.id`（当前 AI Gateway = 1）
- **APP**（cbcn2api 硬编码）= license_core 密钥派生域标识（`"cbcn2api"`）
- **SECRET**（两端一致）= license_core HMAC 签名密钥

在线码校验时客户端传 `product_id`，服务端比对激活码所属产品。

## 授权流程

### 启动流程（check_license）

```
cbcn2api 启动
  → _resolve_license_enabled()
    → remote_license_enabled()  查远端 config?id=APP_ID
      ├ 成功 → enabled=true/false
      └ 失败（断网）→ 兜底 True（保守走授权，靠离线验签）
  → if not enabled → 直接放行（licensed=true）
  → if enabled → status()
    → load_code() 读 license.dat 缓存
      ├ 无缓存 → 未激活，显示激活界面
      └ 有缓存 → verify(code)
        ├ 离线码 → _check_offline_status：本地验签 + 过期检查
        └ 在线码 → _verify_online：调 /api/v1/verify
          ├ 200 → 授权有效
          ├ 403 → 未授权/已禁用/已过期
          └ 断网 → "无法连接授权服务器"
```

### 激活流程（activate）

用户在激活界面输入码 → `doActivate()` → `pywebview.api.activate(code)`：

```
activate(code)
  → _is_offline_code(code) 格式判断
    ├ 离线码 → _verify_offline()
    │   ├ 本地 is_offline_used? → 已使用则拒绝
    │   ├ license_core 验签
    │   ├ 过期检查
    │   └ mark_offline_used + save_code
    └ 在线码 → _activate_online()
        ├ POST /api/v1/activate（code + machine_code + product_id + device_info）
        ├ 200 → save_code
        ├ 403/404 → 透传错误消息
        └ 断网 → "无法连接授权服务器"
```

### 机器码生成

基于网卡 MAC 地址的 UUID 哈希：`MID-` + SHA256(getnode)[:16].upper()
稳定不变，换机器/换网卡才会变。

## 状态流转

### 在线激活码状态

```
unused ──activate──▶ active ──disable──▶ disabled ──enable──▶ active
                         │                                        │
                      到期/verify                              (有绑定→active)
                         │                                  无绑定→unused
                       expired
```

- `disable`：active/disabled/unused → disabled（保留 machine_code）
- `enable`：disabled → 有 machine_code 恢复 active；无 machine_code 恢复 unused
- `expired`：verify 时发现 expires_at 过期自动标记

### 离线授权码

- 客户端本地 `offline_license_records` 表记录已使用码（防重用）
- 落库在 `accounts.db`，软件被删/清理后记录仍保留
- 纯算法验签，服务端不跟踪状态

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
| `license_core.py` | 离线码算法（HMAC-SHA256 + Crockford base32） |
| `API.md` | 客户端接入文档 |
| `static/index.html` | 管理后台 Vue3 前端 |

### cbcn2api
| 文件 | 职责 |
|---|---|
| `src/license.py` | 授权核心（开关查询/激活/验证/离线兜底） |
| `src/license_core.py` | 离线码算法（与 lic-admin 一致） |
| `src/gui/app.py` | pywebview IPC（check_license/activate/get_machine_code） |
| `src/gui/index.html` | 激活界面 + 关于页授权状态 |
| `src/storage/store.py` | offline_license_records 表（防重用记录） |

## 安全要点

- 在线码传输需 HTTPS（生产环境）
- 离线码签名密钥（SECRET + PEPPER）三端一致
- license_core 用 PBKDF2 派生密钥（10 万次迭代），防逆向
- 机器码不上报敏感信息
- 离线码 32bit 到期时间上限 2106-02-07（0xFFFFFFFF）
