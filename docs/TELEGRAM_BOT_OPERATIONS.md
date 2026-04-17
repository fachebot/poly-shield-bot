# Telegram Bot 启用与运维说明

本文用于说明如何在本地 `serve` 进程中启用 Telegram bot、如何维护白名单，以及如何验证 bot 已正常接入任务系统。

## 1. 运行方式概览

Telegram bot 不是单独进程，而是挂在：

- `poetry run poly-shield serve`

同一个进程里完成三件事：

- FastAPI API 与 Web 控制台
- 任务 runtime
- Telegram 长轮询与通知投递

因此 Telegram 功能的运维前提，是 `serve` 本身能成功启动。

## 2. 启用前置条件

启用 Telegram 前，需要先满足以下条件：

- 已完成 Polymarket 账号与本地私钥配置
- 已执行 `poetry install`
- 已能正常运行 `poetry run poly-shield serve`
- 已创建 Telegram bot 并拿到 token

如果 `serve` 还停留在 signer/funder 校验失败阶段，先修复基础环境，再启用 Telegram。

## 3. 启用步骤

### 3.1 保存 Telegram bot token

Telegram token 不从 `.env` 明文读取，必须写入本地加密仓库：

```bash
poetry run poly-shield secrets set-telegram-bot-token
```

也可以显式传值：

```bash
poetry run poly-shield secrets set-telegram-bot-token --value <BOT_TOKEN>
```

确认状态：

```bash
poetry run poly-shield secrets status
```

期望输出中包含：

```json
{
  "has_telegram_bot_token": true
}
```

如果要轮换或撤销 token：

```bash
poetry run poly-shield secrets clear-telegram-bot-token
```

### 3.2 配置 Telegram 开关与白名单

在 `.env` 中加入：

```env
POLY_TELEGRAM_ENABLED=true
POLY_TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
POLY_TELEGRAM_POLL_INTERVAL_SECONDS=5
```

字段说明：

- `POLY_TELEGRAM_ENABLED`：是否启用 Telegram bot。
- `POLY_TELEGRAM_ALLOWED_USER_IDS`：逗号分隔的 Telegram 数字 user id 白名单。
- `POLY_TELEGRAM_POLL_INTERVAL_SECONDS`：轮询退避/重试节奏，默认 `5` 秒，必须大于 `0`。

如果本地网络访问 Telegram 受限，Telegram bot 也会复用同一套代理变量：

- `POLY_HTTP_PROXY`
- `POLY_HTTPS_PROXY`
- `POLY_NO_PROXY`

注意：

- 白名单必须填写数字 user id，不支持 Telegram 用户名。
- 多个 id 之间使用英文逗号分隔。
- 空字符串或非法字符会导致启动失败。

### 3.3 启动服务

```bash
poetry run poly-shield serve
```

如果 Telegram 配置不完整，启动会直接失败，常见报错为：

- `Startup aborted because Telegram is enabled but POLY_TELEGRAM_ALLOWED_USER_IDS is empty.`
- `Startup aborted because Telegram is enabled but no Telegram bot token is stored locally.`

## 4. 白名单 user id 获取方式

`POLY_TELEGRAM_ALLOWED_USER_IDS` 需要填 Telegram 的数字 user id，也就是 update payload 里的 `message.from.id`。

推荐两种方式。

### 方式 A：通过 bot API 自查

1. 先给你的 bot 发一条私聊消息，例如 `/start`。
2. 用 token 调 Telegram Bot API：

```powershell
Invoke-RestMethod "https://api.telegram.org/bot<BOT_TOKEN>/getUpdates"
```

3. 在返回 JSON 中查找：

- `result[].message.from.id`：这是应写入白名单的 user id
- `result[].message.chat.id`：私聊场景通常也会是对应聊天 id

建议白名单以 `from.id` 为准，不要只抄 `chat.id`。

### 方式 B：借助现成的 Telegram ID 查询 bot

也可以先用类似 `@userinfobot` 之类的 Telegram 工具 bot 查询自己的 numeric id，再填回 `.env`。

这种方式更省事，但依赖第三方 bot。生产或长期运维场景，优先推荐方式 A。

## 5. 首次登记与通知接收

白名单只解决“允许谁操作”，不自动解决“通知发到哪里”。

bot 会在白名单用户首次通过私聊发送消息时，自动登记 Telegram recipient。实际运维上建议：

1. 启动 `serve`
2. 每个白名单用户都手动私聊 bot 一次 `/start`
3. 再观察后续任务通知是否送达

如果某个用户还没给 bot 发过私聊消息，即使他已经在白名单中，也可能还没有可投递的 recipient 记录。

## 6. 启用后的验证方法

### 6.1 用命令检查本地密钥仓库

```bash
poetry run poly-shield secrets status
```

重点确认：

- `has_telegram_bot_token` 为 `true`

### 6.2 用健康接口检查服务状态

服务启动后访问：

```text
http://127.0.0.1:8787/health
```

重点看这些字段：

- `local_security.telegram_enabled`
- `local_security.telegram_whitelist_enabled`
- `local_security.telegram_whitelist_size`
- `local_security.telegram_poll_interval_seconds`
- `telegram.running`
- `telegram.registered_recipient_count`
- `telegram.pending_notification_count`
- `telegram.active_wizard_count`
- `telegram.last_poll_error`

### 6.3 用 bot 自检

让白名单用户在私聊中依次执行：

```text
/start
/help
/health
```

通过标准：

- `/start` 能返回命令列表
- `/help` 能显示完整命令集
- `/health` 能返回 active task、recipient 和 runtime 概览

## 7. Telegram Bot 命令文档

### 基础命令

- `/start`：登记当前会话，并返回帮助信息。
- `/help`：显示全部命令。
- `/health`：查看 runtime、recipient 和 Telegram 内部状态。

### 查询命令

- `/tasks [status]`：列出任务；可选状态如 `active`、`paused`、`completed`、`deleted`。
- `/task <task_id>`：查看单个任务详情，包括规则和规则运行态。
- `/records [task_id]`：查看最近执行记录；可选按任务过滤。

### 控制命令

- `/pause <task_id>`：暂停任务。
- `/resume <task_id>`：恢复已暂停任务。
- `/delete <task_id>`：软删除任务。

### 向导命令

- `/create`：进入任务创建向导。
- `/edit <task_id>`：进入任务编辑向导，仅允许编辑 `paused` 任务。
- `/cancel`：取消当前向导。

## 8. 向导输入规则

### `/create` 向导

创建向导会依次询问：

1. `token_id`
2. `dry_run`（`yes` / `no`）
3. `slippage_bps`
4. 可选 `position_size`（输入数值或 `skip`）
5. 可选 `average_cost`（输入数值或 `skip`）
6. 规则循环：
   - `breakeven-stop`
   - `price-stop`
   - `take-profit`
   - `trailing-take-profit`
7. 规则参数：`sell_size`，以及按规则类型需要的 `trigger_price` / `drawdown_ratio` / `activation_price`
8. 可选规则标签（输入文本或 `skip`）
9. 输入 `done` 结束加规则
10. 输入 `confirm` 落库

### `/edit` 向导

编辑向导只允许针对 `paused` 任务。它会从当前任务值出发，支持这些控制词：

- `keep`：保留当前值
- `clear`：清空当前可选字段
- `replace`：重建规则集合
- `confirm`：保存修改
- `/cancel`：中止向导

可编辑字段包括：

- `dry_run`
- `slippage_bps`
- `position_size`
- `average_cost`
- 规则集合

## 9. 运维建议

- 只把真实需要控制 bot 的个人账号加入白名单，不要直接加群。
- bot 只接受 private chat；如果在群里测试，命令不会生效。
- 新加白名单用户后，让他先私聊一次 `/start`，否则不会形成 recipient 记录。
- 轮换 token 时，先执行 `clear-telegram-bot-token` 再写入新 token。
- 如果主要依赖 Telegram 管理任务，建议同时启用 UI Basic Auth，避免 Web 入口裸奔。

## 10. 常见问题

### 现象：`/edit` 返回不能编辑

原因：当前任务不是 `paused`。

处理：

1. 先执行 `/pause <task_id>`
2. 再执行 `/edit <task_id>`

### 现象：白名单用户能发命令，但没收到通知

优先检查：

1. 是否已经私聊 bot 至少一次
2. `/health` 里的 `telegram.registered_recipient_count` 是否大于 `0`
3. `/health` 里的 `telegram.pending_notification_count` 是否持续堆积
4. `/health` 里的 `telegram.last_poll_error` 是否非空

### 现象：`serve` 启动即退出

优先检查：

1. `POLY_TELEGRAM_ENABLED` 是否为 `true`
2. `POLY_TELEGRAM_ALLOWED_USER_IDS` 是否为空或格式错误
3. `poetry run poly-shield secrets status` 中 `has_telegram_bot_token` 是否为 `true`
4. signer/funder 基础配置是否已经先通过启动前校验
