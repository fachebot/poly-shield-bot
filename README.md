# poly-shield-bot

Polymarket 自动止盈止损机器人。

当前这版已经有一个能跑的 CLI 雏形，重点先落在命令行风控而不是 Telegram 交互。

## 当前已实现

- 保本止损规则：买一价小于等于均价时触发，按指定股数卖出。
- 指定价格止损：买一价小于等于目标价时触发，按指定股数卖出。
- 指定价格止盈：买一价大于等于目标价时触发，按指定股数卖出。
- 峰值回撤止盈：记录 watch 生命周期里的最高买一价，按设定回撤比例触发止盈，可选激活价；触发后按指定股数卖出。
- 部分成交续卖语义：规则第一次触发后锁定目标卖出数量，后续只补卖剩余数量，避免重复锁定导致超卖。
- 多条规则的可用仓位隔离：同一轮里前面的规则先占用仓位，后面的规则只从剩余可用仓位里锁定卖出股数；如果目标大于可用仓位，会自动截断，避免同轮超卖。
- `watch` 命令：支持常驻轮询、`--run-once` 单次执行和 `--dry-run` 只读演练。
- `watch` 输出现在会带上 `best_ask`、`top_bids` 和 `top_asks`，方便直接对照盘口联调。
- `positions` 命令：接官方 `GET /positions`，可自动列出账户当前全部持仓和均价，也支持按 token 过滤。
- Polymarket CLOB 接入骨架：订单簿读取、条件 token 余额读取、FAK 市价卖出、heartbeat 接口封装。
- 后端服务骨架：新增 FastAPI 服务、SQLite 任务仓储、任务状态与执行记录表。
- 新的任务管理命令：CLI 已支持 `serve`、`tasks`、`records`，开始向后端 API 驱动模式迁移。
- 后端任务现在也支持手动保存 `position_size` / `average_cost` 覆盖值，在官方 positions 接口受限时仍可运行 active 任务。
- 实时执行链路：后端已接上 Polymarket 市场 websocket，active 任务会按订阅到的行情事件驱动执行，并把规则状态和执行记录持久化。
- 用户成交跟踪：后端已接入 user websocket，会把 bot 提交订单的 order/trade 生命周期继续写入执行记录，补齐 `matched -> confirmed/failed` 这条链路。
- 重连恢复：market websocket 重连前会先拉一轮最新 order book snapshot；user websocket 重连前会对 tracked orders 做一次 REST 对账，尽量把断线期间漏掉的终态补回本地记录。
- crash-safe 执行意图：真实下单前会先把 execution attempt 落成 prepared，再推进到 submitted / confirmed / failed；如果进程在中间异常退出，运行时重启后会把未完成 attempt 标成 needs-review，并自动暂停对应任务，避免“远端可能下单了，本地却没痕迹”。
- 单实例运行时保护：后端 runtime 启动时会抢占 SQLite 里的 lease，同一时刻只允许一个 runtime 持有 active 任务执行权，避免双实例重复消费同一批任务。
- 陈旧数据自动降级：如果 market websocket 或 user websocket 长时间没有新消息，运行时会把相关 active 任务自动暂停，并写入一条 system 类型记录，避免在陈旧行情或陈旧订单状态上继续执行。
- Web 控制台：已提供 HTMX + FastAPI 的任务面板，可在浏览器中查看持仓、任务板、任务详情与执行时间线。
- 持仓分组视图：左侧支持 `仓位` / `存档仓位` 双 tab；当 token 在实时持仓里为 0 但仍有历史任务时自动归档。
- 存档只读语义：存档仓位不允许新建/编辑任务；如果用户重新建仓，token 会自动回到 `仓位` tab。
- 时间线按需加载：任务详情执行时间线和系统事件支持下拉展开、分页懒加载和滚动自动加载更多，避免一次性拉全量记录。
- 存档与系统事件可观测性：staleness 触发的自动暂停会写入 `system` 事件，并在任务详情中展示。

## 当前限制

- 当前环境下直接访问官方 `https://data-api.polymarket.com/positions` 会命中 `403 / error code: 1010`，更像是 Cloudflare/地理限制，不是本地解析代码错误。
- 因为上面的限制，我已经把官方 positions 接口接进代码和测试，但没法在这个环境里完成真实在线验证。
- Telegram Bot、多用户权限系统还没开始做；当前 Web 控制台以单用户本地运维为主。
- 用户 websocket 需要服务端可用的 API key/secret/passphrase；如果本地没有可用交易凭证，这条链路无法在线建立连接。
- 真实卖单路径已经接上 py-clob-client，但还没做真实账户的小仓位联调。

## 安装

建议直接用 Poetry 管理依赖和运行环境，而不是手动创建 venv：

```bash
poetry install
```

如果你只是想同步锁文件而不安装项目本体，也可以先跑：

```bash
poetry lock
```

把 [.env.example](.env.example) 复制成 `.env` 后填写你的 Polymarket 账户信息。

代理钱包场景重点关注这几个字段：

- `POLY_PRIVATE_KEY`
- `POLY_FUNDER`
- `POLY_USER_ADDRESS`
- `POLY_SIGNATURE_TYPE`

如果你本地访问 `data-api.polymarket.com` 或 CLOB 会被拦，可以直接在 `.env` 里配代理：

- `POLY_HTTP_PROXY`
- `POLY_HTTPS_PROXY`
- `POLY_NO_PROXY`

这几个配置会同时作用在：

- 官方 `GET /positions` 数据接口
- `py-clob-client` 的订单簿、余额、下单等请求

如果你已经有预先派生好的 API key，也可以一并填入：

- `POLY_API_KEY`
- `POLY_API_SECRET`
- `POLY_API_PASSPHRASE`

## 命令

查看命令帮助：

```bash
poetry run poly-shield --help
```

启动本地后端服务：

```bash
poetry run poly-shield serve
```

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
