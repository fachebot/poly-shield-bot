# 系统架构

## 概述

poly-shield-bot 是一个分层的 Polymarket 自动交易风控系统，包含 CLI 工具、本地后端服务、WebSocket 实时流、数据持久化和加密密钥管理等多个模块。

```
┌─────────────────────────────────────────┐
│           User Interface Layer          │
├──────────────────┬──────────────────────┤
│   Web Console    │      CLI Tools       │
│  (HTMX/FastAPI) │   (commands)         │
└────────┬─────────┴────────────┬─────────┘
         │                      │
         └──────────┬───────────┘
                    │
    ┌───────────────▼───────────────┐
    │   Application Core Layer      │
    ├───────────────────────────────┤
    │  • Runtime (runtime.py)       │
    │  • Service (service.py)       │
    │  • Rules Engine (rules.py)    │
    │  • Watcher (watcher.py)       │
    └──────────┬────────────────────┘
               │
    ┌──────────┴─────────────────────┐
    │  Integration & Persistence    │
    ├───────────────────────────────┤
    │  • Store (store.py) - SQLite  │
    │  • Polymarket Gateway         │
    │  • WebSocket Streams          │
    │  • Secret Storage             │
    └───────────────────────────────┘
```

## 核心模块

### 配置与密钥管理

#### config.py - 配置管理

- `PolymarketCredentials` —— 账户凭证（私钥、funder、signature_type）
- `from_env()` —— 从环境变量和本地加密仓库加载配置
- 自动推导 user_address 基于 signature_type（代理模式用 funder，直连模式用 signer）
- 启动时验证 signer/funder/signature_type 组合合法性

#### wallet_identity.py - 钱包身份管理

- `resolve_effective_private_key()` —— 从本地加密仓库读取私钥
- `inspect_effective_signer()` —— 显示 signer 地址、user_address 和验证状态
- `validate_signer_configuration()` —— 启动时校验配置
- 支持直连钱包和代理钱包（Poly Proxy、Gnosis Safe）两种模式

#### secret_store.py - 加密密钥仓库

- `LocalSecretStore` —— 本地化密钥加密存储
- **Windows** —— DPAPI（Data Protection API，用户+机器绑定）
- **Linux** —— keyring/SecretService（系统密钥环）
- 私钥从不在环境变量中明文存储，仅在启动时从加密仓库读取一次

### 风控规则引擎

#### rules.py - 规则定义与状态机

定义止损止盈规则：

- `BreakEvenStopRule` —— 保本止损（买一价 ≤ 均价时触发）
- `PriceStopRule` —— 固定价止损（买一价 ≤ 目标价时触发）
- `TakeProfitRule` —— 固定价止盈（买一价 ≥ 目标价时触发）
- `TrailingDrawdownRule` —— 峰值回撤止盈（追踪最高价，按回撤比例触发）

每条规则都维护独立状态机：

- `PENDING` → 等待触发条件
- `TRIGGERED` → 条件满足，待确认卖出
- `PARTIALLY_EXECUTED` → 部分成交，等待补卖
- `COMPLETED` → 规则完成

#### watcher.py - 实时行情监视

- `Watcher` —— 轮询订单簿，评估规则状态，生成卖出建议
- 支持 `--dry-run`（仅读，不下单）和实盘两种模式
- 支持 `--run-once`（单次执行）和常驻轮询

### 交易核心

#### polymarket.py - Polymarket CLOB 网关

- `PolymarketClient` —— 订单薄读取、 余额查询、FAK 市价卖出
- 基于 `py-clob-client` 二次封装
- 支持 HTTP 代理和 HTTPS 代理配置
- 合并 API Key 认证和私钥签名两种方式

#### positions.py - 持仓管理

- 读取官方 `GET /positions` 接口
- 解析持仓大小、均价、token ID 等信息
- 支持 token ID 过滤

### 后端服务与运行时

#### server.py - 服务启动入口

- 解析 CLI 参数并加载本地安全设置
- 启动前输出 signer/funder/signature_type 上下文
- 校验 signer 配置，不合法时直接拒绝启动
- 调用 `api.create_app()` 创建 FastAPI 应用，并交给 uvicorn 运行

#### api.py - FastAPI 应用与 HTTP 接口

- `create_app()` —— 创建 FastAPI 应用实例
- 路由：
  - `GET /` —— Web 控制台首页
  - `GET /health` —— 运行时健康检查
  - `GET /positions` —— 持仓查询（返回内存缓存或官方 API）
  - `POST /tasks` —— 创建任务
  - `GET /tasks` —— 任务列表
  - `GET /records` —— 执行记录查询
- Basic Auth、CSRF 保护和 Origin 拦截
- 默认监听 `127.0.0.1:8787`

#### runtime.py - 执行运行时

核心功能：

- **任务管理** —— 维护 active 任务列表、规则状态
- **市场流订阅** —— 按需订阅/取消订阅 token 的 market WebSocket
- **订单跟踪** —— 记录提交的订单，通过 user WebSocket 跟踪成交/失败
- **执行链路** —— 按行情事件驱动，实时计算规则状态并执行卖出
- **Crash-safe** —— 执行前落成 attempt record，异常重启时恢复未完成订单
- **单实例保护** —— 分布式 lease 机制防止并发执行
- **陈旧数据降级** —— 市场流或用户流无消息超过阈值时自动暂停任务

关键属性：

- `running` —— 主循环是否启动
- `runner_count` —— 当前 active 任务数
- `subscribed_token_ids` —— 市场订阅列表
- `tracked_order_count` —— 待对账订单数
- `last_market_message_at` —— 最近市场消息时间
- `last_user_message_at` —— 最近用户流消息时间

#### service.py - 业务逻辑服务

- `TaskService` —— 聚合 store、任务状态和 runtime 协调所需能力
- `query_positions()` —— 持仓查询
- `create_task()` —— 任务创建和启动
- `list_tasks()` —— 任务查询
- `add_record()` —— 记录执行事件

### 数据存储

#### store.py - SQLite 本地存储

表结构：

- `tasks` —— 任务表（token_id、position_size、规则参数等）
- `execution_records` —— 执行记录表（attempt_id、状态、时间戳）
- `runtime_lease` —— 单实例 lease 表（owner_id、expires_at）

关键操作：

- `save_task()` / `load_task()` —— 任务 CRUD
- `add_record()` / `query_records()` —— 记录 CRUD
- `try_acquire_lease()` —— 竞争执行权
- `update_freshness()` —— 更新 WebSocket 消息时戳

### WebSocket 实时流

#### market_stream.py - 市场行情流

- 订阅 Polymarket `/markets/{token_id}` channel
- 接收 orderbook 更新（best_bid、best_ask、top bids/asks）
- 重连时拉取最新 snapshot
- 发送心跳保活，接收 PONG 更新 freshness

#### user_stream.py - 用户订单流

- 订阅 `/user/{api_user}` channel（需要 API 凭证）
- 接收 order（submitted → matched/rejected）
- 接收 trade（成交详情）
- 自动记录订单生命周期

### CLI 命令行

#### cli.py - 命令处理

命令树：

- `poly-shield watch` —— 单次联调模式
- `poly-shield serve` —— 启动后端服务
- `poly-shield tasks` —— 任务管理
  - `add` —— 创建任务
  - `list` —— 任务列表
- `poly-shield records` —— 执行记录查询
- `poly-shield positions` —— 持仓查询
- `poly-shield secrets` —— 密钥管理
  - `set-private-key` —— 设置私钥
  - `status` —— 存储状态
  - `inspect-private-key` —— 验证私钥
  - `clear-private-key` —— 清除私钥

## 数据流

### 启动流程

```
启动命令
  ↓
加载配置 (config.py)
  ├─ 读取环境变量 (POLY_*)
  ├─ 从本地加密仓库读取私钥
  └─ 推导 user_address
  ↓
验证配置 (wallet_identity.py)
  ├─ 检查 signature_type 合法性
  ├─ 验证 signer/funder 一致性
  └─ 若非法则启动失败
  ↓
初始化服务 (service.py)
  ├─ 创建 Polymarket 网关
  ├─ 打开 SQLite 连接
  └─ 创建 Runtime 实例
  ↓
启动 FastAPI 服务 (server.py)
  ├─ 监听 127.0.0.1:8787
  └─ 提供 Web 控制台和 API
  ↓
启动 Runtime 主循环 (runtime.py)
  ├─ 竞争分布式 lease
  ├─ 加载 active 任务列表
  ├─ 订阅相关 token 的市场流
  └─ 进入事件循环
```

### 任务执行流程

```
用户创建任务 (Web UI 或 CLI)
  ↓
任务保存到 SQLite (store.py)
  ↓
Runtime 加载任务，订阅 market WebSocket
  ↓
  ┌─────────────────────────────────────┐
  │ 市场行情更新                        │
  ├─────────────────────────────────────┤
  │ 1. WebSocket 接收新行情 (msg)       │
  │ 2. Runtime 评估所有规则 (rules.py) │
  │ 3. 规则触发？                       │
  │    ├─ 否 → 继续等待                 │
  │    └─ 是 → 生成卖出执行             │
  │ 4. 下单前保存 attempt record        │
  │ 5. 调用 Polymarket 市价卖出         │
  │ 6. 拿到 order_id，订阅 user stream  │
  │ 7. User stream 跟踪订单终态        │
  │    ├─ matched → 更新 record        │
  │    ├─ failed → 标记失败              │
  │    └─ confirmed → 任务完成           │
  └─────────────────────────────────────┘
      ↓
  陈旧数据检测
      ├─ 市场流 > 15s 无消息 → 暂停任务
      ├─ 用户流 > 30s 无消息 → 暂停任务
      └─ 写入 system 事件记录
```

### 异常重启恢复

```
Runtime 异常退出
  ↓
重新启动
  ↓
Runtime.initialize() 扫描数据库
  ├─ 发现 prepared attempt → 需要确认
  ├─ 发现 submitted attempt → REST 对账
  └─ 无法确认 → 改成 needs-review，暂停任务
  ↓
继续正常执行
```

## 安全机制

### 私钥管理

| 操作系统 | 存储方式   | 加密机制                    | 特点                                      |
| -------- | ---------- | --------------------------- | ----------------------------------------- |
| Windows  | 本地文件   | DPAPI (Data Protection API) | 用户+机器绑定，无法跨用户/机器解密        |
| Linux    | 系统密钥环 | Secret Service / keyring    | 系统级密钥管理，需要 gnome-keyring 等依赖 |

### 运行时安全

- **CSRF 保护** —— Web UI 写操作需要 CSRF token
- **Origin 拦截** —— 跨源请求被拒绝
- **本地监听** —— 默认仅 `127.0.0.1:8787`，外网无法直接访问
- **Lease 机制** —— 分布式锁确保单实例执行

### 执行可靠性

1. **Attempt 记录** —— 下单前落成 prepared record
2. **Crash-safe** —— 异常重启时自动扫描未完成 attempt
3. **状态确认** —— user WebSocket + REST 对账双通道
4. **陈旧数据降级** —— 无消息超时时自动暂停而不是盲目继续

## 配置拓扑

### 直连钱包模式 (signature_type=0)

```
┌─────────────┐
│ User Wallet │  ← 私钥
│ (EOA)       │
└──────┬──────┘
       │ signer_address
       │
    ┌─►POLY_FUNDER = signer_address
    │
    user_address = signer_address
```

### 代理钱包模式 (signature_type=1 或 2)

```
┌──────────────┐     ┌──────────────┐
│ Signer       │     │ Funder       │
│ (签名者)     │     │ (资金方)     │
└──────┬───────┘     └──────┬───────┘
       │ 私钥               │ 资金
       │                    │
       │    POLY_SIGNATURE_TYPE=1 (Poly Proxy)
       │    或 2 (Gnosis Safe)
       │                    │
       └────────┬───────────┘
                │
           user_address = POLY_FUNDER
```

启动时验证：

- `signature_type=0` → signer_address 必须等于 POLY_FUNDER
- `signature_type=1/2` → signer_address 和 POLY_FUNDER 可以不同

## 性能考虑

### WebSocket 连接管理

- 按需订阅/取消订阅 token channels
- 市场流每次数据都更新 freshness 时戳
- 心跳 (PING/PONG) 不中断任务执行，仅更新 freshness

### SQLite 持久化

- 异步写入 execution records（不阻塞主循环）
- 定期清理过期记录（保留策略可配置）
- Lease 表用于单实例竞争（3 秒过期，续约间隔）

### 内存管理

- 任务列表按需加载
- 规则状态维护在内存（task object）
- 不缓存整个订单簿，仅保留最新 best_bid/best_ask

## 扩展点

1. **新规则类型** —— 继承 `Rule` 基类实现 `should_execute()` 和 `on_execute()`
2. **新的数据源** —— 扩展 `store.py` 支持其他数据库
3. **通知系统** —— Telegram / Email 等提醒（当前可通过 records 查询）
4. **多用户权限** —— 扩展 Web UI 添加用户认证和 task 隔离
5. **Linux 本地加密** —— 支持加密文件 + 启动时密码解锁（后续可选）

## 依赖关系

```
core 层:
  └─ rules.py (风控规则)
  └─ watcher.py (实时监视)

polymarket 层:
  └─ polymarket.py (CLOB 网关)
  └─ positions.py (持仓管理)

storage 层:
  └─ store.py (SQLite 持久化)
  └─ secret_store.py (密钥加密存储)

config 层:
  └─ config.py (配置管理)
  └─ wallet_identity.py (钱包身份)

integration 层:
  └─ market_stream.py (市场 WebSocket)
  └─ user_stream.py (用户订单 WebSocket)
  └─ runtime.py (执行运行时)
  └─ service.py (业务服务)

api 层:
  └─ server.py (HTTP API)
  └─ api.py (服务层 API)

cli 层:
  └─ cli.py (命令行工具)
```

## 常见部署场景

### 本地开发 + Web 控制台

```bash
poetry run poly-shield serve --ui-username admin --ui-password "xxx"
# 访问 http://127.0.0.1:8787/
# 在 Web UI 创建任务，后端自动执行
```

### CLI 单次联调

```bash
poetry run poly-shield watch \
    --token-id "..." \
    --position-size 100 \
    --average-cost 0.42 \
    --take-profit 0.68 \
    --take-profit-size 25 \
    --dry-run
```

### 生产环境（长期运行）

1. 使用代理钱包配置（Poly Proxy 或 Gnosis Safe）
2. 配置 API 凭证用于订单跟踪
3. 配置代理地址（如 Cloudflare 限制）
4. 使用 systemd 或 supervisor 管理进程
5. 定期查看 `/health` 和 records 确保运行状态
6. 配置告警规则（e.g. lease 失败、stale 任务过多）

## 许可

见项目 LICENSE 文件。
