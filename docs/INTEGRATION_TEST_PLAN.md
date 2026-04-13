# Poly Shield 联合测试方案

这份方案的目标不是替代单元测试，而是验证这条基本链路在真实环境里是否可用：

- CLI 能不能正常读取持仓和盘口
- 后端服务能不能启动并管理任务
- SQLite 能不能保存任务、状态和记录
- market websocket 能不能驱动 active 任务持续产生日志
- 可选的真实下单链路能不能把 order / trade 生命周期补到 records

默认原则：

- 先跑 P0 必测，再决定要不要跑 P1 可选项
- P0 尽量只用只读或 `--dry-run`
- 联调时一次只验证一个 token
- 除非明确要验证真实下单，否则一律传手动 `--position-size` 和 `--average-cost`，避免把 positions 网络问题和后端链路问题混在一起

## 1. 测试分层

### P0 必测

这组用来回答“基本功能是否可用”：

1. 本地回归测试通过
2. CLI 持仓和盘口链路可用
3. 后端服务可启动，`/health` 可观察
4. 任务可创建、列出、暂停、恢复、删除
5. active 任务可被 runtime 接管，并持续写入 records
6. 服务重启后可从 SQLite 恢复 active 任务

### P1 可选

这组用来回答“真实环境下是否已经接近可实盘”：

1. CLI 能从官方 `positions` 自动补齐仓位和均价
2. 小额真实卖单能打通 `submitted -> confirmed/failed`
3. user websocket 或 REST 对账能把终态补进 records

### P2 可靠性冒烟

这组不属于“基本功能可用”的最低门槛，但建议在准备长时间运行前补一轮：

1. 服务重启后未完成 attempt 是否会进入 `needs-review`
2. 网络断开较长时间后，任务是否会自动切到 `paused`
3. 单实例保护是否能阻止两个 runtime 同时持有执行权

## 2. 测试前准备

建议准备 3 个终端：

- 终端 A：跑后端服务
- 终端 B：发 CLI 命令创建和管理任务
- 终端 C：查 `health` 和 `records`

准备数据：

- 一个你熟悉的 `TOKEN_ID`
- 当前大致 `best_bid`
- 一组手动参数：`position_size`、`average_cost`
- 如果要跑 P1 真实下单，提前准备可接受损失的小仓位

前置条件：

- 已配置 `.env`
- 已执行 `poetry install`
- 如果网络受限，已配置 `POLY_HTTP_PROXY` / `POLY_HTTPS_PROXY`
- 如果要验证 user websocket / 实盘链路，已配置 API key / secret / passphrase

PowerShell 下查询健康检查建议用：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health | ConvertTo-Json -Depth 6
```

## 3. P0 必测用例

### JT-00 基线回归

运行：

```bash
poetry run pytest
```

通过标准：

- 全量测试通过

### JT-01 CLI 数据链路

先验证持仓接口：

```bash
poetry run poly-shield positions --size-threshold 0
```

再验证盘口只读链路：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --average-cost 0.42 \
  --take-profit <低于当前best_bid的价格> \
  --take-profit-ratio 0.25 \
  --dry-run \
  --run-once
```

通过标准：

- `positions` 能返回数组，或者明确报出 `403 / 1010` 网络限制
- `watch` 输出包含 `best_bid`、`best_ask`、`top_bids`、`top_asks`
- `watch` 输出状态为 `dry-run`
- `requested_size` 等于 `position_size * take-profit-ratio`

说明：

- 如果 `positions` 因 1010 失败，但 `watch` 在手动仓位参数下能跑通，这仍然说明核心 CLI 和盘口链路可用

### JT-02 后端启动与健康检查

终端 A 启动后端：

```bash
poetry run poly-shield serve
```

终端 C 查询健康状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health | ConvertTo-Json -Depth 6
```

通过标准：

- 服务可启动，不立即退出
- `/health` 返回 `status = ok`
- 返回里包含 `active_task_ids`
- 返回里包含 `runtime.running`、`runtime.runner_count`、`runtime.stale_seconds`

### JT-03 任务生命周期联调

终端 B 创建一个 dry-run 任务：

```bash
poetry run poly-shield tasks add \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --average-cost 0.42 \
  --take-profit <低于当前best_bid的价格> \
  --take-profit-ratio 0.25 \
  --dry-run
```

记录返回的 `task_id`，然后依次执行：

```bash
poetry run poly-shield tasks list
poetry run poly-shield tasks pause --task-id <TASK_ID>
poetry run poly-shield tasks resume --task-id <TASK_ID>
poetry run poly-shield tasks delete --task-id <TASK_ID>
```

通过标准：

- `tasks add` 返回 JSON，包含 `task_id`、`status`
- `tasks list` 能看到新任务
- `pause` 后任务状态变成 `paused`
- `resume` 后任务状态变成 `active`
- `delete` 后任务状态变成 `deleted`

失败判定：

- 如果 `tasks add` 成功但 `tasks list` 看不到，优先排查 SQLite 路径和 API 地址
- 如果状态切换失败，先查后端终端日志，再查 `records`

### JT-04 runtime 驱动 records 持续写入

重新创建一个 dry-run active 任务，这次不要立刻删除：

```bash
poetry run poly-shield tasks add \
  --token-id <TOKEN_ID> \
  --position-size 100 \
  --average-cost 0.42 \
  --take-profit <低于当前best_bid的价格> \
  --take-profit-ratio 0.25 \
  --dry-run
```

等待 10 到 20 秒后查询：

```bash
poetry run poly-shield records --limit 20
```

同时看健康状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health | ConvertTo-Json -Depth 6
```

通过标准：

- `records` 能看到该任务产生的新记录
- 至少有一条记录的 `event_type` 为 `rule`
- 至少有一条记录的 `status` 为 `dry-run`
- `/health.runtime.runner_count` 大于等于 1
- `/health.runtime.subscribed_token_ids` 包含当前 `TOKEN_ID`
- `/health.runtime.last_market_message_at` 不为 null
- `/health.runtime.stale_seconds.market` 有值且不是异常大数

说明：

- 这个用例证明的是后端、SQLite、market websocket 和 runtime 调度的联动链路可用

### JT-05 服务重启恢复

在保留一个 active 任务的前提下：

1. 停掉终端 A 的后端服务
2. 重新执行 `poetry run poly-shield serve`
3. 再查一次任务列表、records 和 `/health`

建议执行：

```bash
poetry run poly-shield tasks list
poetry run poly-shield records --limit 20
```

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health | ConvertTo-Json -Depth 6
```

通过标准：

- 重启后服务能正常启动
- 原 active 任务仍能在 `tasks list` 中看到
- `/health.active_task_ids` 仍包含该任务
- `/health.runtime.runner_count` 恢复为大于等于 1
- 重启后一段时间内 `records` 继续新增 dry-run 记录

通过 JT-00 到 JT-05，基本可以判定“当前版本的基础功能可用”。

## 4. P1 可选用例

### JT-06 positions 自动补全

这条只在你本机能访问官方 `positions` 接口时执行：

```bash
poetry run poly-shield watch \
  --token-id <TOKEN_ID> \
  --breakeven-stop-ratio 0.5 \
  --dry-run \
  --run-once
```

通过标准：

- 不手动传 `--position-size` 和 `--average-cost` 也能成功运行
- 输出里的 `trigger_price` 等于当前持仓均价
- `requested_size` 与持仓数量乘卖出比例一致

如果命中 `403 / 1010`：

- 记录为网络或代理问题，不把它算成规则或后端功能失败

### JT-07 小额真实下单链路

只有在你明确接受真实仓位变化时才执行。

先用同样参数跑 dry-run，再去掉 `--dry-run`：

```bash
poetry run poly-shield tasks add \
  --token-id <TOKEN_ID> \
  --position-size <小仓位> \
  --average-cost <真实均价或保守估计> \
  --take-profit <低于当前best_bid的价格> \
  --take-profit-ratio 0.1
```

创建后持续查询：

```bash
poetry run poly-shield records --limit 20
```

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health | ConvertTo-Json -Depth 6
```

通过标准：

- 首条规则记录包含 `order_id` 和 `market_id`
- `records` 里后续能看到 `trade confirmed` 或 `trade failed`
- 如果 user websocket 短时断开，稍后也应由 REST 对账把终态补回记录
- Polymarket 前端仓位变化与 records 描述一致

风险提示：

- 如果真实执行返回里没有 `order_id`，runtime 会把该次执行标成 `needs-review` 并暂停任务，这是当前实现的保守语义，不是 bug

## 5. P2 可靠性冒烟建议

这部分不是基础可用性的必过项，但建议在你准备长时间运行前补一轮：

### JT-08 单实例保护

在同一个数据库路径下尝试启动第二个 `serve` 实例。

预期：

- 第二个实例启动失败，或者直接报 runtime lease 冲突

### JT-09 stale 自动暂停

当前默认阈值是 market 15 秒、user 30 秒。因为 `serve` 命令没有暴露阈值参数，这条手工联调不够便宜，建议两种方式二选一：

1. 直接依赖 `tests/test_runtime.py` 里的自动化用例做覆盖
2. 手工断开网络或代理 30 秒以上，再观察任务是否进入 `paused`

手工观察重点：

- `records` 里是否新增 `event_type = system`、`status = paused`
- `message` 是否包含 `market data stale` 或 `user execution updates stale`

### JT-10 needs-review 恢复

这条更适合测试环境做故障注入：

1. 让 runtime 在 prepared / submitted 中间退出
2. 重启后观察 attempt 和任务状态

预期：

- 未完成的 attempt 会被恢复出来
- 无法确认终态的 attempt 会被标成 `needs-review`
- 对应任务会进入 `paused`

## 6. 通过结论模板

建议最后按下面格式给每一轮联调下结论：

| 维度               | 结论                   | 备注 |
| ------------------ | ---------------------- | ---- |
| CLI 数据链路       | 通过 / 不通过          |      |
| 后端启动与健康检查 | 通过 / 不通过          |      |
| 任务生命周期       | 通过 / 不通过          |      |
| runtime 持续执行   | 通过 / 不通过          |      |
| 服务重启恢复       | 通过 / 不通过          |      |
| positions 自动补全 | 通过 / 不通过 / 未执行 |      |
| 真实下单链路       | 通过 / 不通过 / 未执行 |      |

建议判定口径：

- JT-00 到 JT-05 全部通过：可认为“基本功能可用”
- JT-06 通过：可认为“自动持仓补全可用”
- JT-07 通过：可认为“真实执行闭环可用”
- JT-08 到 JT-10 通过：可认为“可靠性冒烟通过，可进入更长时间观察”
