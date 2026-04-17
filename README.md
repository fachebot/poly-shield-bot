# poly-shield-bot

Polymarket 自动止盈止损交易机器人。

这是一个面向 Polymarket CLOB 的本地化风控系统，提供多规则止损止盈、实时市场流驱动执行、Web 控制台，以及适合联调和排错的 CLI。

## 核心特性

### 风控规则

- 保本止损：买一价小于等于均价时触发。
- 固定价格止损：达到指定价格阈值后自动卖出。
- 固定价格止盈：达到目标价后自动卖出。
- 峰值回撤止盈：追踪最高买一价，按回撤比例触发，可配置激活价。
- 多规则仓位隔离：同一轮中自动裁剪可用仓位，避免超卖。

### 后端运行时

- 实时市场流驱动：active 任务由 market WebSocket 事件推动执行。
- Crash-safe 执行：下单前先落 execution attempt，异常重启后恢复或转 needs-review。
- 单实例保护：SQLite lease 防止多个 runtime 并发消费同一批任务。
- 陈旧数据降级：market 或 user 流长时间无消息时自动暂停相关任务。
- SQLite 持久化：任务、执行记录、system 事件完整落地。

### Web 控制台

- 任务面板：创建、编辑、暂停、恢复任务。
- 执行时间线：分页查看执行记录和 system 事件。
- 健康状态面板：显示 runtime、订阅状态和 freshness 信息。

### 命令行工具

- watch：适合单次联调，支持 --dry-run 和 --run-once。
- serve：启动后端服务和 Web 控制台。
- tasks：通过后端 API 管理任务。
- records：查询执行记录和 system 事件。
- positions：查看账户持仓。
- secrets：管理本地加密私钥仓库。

### 安全机制

- 本地密钥存储：Windows 使用 DPAPI，Linux 使用 keyring/SecretService。
- 私钥仅从本地密钥仓库读取，不支持通过环境变量明文注入。
- 代理钱包支持：按 signature_type 自动推导 effective user address。
- 本地访问保护：支持 UI Basic Auth、Origin/Referer 拦截和 CSRF 校验。

## 快速开始

建议使用 Poetry 管理依赖和运行环境：

```bash
poetry install
```

把 [.env.example](.env.example) 复制成 `.env`，填写你的 Polymarket 配置。常用字段包括：

- POLY_HOST
- POLY_DATA_API_URL
- POLY_CHAIN_ID
- POLY_FUNDER
- POLY_SIGNATURE_TYPE
- POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE

如果你使用代理钱包，先看 [docs/PROXY_WALLET_MODE_GUIDE.md](docs/PROXY_WALLET_MODE_GUIDE.md)。

user address 现在由程序自动推导：

- signature_type=1 或 2：使用 POLY_FUNDER。
- signature_type=0 或未设置：使用私钥推导出的 signer 地址。

如果本地访问 CLOB 或 data-api 受限，可以配置代理：

- POLY_HTTP_PROXY
- POLY_HTTPS_PROXY
- POLY_NO_PROXY

## 私钥配置

私钥不再从 `.env` 读取，必须写入本地加密仓库：

```bash
poetry run poly-shield secrets set-private-key
```

查看密钥仓库状态：

```bash
poetry run poly-shield secrets status
```

检查当前私钥与 signer/funder 关系：

```bash
poetry run poly-shield secrets inspect-private-key
```

清除本地私钥：

```bash
poetry run poly-shield secrets clear-private-key
```

可选的密钥仓库后端覆盖项：

- POLY_SECRET_STORE_BACKEND=dpapi
- POLY_SECRET_STORE_BACKEND=keyring

## 运行服务

查看命令帮助：

```bash
poetry run poly-shield --help
```

启动本地后端服务：

```bash
poetry run poly-shield serve
```

如果准备长期本机运行，建议启用 UI 口令：

```bash
poetry run poly-shield serve --ui-username admin --ui-password "change-me"
```

也可以使用环境变量：

- POLY_UI_USERNAME
- POLY_UI_PASSWORD

默认本地安全行为：

- 服务监听 127.0.0.1。
- 浏览器跨站写请求会做 Origin/Referer 拦截。
- UI 写操作要求 CSRF token。

启动后可直接打开：

```text
http://127.0.0.1:8787/
```

## 常用命令

创建一个 dry-run 任务：

```bash
poetry run poly-shield tasks add \
    --token-id <TOKEN_ID> \
    --position-size 100 \
    --average-cost 0.42 \
    --take-profit 0.68 \
    --take-profit-size 25 \
    --dry-run
```

列出任务：

```bash
poetry run poly-shield tasks list
```

查询执行记录：

```bash
poetry run poly-shield records --limit 20
```

查看全部持仓：

```bash
poetry run poly-shield positions --size-threshold 0
```

如果要经由本机代理联调：

```powershell
$env:POLY_HTTPS_PROXY='http://127.0.0.1:7890'
poetry run poly-shield positions --size-threshold 0
```

只读演练一个固定价止损：

```bash
poetry run poly-shield watch \
    --token-id <TOKEN_ID> \
    --position-size 100 \
    --price-stop 0.40 \
    --price-stop-size 50 \
    --dry-run \
    --run-once
```

跑一个保本止损 + 固定止盈组合：

```bash
poetry run poly-shield watch \
    --token-id <TOKEN_ID> \
    --average-cost 0.42 \
    --position-size 100 \
    --breakeven-stop-size 50 \
    --take-profit 0.68 \
    --take-profit-size 25 \
    --dry-run
```

跑一个峰值回撤止盈：

```bash
poetry run poly-shield watch \
    --token-id <TOKEN_ID> \
    --trailing-drawdown 0.10 \
    --trailing-sell-size 50 \
    --trailing-activation-price 0.65 \
    --dry-run
```

watch 的持仓来源规则如下：

- 同时传入 --position-size 和 --average-cost 时，完全使用手动值。
- 缺少其中一个时，CLI 会尝试通过官方 GET /positions 补齐。
- 如果官方 positions 接口不可用，仍可用手动参数继续 dry-run 或实盘。

## 文档导航

- [docs/PROXY_WALLET_MODE_GUIDE.md](docs/PROXY_WALLET_MODE_GUIDE.md) —— 代理钱包配置说明。
- [docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md](docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md) —— 止损止盈规则参数和示例。
- [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md) —— 系统架构、模块关系与数据流。
- [docs/INTEGRATION_TEST_PLAN.md](docs/INTEGRATION_TEST_PLAN.md) —— CLI、后端、WebSocket 集成测试计划。
- [docs/ACCEPTANCE_CHECKLIST.md](docs/ACCEPTANCE_CHECKLIST.md) —— 换账号或换环境时的验收清单。

## 许可

见 [LICENSE](LICENSE)。
