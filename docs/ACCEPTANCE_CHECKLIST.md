# Poly Shield 验收清单

这份清单用于两类场景：

- 换账号后重跑联调
- 换机器、换网络或换代理后做环境验收

默认原则：

- 除最后一节外，全部使用只读或 `--dry-run`。
- 先验证数据链路，再考虑真实卖单。
- 每次只盯一个 token，避免把多个变量混在一起。

## 0. 前置条件

- 已配置 `.env`
- 已执行 `poetry install`
- 如果本地网络受限，已配置 `POLY_HTTP_PROXY` / `POLY_HTTPS_PROXY`

建议先确认 CLI 可启动：

```bash
poetry run poly-shield watch --help
```

通过标准：

- 命令可正常输出帮助信息

## 1. 基线校验

运行：

```bash
poetry run pytest
```

通过标准：

- 全量测试通过

## 2. 持仓接口校验

运行：

```bash
poetry run poly-shield positions --size-threshold 0
```

通过标准：

- 能返回至少一笔真实持仓，字段包含 `token_id`、`size`、`average_cost`
- 如果没有持仓，也应该返回空数组而不是报错

失败记录：

- 如果命中 `403 / 1010`，记录为网络或代理问题，不要误判为解析逻辑问题

## 3. 盘口输出校验

挑一笔真实 token，运行：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --price-stop 0.07 \
  --price-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

通过标准：

- 输出中包含 `best_bid`
- 输出中包含 `best_ask`
- 输出中包含 `top_bids` 和 `top_asks`
- `best_bid` / `best_ask` 与 Polymarket 网页盘口大致一致
- `top_bids[0].price == best_bid`
- `top_asks[0].price == best_ask`

## 4. 自动持仓补全校验

不要手动传 `--position-size` 和 `--average-cost`，直接运行：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --breakeven-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

通过标准：

- CLI 能从 `positions` 自动补齐仓位和均价
- `trigger_price` 等于当前持仓均价
- `requested_size` 等于持仓数量乘以卖出比例

## 5. 覆盖逻辑校验

仅覆盖仓位，均价走自动补全：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 40 \
  --breakeven-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

仅覆盖均价，仓位走自动补全：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --average-cost 0.50 \
  --breakeven-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

通过标准：

- 只覆盖一个字段时，另一个字段仍能自动补齐
- `requested_size` 与覆盖后的参数一致

## 6. 固定止盈校验

选一笔实时 `best_bid` 已知的持仓，运行：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --take-profit <低于当前best_bid的价格> \
  --take-profit-ratio 0.25 \
  --dry-run \
  --run-once
```

通过标准：

- 状态为 `dry-run`
- `requested_size` 等于持仓数量乘以 0.25

## 7. 峰值回撤止盈校验

运行：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --trailing-drawdown 0.05 \
  --trailing-drawdown-ratio 0.25 \
  --trailing-activation-price 0.11 \
  --dry-run \
  --run-once
```

通过标准：

- 首轮通常应为 `waiting`
- `message` 中应能看到当前峰值和回撤阈值

## 8. 短时常驻稳定性校验

运行 2 到 5 分钟：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --take-profit <低于当前best_bid的价格> \
  --take-profit-ratio 0.25 \
  --dry-run \
  --poll-interval 2
```

通过标准：

- 持续输出事件
- 不崩溃
- `best_bid` / `best_ask` 字段持续存在

## 9. 可选：小额真实卖单验收

只有在你明确要动真实仓位时才执行。

建议流程：

1. 先用完全相同参数跑一遍 `--dry-run`
2. 确认 `requested_size` 合理
3. 去掉 `--dry-run` 再执行一次

模板：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size <小仓位> \
  --price-stop <高于当前best_bid一点的价格> \
  --price-stop-ratio 0.1 \
  --run-once
```

通过标准：

- 返回 `submitted`、`matched` 或 `partial`
- Polymarket 前端可看到仓位变化

## 10. 结果记录模板

每次联调建议至少记录这些信息：

| 项目           | 记录值      |
| -------------- | ----------- |
| 日期           |             |
| 账号           |             |
| 网络模式       | 直连 / 代理 |
| token_id       |             |
| 命令           |             |
| best_bid       |             |
| best_ask       |             |
| requested_size |             |
| status         |             |
| 是否符合预期   | 是 / 否     |
| 备注           |             |