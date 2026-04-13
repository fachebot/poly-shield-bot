# 止盈止损使用说明

这份文档专门说明 `poly-shield` 当前这版 CLI 的止盈止损用法，重点回答四件事：

- 每种规则什么时候触发
- 参数应该怎么传
- 不同规则可以怎么组合
- 联调和实盘前应该怎么验证

如果你是第一次使用，建议先通读“快速开始”和“规则说明”，然后直接照着“示例”里的命令跑一遍。

## 适用范围

当前文档对应的是现在这版 CLI，支持以下规则：

- 保本止损
- 指定价格止损
- 指定价格止盈
- 峰值回撤止盈

当前版本的边界也要先说清楚：

- 每次 `watch` 只监控一个 token
- 保本止损和固定止损二选一，不能同时开
- 可以在一条 `watch` 任务里叠加固定止盈和峰值回撤止盈
- `trailing` 的峰值只保存在内存里，进程重启后会重新开始记录

## 快速开始

推荐按下面顺序使用：

1. 先确认能读到当前持仓

```bash
poetry run poly-shield positions --size-threshold 0
```

2. 先用 `--dry-run --run-once` 做一次只读验证

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --price-stop 0.40 \
  --price-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

3. 确认触发逻辑、盘口和卖出数量都符合预期后，再决定是否去掉 `--dry-run`

## watch 的运行方式

`watch` 是核心命令。它每轮会做这几件事：

1. 读取当前持仓数量和均价
2. 读取当前 token 的盘口
3. 计算规则是否触发
4. 如果触发，计算本轮应该卖出的数量
5. `--dry-run` 时只输出结果；实盘时才提交卖单

几个最重要的控制参数：

- `--dry-run`：只做演练，不真正下单
- `--run-once`：只执行一轮后退出，适合联调
- `--poll-interval`：常驻模式下的轮询间隔，默认 5 秒

## 持仓数据来源

`watch` 的持仓和均价有两种来源。

第一种是完全手动：

- 同时传 `--position-size` 和 `--average-cost`
- 这时不会去官方 `positions` 接口补齐数据

第二种是自动补全：

- 如果你少传其中一个，CLI 会尝试从官方 `GET /positions` 读取缺失数据
- 适合保本止损和日常联调

建议这样理解：

- 想彻底手控，就两个都传
- 想省事，就只传缺的那一个，剩下的让官方持仓接口补

## 输出字段怎么看

`watch` 现在会输出 JSON，常用字段含义如下：

- `best_bid`：当前买一价，也是所有止损/止盈判断的核心价格
- `best_ask`：当前卖一价，主要用于和网页盘口对照
- `top_bids`：买盘前几档
- `top_asks`：卖盘前几档
- `trigger_price`：这条规则当前使用的触发价
- `requested_size`：本轮计划卖出的数量
- `filled_size`：本轮实际成交数量，`dry-run` 时通常是 0
- `status`：当前状态，常见值有 `waiting`、`dry-run`、`submitted`、`partial`、`matched`、`completed`

一个典型输出长这样：

```json
{
  "token_id": "...",
  "rule": "price-stop",
  "status": "waiting",
  "best_bid": "0.084",
  "best_ask": "0.088",
  "top_bids": [
    { "price": "0.084", "size": "55.69" },
    { "price": "0.083", "size": "67.39" }
  ],
  "top_asks": [
    { "price": "0.088", "size": "15" },
    { "price": "0.089", "size": "56.37" }
  ],
  "trigger_price": "0.07",
  "requested_size": "0",
  "filled_size": "0",
  "message": "best bid 0.084 has not reached 0.07"
}
```

## 规则说明

## 1. 保本止损

适用场景：

- 你希望价格跌回均价时先卖掉一部分，降低回撤

触发条件：

- `best_bid <= average_cost`

必须参数：

- `--breakeven-stop-ratio`

均价来源：

- 手动传 `--average-cost`
- 或者让 `positions` 自动补齐

示例：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --breakeven-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

说明：

- 如果当前持仓均价是 `0.065`
- 当前 `best_bid` 是 `0.064`
- 那就会触发保本止损
- 如果持仓数量是 `100`，卖出比例是 `0.5`，那 `requested_size` 就是 `50`

## 2. 指定价格止损

适用场景：

- 你不想用均价做触发，而是直接指定一个明确止损位

触发条件：

- `best_bid <= price_stop`

必须参数：

- `--price-stop`
- `--price-stop-ratio`

示例：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --price-stop 0.40 \
  --price-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

说明：

- 当买一价跌到 `0.40` 或更低时触发
- 如果仓位是 `100`，比例是 `0.5`，则本轮计划卖出 `50`

## 3. 指定价格止盈

适用场景：

- 你已经有明确的目标价，到了就卖一部分

触发条件：

- `best_bid >= take_profit`

必须参数：

- `--take-profit`
- `--take-profit-ratio`

示例：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --take-profit 0.68 \
  --take-profit-ratio 0.25 \
  --dry-run \
  --run-once
```

说明：

- 当买一价涨到 `0.68` 或更高时触发
- 如果仓位是 `100`，比例是 `0.25`，则本轮计划卖出 `25`

## 4. 峰值回撤止盈

适用场景：

- 你不想提前写死止盈价，而是希望价格先上涨，之后从峰值回撤一定比例时再止盈

核心逻辑：

1. `watch` 运行后开始记录最高 `best_bid`
2. 一旦价格从峰值回撤达到设定比例，就触发

必须参数：

- `--trailing-drawdown`
- `--trailing-drawdown-ratio`

可选参数：

- `--trailing-activation-price`

示例：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --trailing-drawdown 0.10 \
  --trailing-drawdown-ratio 0.50 \
  --trailing-activation-price 0.65 \
  --dry-run
```

举例说明：

- 激活价设为 `0.65`
- 价格先涨到 `0.80`
- 峰值回撤比例设为 `0.10`
- 那么动态触发价就是 `0.80 × (1 - 0.10) = 0.72`
- 当 `best_bid <= 0.72` 时触发

注意：

- `trailing` 的峰值只在当前进程内有效
- 如果你重启了 `watch`，峰值会重新开始记录

## 规则组合怎么用

当前 CLI 每次最常见的组合有三种。

第一种：保本止损 + 固定止盈

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --breakeven-stop-ratio 0.5 \
  --take-profit 0.68 \
  --take-profit-ratio 0.25 \
  --dry-run
```

适合：

- 上行时先止盈一部分
- 下行回到成本时再减仓

第二种：固定止损 + 固定止盈

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --price-stop 0.40 \
  --price-stop-ratio 0.5 \
  --take-profit 0.68 \
  --take-profit-ratio 0.25 \
  --dry-run
```

适合：

- 你已经有明确的上下边界

第三种：固定止损 + 峰值回撤止盈

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --price-stop 0.40 \
  --price-stop-ratio 0.5 \
  --trailing-drawdown 0.10 \
  --trailing-drawdown-ratio 0.25 \
  --trailing-activation-price 0.65 \
  --dry-run
```

适合：

- 下行风险想固定住
- 上行部分不想太早写死止盈点

当前限制：

- 保本止损和固定止损不能同时开
- 每轮内部会自动做可用仓位扣减，避免同轮超卖

## 示例

## 示例 1：先查持仓，再自动保本止损

先查当前仓位：

```bash
poetry run poly-shield positions --size-threshold 0
```

然后直接跑保本止损：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --breakeven-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

适合：

- 已经有真实持仓
- 不想手填均价

## 示例 2：只手动覆盖仓位，均价自动补全

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 40 \
  --breakeven-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

适合：

- 你想只卖一部分仓位
- 但仍然希望均价来自官方持仓接口

## 示例 3：固定止盈的只读联调

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --take-profit 0.118 \
  --take-profit-ratio 0.25 \
  --dry-run \
  --run-once
```

适合：

- 联调输出字段
- 验证触发价是否和网页盘口一致

## 示例 4：持续运行的常驻监控

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --take-profit 0.118 \
  --take-profit-ratio 0.25 \
  --dry-run \
  --poll-interval 2
```

说明：

- 不加 `--run-once` 就会常驻运行
- 输出会每轮打印一次 JSON

## 实盘前建议

如果你准备从联调切到真实卖单，建议按这个顺序做：

1. 先用完全相同参数跑一遍 `--dry-run`
2. 对照 `best_bid`、`best_ask`、`top_bids`、`top_asks` 和网页盘口
3. 确认 `requested_size` 是你愿意实际卖出的数量
4. 第一次实盘只用小仓位
5. 同一个 token 不要同时开多个 `watch` 进程

## 常见问题

## 为什么我没传 `--average-cost` 也能跑？

因为 CLI 会优先从官方 `positions` 接口补齐缺失的均价和仓位。

## 为什么我传了 `--dry-run`，状态是 `dry-run` 但没有成交？

这是正常现象。`dry-run` 只做规则评估，不会真正提交卖单。

## 为什么 `trailing` 重启后行为变了？

因为峰值是内存态，不会持久化。进程重启后，峰值会重新开始记录。

## 为什么自动持仓有时会失败？

常见原因有两个：

- 当前网络访问不到官方 `positions` 接口
- 没有配置可用代理

遇到这种情况，你可以：

- 手动传 `--position-size` 和 `--average-cost`
- 或者先修好代理，再使用自动补全

## 进一步验收

如果你是换账号、换机器或换网络，建议继续结合 [docs/ACCEPTANCE_CHECKLIST.md](docs/ACCEPTANCE_CHECKLIST.md) 逐项跑完验收清单。
