# poly-shield-bot

Polymarket 自动止盈止损交易机器人。

一个专为 Polymarket CLOB（中央订单薄）设计的本地化风控工具，支持**多种止损/止盈规则、实时市场数据驱动、Web 控制台和 CLI 双引擎**。

## 核心特性

### 风控规则

### 风控规则

- **保本止损** —— 当买一价小于等于均价时触发
- **固定价格止损** —— 指定价格阈值，达到时自动卖出
- **固定价格止盈** —— 指定目标价，达到时自动卖出
- **峰值回撤止盈** —— 追踪最高买一价，按设定回撤比例触发（支持激活价）
- **智能仓位隔离** —— 多规则并行时，自动分配可用仓位，避免超卖

- **Crash-safe 执行** —— 异常重启时自动恢复未完成订单，防止遗漏或重复
- **单实例保护** —— 分布式 lease 机制，同一时刻仅一个 runtime 管理任务
- **陈旧数据自动降级** —— WebSocket 断连或超时时自动暂停相关任务
- **SQLite 持久化** —— 任务、执行记录、系统事件完整落地

- **任务面板** —— 创建、编辑、暂停、恢复任务
- **执行时间线** —— 分页查看执行记录和系统事件
- **Health Dashboard** —— 实时显示后端运行状态、WebSocket 连接、陈旧数据警告

### 命令行工具

- **watch** —— 单次联调模式（支持 `--dry-run` 和 `--run-once`）
- **serve** —— 启动后端服务和 Web 控制台
- **tasks** —— 任务增删查管理
- **records** —— 查询执行记录和系统事件
- **positions** —— 查看账户持仓（支持代理过滤）
- **secrets** —— 管理本地加密密钥仓库

### 安全机制

- **跨平台密钥存储**
  - Windows：DPAPI（用户+机器绑定加密）
  - Linux：keyring/SecretService（系统密钥环）
  - 私钥 **仅存储在本地加密仓库，不允许在环境变量中配置**
- **代理钱包支持** —— 区分 signer（签名者）和 funder（资金方），启动时自动验证配置合法性
- **CSRF 保护** —— Web UI 写操作验证 token

### 安装

建议直接用 Poetry 管理依赖和运行环境，而不是手动创建 venv：
把 [.env.example](.env.example) 复制成 `.env` 后填写你的 Polymarket 账户信息。

如果你在用代理钱包，建议先看一遍 [docs/PROXY_WALLET_MODE_GUIDE.md](docs/PROXY_WALLET_MODE_GUIDE.md)。

代理钱包场景重点关注这几个字段：

- `POLY_PRIVATE_KEY`
- `POLY_FUNDER`
- `POLY_SIGNATURE_TYPE`

`POLY_USER_ADDRESS` / `POLY_USER` 已不再需要，程序会按 `POLY_SIGNATURE_TYPE` 自动推导 user address：

- `signature_type=1/2`（代理钱包）：user address 使用 `POLY_FUNDER`
- `signature_type=0` 或未设置（直连钱包）：user address 使用私钥推导出的 signer 地址

如果你本地访问 `data-api.polymarket.com` 或 CLOB 会被拦，可以直接在 `.env` 里配代理：

- `POLY_HTTP_PROXY`
- `POLY_HTTPS_PROXY`
- `POLY_NO_PROXY`

查看命令帮助：

```bash
poetry run poly-shield --help
```

启动本地后端服务：

```bash
poetry run poly-shield serve
```

如果你准备长期在本机运行，建议至少开启本地 UI 口令：

```bash
poetry run poly-shield serve --ui-username admin --ui-password "change-me"
```

也可以用环境变量：

- `POLY_UI_USERNAME`
- `POLY_UI_PASSWORD`

本地安全默认行为：

- 后端默认监听 `127.0.0.1`（仅本机可访问）
- 对浏览器发起的跨站写请求做 Origin/Referer 拦截
- UI 写操作要求 CSRF token（HTMX 页面会自动附带）

- Windows: DPAPI（仅当前 Windows 用户、当前机器可解密）
- Linux: keyring/SecretService（通过系统密钥环保存）

- `POLY_SECRET_STORE_BACKEND=dpapi`
- `POLY_SECRET_STORE_BACKEND=keyring`

```

查看本地密文仓库状态：
poetry run poly-shield secrets status
```

```bash
poetry run poly-shield secrets inspect-private-key
```

这个命令会从本地加密密文仓库读取私钥。

输出里会同时给出：

- 私钥来源
- signer 地址（也就是私钥真实对应的 EOA 地址）
- `POLY_SIGNATURE_TYPE`
- 推导后的 user address
- 是否与 `POLY_FUNDER` / user address 一致

注意：

- 直连钱包模式下，signer 地址通常会和 `POLY_FUNDER` 一致
- 代理钱包模式下（例如 `POLY_SIGNATURE_TYPE=1`），signer 地址和 `POLY_FUNDER` 不一致通常是正常的；`POLY_FUNDER` 表示代理/出资地址，私钥只负责签名

清除本地密文仓库中的私钥：

````bash

运行时私钥来源：


```text
http://127.0.0.1:8787/
````

```bash
poetry run poly-shield tasks add \
	--average-cost 0.42 \
	--take-profit 0.68 \
	--take-profit-size 25 \
	--dry-run
```

列出当前任务：

```bash
poetry run poly-shield tasks list
```

查询执行记录：

```bash
poetry run poly-shield records --limit 20
```

列出当前账户全部持仓：

```bash
poetry run poly-shield positions \
	--size-threshold 0
$env:POLY_HTTPS_PROXY='http://127.0.0.1:7890'
poetry run poly-shield positions --size-threshold 0
```

```bash
poetry run poly-shield positions \
	--token-id <TOKEN_ID> \
	--size-threshold 0
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

`watch` 现在的默认数据来源是这样的：

- 如果你同时传了 `--position-size` 和 `--average-cost`，就完全走手动值。
- 如果你少传其中一个，CLI 会优先去官方 `GET /positions` 补齐缺的持仓大小或均价。
- 如果官方 positions 接口不可用，你仍然可以继续用手动参数跑 dry-run 或实盘。
- 如果本地网络受限，可以通过 `POLY_HTTP_PROXY / POLY_HTTPS_PROXY` 让 `positions` 和 `watch` 同时走代理。

换账号或换环境时，可以直接按 [docs/ACCEPTANCE_CHECKLIST.md](docs/ACCEPTANCE_CHECKLIST.md) 逐项验收。

相关文档：

- [docs/PROXY_WALLET_MODE_GUIDE.md](docs/PROXY_WALLET_MODE_GUIDE.md)
- [docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md](docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md)
- [docs/INTEGRATION_TEST_PLAN.md](docs/INTEGRATION_TEST_PLAN.md)

如果你要验证 CLI、后端服务、SQLite、market websocket 和 user websocket 这整条链路，而不只是单个命令是否能跑，见 [docs/INTEGRATION_TEST_PLAN.md](docs/INTEGRATION_TEST_PLAN.md)。

止盈止损的详细参数说明、规则解释和示例，见 [docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md](docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md)。

如果你现在想体验新的后端任务链路，建议这样跑：

1. 先执行 `poetry run poly-shield serve`
2. 再用 `poetry run poly-shield tasks add ...` 创建任务
3. 用 `poetry run poly-shield tasks list` 和 `poetry run poly-shield records` 查询状态和记录

现在这条新链路已经能持久化任务和记录，并且 active 任务会由后端通过市场 websocket 自动执行；如果官方 positions 接口在你的环境里不可用，也可以在 `tasks add` 时直接写入 `--position-size` 和 `--average-cost`。原来的 `watch` 命令仍保留，适合单次联调和本地排错。

如果任务是真实下单而不是 dry-run，后端还会在拿到 order id 之后自动订阅对应 market 的 user channel，把后续 `order` / `trade` 更新继续追加到 `records` 里。

`GET /health` 现在也会返回 `last_market_message_at`、`last_user_message_at` 和 `stale_seconds`，方便你直接判断本地运行时数据是不是已经陈旧。

## 后端可靠性语义

后端 runtime 现在默认按“宁可暂停，也不要在状态不确定时继续卖”的思路处理真实下单：

- 对真实下单任务，先写一条 execution attempt，再发单；发单成功后会继续推进 attempt 状态。
- 如果真实下单返回里没有 order id，这次执行不会被当成“正常 matched”，而是会直接标成 needs-review，并暂停该任务，等你人工确认。
- 如果 runtime 重启时发现数据库里还有 prepared 或 submitted 但未完成对账的 attempt，会优先恢复它们；无法确认终态的会被改成 needs-review，并把任务暂停。
- user websocket 正常收到 confirmed / failed，或者重连后 REST 对账拿到终态，都会把 attempt 和 execution record 一起推进到终态。

`GET /health` 里的 runtime 字段现在建议这样理解：

- running：当前 runtime 主循环是否已启动。
- runner_count：当前正在管理的 active 任务数量。
- subscribed_token_ids：当前 market websocket 正在订阅的 token 列表。
- tracked_order_count：当前仍在等待 user channel 或 REST 对账补终态的订单数。
- subscribed_market_ids：当前 user websocket 正在订阅的 market 列表。
- lease_owner_id / lease_expires_at：当前 runtime 持有的单实例 lease 信息；如果拿不到 lease，新的 runtime 会直接启动失败，而不是并发执行。
- last_market_message_at：最近一次收到市场消息的时间；只有在当前确实存在 active token 订阅时这个字段才有意义，没有 active 任务时会回到 null。
- last_user_message_at：最近一次收到用户订单更新的时间；只有在当前确实有 tracked orders 或 user market 订阅时这个字段才有意义，没有待跟踪订单时会回到 null。
- stale_seconds.market：市场流距离最近一条消息过去了多久；仅在存在 market 订阅上下文时才会有值。
- stale_seconds.user：用户流距离最近一条消息过去了多久；仅在存在 tracked order / user 订阅上下文时才会有值。
- stale_seconds.max：当前所有有效 stale 秒数里的最大值；如果 market 和 user 两边都没有相关上下文，就是 null。

默认阈值下，market stale 超过 15 秒、user stale 超过 30 秒时，runtime 会把受影响任务自动切到 paused，并在 records 里补一条 system 记录说明暂停原因。后续你可以先查 records，再决定是人工恢复、继续观测，还是直接删除任务。

补充说明：market 流的心跳回包（PONG）也会更新运行时 freshness，因此“市场本身不活跃但 websocket 仍存活”的场景不会再被误判成 market stale。

```bash
poetry run pytest tests/test_rules.py tests/test_watcher.py tests/test_positions.py
```

代理配置回归测试：

````

已验证通过的链路：

- 规则单测通过。
- watcher 状态机单测通过。
- 官方 positions 响应解析单测通过。
- 后端任务仓储、服务层和 API 单测通过。
- websocket 市场流解析与 runtime 集成单测通过。
- websocket user channel 解析与 runtime 订单跟踪单测通过。
- websocket 重连后的 market snapshot 补拉、tracked order REST 对账和 health freshness 快照单测通过。
- execution attempts、runtime lease、runtime 重启恢复和 stale 自动暂停单测通过。
- CLI `watch --help` 可正常启动。
- CLI `serve --help`、`tasks add --help` 可正常启动。
- 使用公开订单簿的 `watch --dry-run --run-once` 已成功跑通一次。
- 官方 positions CLI 在当前环境会被 `403 / 1010` 拦截，所以这条只完成了本地单测验证，没有完成在线实测。

## 下一步

按当前优先级，建议继续做这几件事：

## 安装

建议直接用 Poetry 管理依赖和运行环境，而不是手动创建 venv：
把 [.env.example](.env.example) 复制成 `.env` 后填写你的 Polymarket 账户信息。

如果你在用代理钱包，建议先看一遍 [docs/PROXY_WALLET_MODE_GUIDE.md](docs/PROXY_WALLET_MODE_GUIDE.md)。

代理钱包场景重点关注这几个字段：

- `POLY_PRIVATE_KEY`
- `POLY_FUNDER`
- `POLY_SIGNATURE_TYPE`

`POLY_USER_ADDRESS` / `POLY_USER` 已不再需要，程序会按 `POLY_SIGNATURE_TYPE` 自动推导 user address：

- `signature_type=1/2`（代理钱包）：user address 使用 `POLY_FUNDER`
- `signature_type=0` 或未设置（直连钱包）：user address 使用私钥推导出的 signer 地址

如果你本地访问 `data-api.polymarket.com` 或 CLOB 会被拦，可以直接在 `.env` 里配代理：

- `POLY_HTTP_PROXY`
- `POLY_HTTPS_PROXY`
- `POLY_NO_PROXY`


查看命令帮助：

```bash
poetry run poly-shield --help
````

启动本地后端服务：

## 文档导航

关于项目的详细说明，请参阅：

- [docs/PROXY_WALLET_MODE_GUIDE.md](docs/PROXY_WALLET_MODE_GUIDE.md) —— 代理钱包 (Poly Proxy / Gnosis Safe) 配置指南
- [docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md](docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md) —— 止损/止盈规则详细参数说明和示例
- [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md) —— 系统架构、模块设计、数据流详解
- [docs/INTEGRATION_TEST_PLAN.md](docs/INTEGRATION_TEST_PLAN.md) —— CLI / 后端 / WebSocket 集成测试计划
- [docs/ACCEPTANCE_CHECKLIST.md](docs/ACCEPTANCE_CHECKLIST.md) —— 换账号时的验收清单

## 许可

见 [LICENSE](LICENSE) 文件。

```bash
poetry run poly-shield serve
```

如果你准备长期在本机运行，建议至少开启本地 UI 口令：

```bash
poetry run poly-shield serve --ui-username admin --ui-password "change-me"
```

也可以用环境变量：

- `POLY_UI_USERNAME`
- `POLY_UI_PASSWORD`

本地安全默认行为：

- 后端默认监听 `127.0.0.1`（仅本机可访问）
- 对浏览器发起的跨站写请求做 Origin/Referer 拦截
- UI 写操作要求 CSRF token（HTMX 页面会自动附带）

如果你不想把 `POLY_PRIVATE_KEY` 明文写进 `.env`，现在也可以把私钥写入本地加密仓库。
当前支持：

- Windows: DPAPI（仅当前 Windows 用户、当前机器可解密）
- Linux: keyring/SecretService（通过系统密钥环保存）

如需手动指定后端，可设置：

- `POLY_SECRET_STORE_BACKEND=dpapi`
- `POLY_SECRET_STORE_BACKEND=keyring`

```bash
poetry run poly-shield secrets set-private-key
```

查看本地密文仓库状态：

```bash
poetry run poly-shield secrets status
```

输出中会包含 `backend` 字段，表示当前使用的本地密钥后端。

校验当前生效私钥是否合法，并输出它对应的以太坊地址：

```bash
poetry run poly-shield secrets inspect-private-key
```

这个命令会从本地加密密文仓库读取私钥。

输出里会同时给出：

- 私钥来源
- signer 地址（也就是私钥真实对应的 EOA 地址）
- `POLY_SIGNATURE_TYPE`
- 推导后的 user address
- 是否与 `POLY_FUNDER` / user address 一致

注意：

- 直连钱包模式下，signer 地址通常会和 `POLY_FUNDER` 一致
- 代理钱包模式下（例如 `POLY_SIGNATURE_TYPE=1`），signer 地址和 `POLY_FUNDER` 不一致通常是正常的；`POLY_FUNDER` 表示代理/出资地址，私钥只负责签名

清除本地密文仓库中的私钥：

```bash
poetry run poly-shield secrets clear-private-key
```

运行时私钥来源：

1. 本地加密密文仓库

启动后可直接打开 Web 控制台：

```text
http://127.0.0.1:8787/
```

通过后端 API 创建任务：

```bash
poetry run poly-shield tasks add \
	--token-id <TOKEN_ID> \
	--position-size 100 \
	--average-cost 0.42 \
	--take-profit 0.68 \
	--take-profit-size 25 \
	--dry-run
```

列出当前任务：

```bash
poetry run poly-shield tasks list
```

查询执行记录：

```bash
poetry run poly-shield records --limit 20
```

列出当前账户全部持仓：

```bash
poetry run poly-shield positions \
	--size-threshold 0
```

如果你要通过本机代理联调，可以直接这样跑：

```powershell
$env:POLY_HTTPS_PROXY='http://127.0.0.1:7890'
poetry run poly-shield positions --size-threshold 0
```

按 token 过滤单个持仓：

```bash
poetry run poly-shield positions \
	--token-id <TOKEN_ID> \
	--size-threshold 0
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

`watch` 现在的默认数据来源是这样的：

- 如果你同时传了 `--position-size` 和 `--average-cost`，就完全走手动值。
- 如果你少传其中一个，CLI 会优先去官方 `GET /positions` 补齐缺的持仓大小或均价。
- 如果官方 positions 接口不可用，你仍然可以继续用手动参数跑 dry-run 或实盘。
- 如果本地网络受限，可以通过 `POLY_HTTP_PROXY / POLY_HTTPS_PROXY` 让 `positions` 和 `watch` 同时走代理。

换账号或换环境时，可以直接按 [docs/ACCEPTANCE_CHECKLIST.md](docs/ACCEPTANCE_CHECKLIST.md) 逐项验收。

相关文档：

- [docs/PROXY_WALLET_MODE_GUIDE.md](docs/PROXY_WALLET_MODE_GUIDE.md)
- [docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md](docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md)
- [docs/INTEGRATION_TEST_PLAN.md](docs/INTEGRATION_TEST_PLAN.md)

如果你要验证 CLI、后端服务、SQLite、market websocket 和 user websocket 这整条链路，而不只是单个命令是否能跑，见 [docs/INTEGRATION_TEST_PLAN.md](docs/INTEGRATION_TEST_PLAN.md)。

止盈止损的详细参数说明、规则解释和示例，见 [docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md](docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md)。

如果你现在想体验新的后端任务链路，建议这样跑：

1. 先执行 `poetry run poly-shield serve`
2. 再用 `poetry run poly-shield tasks add ...` 创建任务
3. 用 `poetry run poly-shield tasks list` 和 `poetry run poly-shield records` 查询状态和记录

现在这条新链路已经能持久化任务和记录，并且 active 任务会由后端通过市场 websocket 自动执行；如果官方 positions 接口在你的环境里不可用，也可以在 `tasks add` 时直接写入 `--position-size` 和 `--average-cost`。原来的 `watch` 命令仍保留，适合单次联调和本地排错。

如果任务是真实下单而不是 dry-run，后端还会在拿到 order id 之后自动订阅对应 market 的 user channel，把后续 `order` / `trade` 更新继续追加到 `records` 里。

`GET /health` 现在也会返回 `last_market_message_at`、`last_user_message_at` 和 `stale_seconds`，方便你直接判断本地运行时数据是不是已经陈旧。

## 后端可靠性语义

后端 runtime 现在默认按“宁可暂停，也不要在状态不确定时继续卖”的思路处理真实下单：

- 对真实下单任务，先写一条 execution attempt，再发单；发单成功后会继续推进 attempt 状态。
- 如果真实下单返回里没有 order id，这次执行不会被当成“正常 matched”，而是会直接标成 needs-review，并暂停该任务，等你人工确认。
- 如果 runtime 重启时发现数据库里还有 prepared 或 submitted 但未完成对账的 attempt，会优先恢复它们；无法确认终态的会被改成 needs-review，并把任务暂停。
- user websocket 正常收到 confirmed / failed，或者重连后 REST 对账拿到终态，都会把 attempt 和 execution record 一起推进到终态。

`GET /health` 里的 runtime 字段现在建议这样理解：

- running：当前 runtime 主循环是否已启动。
- runner_count：当前正在管理的 active 任务数量。
- subscribed_token_ids：当前 market websocket 正在订阅的 token 列表。
- tracked_order_count：当前仍在等待 user channel 或 REST 对账补终态的订单数。
- subscribed_market_ids：当前 user websocket 正在订阅的 market 列表。
- lease_owner_id / lease_expires_at：当前 runtime 持有的单实例 lease 信息；如果拿不到 lease，新的 runtime 会直接启动失败，而不是并发执行。
- last_market_message_at：最近一次收到市场消息的时间；只有在当前确实存在 active token 订阅时这个字段才有意义，没有 active 任务时会回到 null。
- last_user_message_at：最近一次收到用户订单更新的时间；只有在当前确实有 tracked orders 或 user market 订阅时这个字段才有意义，没有待跟踪订单时会回到 null。
- stale_seconds.market：市场流距离最近一条消息过去了多久；仅在存在 market 订阅上下文时才会有值。
- stale_seconds.user：用户流距离最近一条消息过去了多久；仅在存在 tracked order / user 订阅上下文时才会有值。
- stale_seconds.max：当前所有有效 stale 秒数里的最大值；如果 market 和 user 两边都没有相关上下文，就是 null。

默认阈值下，market stale 超过 15 秒、user stale 超过 30 秒时，runtime 会把受影响任务自动切到 paused，并在 records 里补一条 system 记录说明暂停原因。后续你可以先查 records，再决定是人工恢复、继续观测，还是直接删除任务。

补充说明：market 流的心跳回包（PONG）也会更新运行时 freshness，因此“市场本身不活跃但 websocket 仍存活”的场景不会再被误判成 market stale。

## 开发校验

当前最小测试集：

```bash
poetry run pytest tests/test_rules.py tests/test_watcher.py tests/test_positions.py
```

代理配置回归测试：

```bash
poetry run pytest tests/test_proxy.py
```

已验证通过的链路：

- 规则单测通过。
- watcher 状态机单测通过。
- 官方 positions 响应解析单测通过。
- 后端任务仓储、服务层和 API 单测通过。
- websocket 市场流解析与 runtime 集成单测通过。
- websocket user channel 解析与 runtime 订单跟踪单测通过。
- websocket 重连后的 market snapshot 补拉、tracked order REST 对账和 health freshness 快照单测通过。
- execution attempts、runtime lease、runtime 重启恢复和 stale 自动暂停单测通过。
- CLI `watch --help` 可正常启动。
- CLI `serve --help`、`tasks add --help` 可正常启动。
- 使用公开订单簿的 `watch --dry-run --run-once` 已成功跑通一次。
- 官方 positions CLI 在当前环境会被 `403 / 1010` 拦截，所以这条只完成了本地单测验证，没有完成在线实测。

## 下一步

按当前优先级，建议继续做这几件事：

1. 给 `positions` 和 `watch` 增加 geoblock / Cloudflare 1010 的更明确提示和降级策略。
2. 给真实卖单路径补一轮小仓位联调和错误码兜底。
3. 开始做 Telegram Bot 控制面，或者先做分批止盈后自动抬止损。
