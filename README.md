# poly-shield-bot

Polymarket 自动止盈止损机器人。

当前这版已经有一个能跑的 CLI 雏形，重点先落在命令行风控而不是 Telegram 交互。

## 当前已实现

- 保本止损规则：买一价小于等于均价时触发，按比例卖出。
- 指定价格止损：买一价小于等于目标价时触发，按比例卖出。
- 指定价格止盈：买一价大于等于目标价时触发，按比例卖出。
- 峰值回撤止盈：记录 watch 生命周期里的最高买一价，按设定回撤比例触发止盈，可选激活价。
- 部分成交续卖语义：规则第一次触发后锁定目标卖出数量，后续只补卖剩余数量，避免重复按比例超卖。
- 多条规则的可用仓位隔离：同一轮里前面的规则先占用仓位，后面的规则只按剩余可用仓位计算卖出比例，避免同轮超卖。
- `watch` 命令：支持常驻轮询、`--run-once` 单次执行和 `--dry-run` 只读演练。
- `watch` 输出现在会带上 `best_ask`、`top_bids` 和 `top_asks`，方便直接对照网页盘口联调。
- `positions` 命令：接官方 `GET /positions`，可自动列出账户当前全部持仓和均价，也支持按 token 过滤。
- Polymarket CLOB 接入骨架：订单簿读取、条件 token 余额读取、FAK 市价卖出、heartbeat 接口封装。

## 当前限制

- 当前环境下直接访问官方 `https://data-api.polymarket.com/positions` 会命中 `403 / error code: 1010`，更像是 Cloudflare/地理限制，不是本地解析代码错误。
- 因为上面的限制，我已经把官方 positions 接口接进代码和测试，但没法在这个环境里完成真实在线验证。
- Telegram Bot、分层梯子、任务持久化和重启恢复还没开始做。
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
poetry run poly-shield watch --help
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
	--price-stop-ratio 0.5 \
	--dry-run \
	--run-once
```

跑一个保本止损 + 固定止盈组合：

```bash
poetry run poly-shield watch \
	--token-id <TOKEN_ID> \
	--average-cost 0.42 \
	--position-size 100 \
	--breakeven-stop-ratio 0.5 \
	--take-profit 0.68 \
	--take-profit-ratio 0.25 \
	--dry-run
```

跑一个峰值回撤止盈：

```bash
poetry run poly-shield watch \
	--token-id <TOKEN_ID> \
	--trailing-drawdown 0.10 \
	--trailing-drawdown-ratio 0.50 \
	--trailing-activation-price 0.65 \
	--dry-run
```

`watch` 现在的默认数据来源是这样的：

- 如果你同时传了 `--position-size` 和 `--average-cost`，就完全走手动值。
- 如果你少传其中一个，CLI 会优先去官方 `GET /positions` 补齐缺的持仓大小或均价。
- 如果官方 positions 接口不可用，你仍然可以继续用手动参数跑 dry-run 或实盘。
- 如果本地网络受限，可以通过 `POLY_HTTP_PROXY / POLY_HTTPS_PROXY` 让 `positions` 和 `watch` 同时走代理。

换账号或换环境时，可以直接按 [docs/ACCEPTANCE_CHECKLIST.md](docs/ACCEPTANCE_CHECKLIST.md) 逐项验收。

止盈止损的详细参数说明、规则解释和示例，见 [docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md](docs/STOP_LOSS_TAKE_PROFIT_GUIDE.md)。

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
- CLI `watch --help` 可正常启动。
- 使用公开订单簿的 `watch --dry-run --run-once` 已成功跑通一次。
- 官方 positions CLI 在当前环境会被 `403 / 1010` 拦截，所以这条只完成了本地单测验证，没有完成在线实测。

## 下一步

按当前优先级，建议继续做这几件事：

1. 给 `positions` 和 `watch` 增加 geoblock / Cloudflare 1010 的更明确提示和降级策略。
2. 给真实卖单路径补一轮小仓位联调和错误码兜底。
3. 开始做 Telegram Bot 控制面，或者先做分批止盈后自动抬止损。
