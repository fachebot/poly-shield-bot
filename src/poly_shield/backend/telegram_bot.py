from __future__ import annotations

"""Telegram bot controller for mobile-friendly task control and notifications."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Protocol
from urllib import error, request

from poly_shield.backend.models import NotificationChannel, NotificationDeliveryStatus, NotificationOutboxEntry, TaskStatus, utc_now
from poly_shield.backend.security import LocalAccessSecuritySettings
from poly_shield.backend.service import TaskConflictError, TaskNotFoundError, TaskService
from poly_shield.config import PolymarketCredentials, apply_proxy_environment_from_env
from poly_shield.polymarket import PolymarketConfigurationError, PolymarketGateway, PolymarketRequestError
from poly_shield.positions import PositionReader, PositionRecord
from poly_shield.rules import ExitRule, RuleKind


RULE_KIND_ALIASES: dict[str, RuleKind] = {
    "breakeven-stop": RuleKind.BREAKEVEN_STOP,
    "breakeven": RuleKind.BREAKEVEN_STOP,
    "break-even": RuleKind.BREAKEVEN_STOP,
    "保本止损": RuleKind.BREAKEVEN_STOP,
    "price-stop": RuleKind.PRICE_STOP,
    "stop": RuleKind.PRICE_STOP,
    "价格止损": RuleKind.PRICE_STOP,
    "take-profit": RuleKind.TAKE_PROFIT,
    "tp": RuleKind.TAKE_PROFIT,
    "止盈": RuleKind.TAKE_PROFIT,
    "固定止盈": RuleKind.TAKE_PROFIT,
    "trailing-take-profit": RuleKind.TRAILING_TAKE_PROFIT,
    "trailing": RuleKind.TRAILING_TAKE_PROFIT,
    "追踪止盈": RuleKind.TRAILING_TAKE_PROFIT,
    "移动止盈": RuleKind.TRAILING_TAKE_PROFIT,
}

COMMAND_DESCRIPTIONS: tuple[tuple[str, str], ...] = (
    ("/start", "重新登记当前私聊会话"),
    ("/help", "查看命令列表"),
    ("/health", "查看运行状态"),
    ("/tasks [status]", "查看任务列表"),
    ("/task <task_id>", "查看任务详情"),
    ("/positions [关键词]", "查询当前仓位"),
    ("/records [task_id]", "查看最近执行记录"),
    ("/create", "创建新任务"),
    ("/edit <task_id>", "编辑已暂停任务"),
    ("/pause <task_id>", "暂停任务"),
    ("/resume <task_id>", "恢复任务"),
    ("/delete <task_id>", "删除任务"),
    ("/cancel", "取消当前向导"),
)

TASK_STATUS_LABELS: dict[TaskStatus, str] = {
    TaskStatus.ACTIVE: "运行中",
    TaskStatus.PAUSED: "已暂停",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "已取消",
    TaskStatus.DELETED: "已删除",
}

RULE_KIND_LABELS: dict[RuleKind, str] = {
    RuleKind.BREAKEVEN_STOP: "保本止损",
    RuleKind.PRICE_STOP: "价格止损",
    RuleKind.TAKE_PROFIT: "固定止盈",
    RuleKind.TRAILING_TAKE_PROFIT: "追踪止盈",
}

GENERIC_STATUS_LABELS: dict[str, str] = {
    "prepared": "已准备",
    "submitted": "已提交",
    "confirmed": "已确认",
    "failed": "失败",
    "needs-review": "待复核",
    "matched": "已匹配",
    "active": "运行中",
    "paused": "已暂停",
    "completed": "已完成",
    "cancelled": "已取消",
    "deleted": "已删除",
}

EVENT_TYPE_LABELS: dict[str, str] = {
    "rule": "规则",
    "trade": "成交",
    "order": "订单",
    "system": "系统",
}

NOTIFICATION_BODY_LABELS: dict[str, str] = {
    "task_id": "任务ID",
    "token_id": "Token",
    "status": "状态",
    "dry_run": "模拟执行",
    "rule_count": "规则数量",
    "event_type": "事件类型",
    "rule": "规则",
    "requested_size": "请求数量",
    "filled_size": "成交数量",
    "message": "说明",
    "order_id": "订单ID",
}


class TelegramTransport(Protocol):
    async def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        ...

    async def send_message(self, *, chat_id: int, text: str) -> None:
        ...


class TelegramHttpTransport:
    def __init__(self, token: str) -> None:
        self.token = token.strip()
        if not self.token:
            raise ValueError("telegram bot token cannot be empty")
        apply_proxy_environment_from_env()
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    async def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await asyncio.to_thread(
            self._request_json,
            "getUpdates",
            payload,
            timeout_seconds + 5,
        )
        if not isinstance(result, list):
            raise RuntimeError("unexpected Telegram getUpdates result")
        return [item for item in result if isinstance(item, dict)]

    async def send_message(self, *, chat_id: int, text: str) -> None:
        await asyncio.to_thread(
            self._request_json,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            15,
        )

    def _request_json(self, method_name: str, payload: dict[str, Any], timeout_seconds: int) -> Any:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=f"{self.base_url}/{method_name}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"telegram API error: {exc.code} {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"telegram API unreachable: {exc.reason}") from exc
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            raise RuntimeError(
                f"telegram API returned error payload: {parsed!r}")
        return parsed.get("result")


@dataclass(frozen=True)
class TelegramMessageContext:
    update_id: int
    user_id: int
    chat_id: int
    chat_type: str
    text: str


@dataclass
class TelegramWizardSession:
    mode: str
    chat_id: int
    step: str
    task_id: str | None = None
    token_id: str | None = None
    title: str | None = None
    dry_run: bool | None = None
    slippage_bps: Decimal | None = None
    position_size: Decimal | None = None
    average_cost: Decimal | None = None
    rules: list[ExitRule] = field(default_factory=list)
    pending_rule_kind: RuleKind | None = None
    pending_rule_sell_size: Decimal | None = None
    pending_rule_trigger_price: Decimal | None = None
    pending_rule_drawdown_ratio: Decimal | None = None


@dataclass
class TelegramBotController:
    service: TaskService
    settings: LocalAccessSecuritySettings
    transport: TelegramTransport
    position_reader: PositionReader | None = None
    runtime_snapshot_provider: Callable[[], dict[str, object]] | None = None
    refresh_runtime: Callable[[], Awaitable[None]] | None = None
    update_timeout_seconds: int = 30
    notification_batch_size: int = 50
    _stop_event: asyncio.Event | None = field(
        default=None, init=False, repr=False)
    _update_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False)
    _notification_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False)
    _next_update_offset: int | None = field(
        default=None, init=False, repr=False)
    _last_poll_error: str | None = field(default=None, init=False, repr=False)
    _wizard_sessions: dict[int, TelegramWizardSession] = field(
        default_factory=dict, init=False, repr=False)
    _default_position_reader: PositionReader | None = field(
        default=None, init=False, repr=False)

    async def start(self) -> None:
        if self._update_task is not None or not self.settings.telegram_enabled:
            return
        if not self.settings.telegram_whitelist_enabled:
            raise RuntimeError(
                "Telegram bot enabled but no Telegram whitelist user IDs are configured")
        self._stop_event = asyncio.Event()
        self._update_task = asyncio.create_task(
            self._run_updates(), name="poly-shield-telegram-updates")
        self._notification_task = asyncio.create_task(
            self._run_notification_delivery(),
            name="poly-shield-telegram-notifications",
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        tasks = [task for task in (
            self._update_task, self._notification_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._update_task = None
        self._notification_task = None
        self._stop_event = None

    def snapshot(self) -> dict[str, object]:
        pending = self.service.list_notification_outbox(
            status=NotificationDeliveryStatus.PENDING,
            channel=NotificationChannel.TELEGRAM,
            ready_only=False,
            limit=1000,
        )
        return {
            "running": self._update_task is not None,
            "registered_recipient_count": len(self.service.list_telegram_recipients()),
            "pending_notification_count": len(pending),
            "whitelist_size": len(self.settings.telegram_allowed_user_ids),
            "active_wizard_count": len(self._wizard_sessions),
            "last_poll_error": self._last_poll_error,
        }

    async def handle_update(self, update: dict[str, Any]) -> None:
        context = self._extract_message_context(update)
        if context is None:
            return
        self._next_update_offset = context.update_id + 1

        if context.chat_type != "private":
            if context.user_id in self.settings.telegram_allowed_user_ids:
                await self.transport.send_message(
                    chat_id=context.chat_id,
                    text="请在与机器人私聊时使用命令，群聊里不会执行控制操作。",
                )
            return

        if context.user_id not in self.settings.telegram_allowed_user_ids:
            await self.transport.send_message(
                chat_id=context.chat_id,
                text="当前账号不在 Telegram 白名单中，无法操作机器人。",
            )
            return

        self.service.register_telegram_recipient(
            telegram_user_id=context.user_id,
            chat_id=context.chat_id,
            chat_type=context.chat_type,
        )

        try:
            if context.text.startswith("/"):
                command, arguments = self._parse_command(context.text)
                if command == "/cancel":
                    response = self._cancel_wizard(context.user_id)
                elif command in {"/create", "/edit"}:
                    response = await self._dispatch_command(command, arguments, context=context)
                elif context.user_id in self._wizard_sessions:
                    response = "当前有未完成的向导，请按提示继续回复，或发送 /cancel 取消。"
                else:
                    response = await self._dispatch_command(command, arguments, context=context)
            elif context.user_id in self._wizard_sessions:
                response = await self._handle_wizard_message(context)
            else:
                response = self._render_help(prefix="未识别的命令。")
        except TaskNotFoundError:
            response = "未找到对应任务。"
        except TaskConflictError as exc:
            response = f"任务操作被拒绝：{exc}"
        except ValueError as exc:
            response = f"输入有误：{exc}"

        if response:
            await self.transport.send_message(chat_id=context.chat_id, text=response)

    async def deliver_pending_notifications_once(self) -> None:
        entries = self.service.list_notification_outbox(
            status=NotificationDeliveryStatus.PENDING,
            channel=NotificationChannel.TELEGRAM,
            ready_only=True,
            limit=self.notification_batch_size,
        )
        if not entries:
            return
        recipients = {
            recipient.recipient_id: recipient
            for recipient in self.service.list_telegram_recipients()
        }
        for entry in entries:
            recipient = recipients.get(entry.recipient_id)
            if recipient is None:
                updated = entry.mark_for_retry(
                    last_error="telegram recipient missing",
                    available_at=utc_now() + timedelta(seconds=60),
                )
                self.service.update_notification_outbox_entry(updated)
                continue
            try:
                await self.transport.send_message(
                    chat_id=recipient.chat_id,
                    text=self._format_outbox_message(entry),
                )
            except Exception as exc:
                updated = entry.mark_for_retry(
                    last_error=str(exc),
                    available_at=utc_now() + timedelta(seconds=self._retry_delay_seconds(entry)),
                )
            else:
                updated = entry.mark_delivered()
            self.service.update_notification_outbox_entry(updated)

    async def _run_updates(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                updates = await self.transport.get_updates(
                    offset=self._next_update_offset,
                    timeout_seconds=self.update_timeout_seconds,
                )
                self._last_poll_error = None
                for update in updates:
                    await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_poll_error = str(exc)
                await self._sleep_or_stop(self.settings.telegram_poll_interval_seconds)

    async def _run_notification_delivery(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.deliver_pending_notifications_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_poll_error = str(exc)
            await self._sleep_or_stop(self.settings.telegram_poll_interval_seconds)

    async def _dispatch_command(
        self,
        command: str,
        arguments: list[str],
        *,
        context: TelegramMessageContext | None,
    ) -> str:
        if command == "/start":
            return self._render_help(prefix="已连接 Poly Shield Telegram 控制台。")
        if command == "/help":
            return self._render_help()
        if command == "/health":
            return self._render_health()
        if command == "/tasks":
            return self._render_tasks(arguments)
        if command == "/task":
            return self._render_task(arguments)
        if command == "/positions":
            return self._render_positions(arguments)
        if command == "/records":
            return self._render_records(arguments)
        if command == "/create":
            if context is None:
                raise ValueError("缺少 Telegram 会话上下文，无法启动创建向导")
            return self._start_create_wizard(context)
        if command == "/edit":
            if context is None:
                raise ValueError("缺少 Telegram 会话上下文，无法启动编辑向导")
            return self._start_edit_wizard(context, arguments)
        if command == "/pause":
            return await self._change_task_status(arguments, action="pause")
        if command == "/resume":
            return await self._change_task_status(arguments, action="resume")
        if command == "/delete":
            return await self._change_task_status(arguments, action="delete")
        return self._render_help(prefix="未识别的命令。")

    def _render_help(self, *, prefix: str | None = None) -> str:
        body = "\n".join(
            f"{command} - {description}" for command, description in COMMAND_DESCRIPTIONS
        )
        return self._compose_message("命令列表", body, prefix=prefix)

    def _render_health(self) -> str:
        active_tasks = self.service.list_tasks(status=TaskStatus.ACTIVE)
        runtime_snapshot = self.runtime_snapshot_provider(
        ) if self.runtime_snapshot_provider is not None else None
        lines = [
            f"活跃任务数：{len(active_tasks)}",
            f"已登记会话：{len(self.service.list_telegram_recipients())}",
            f"白名单用户数：{len(self.settings.telegram_allowed_user_ids)}",
            f"活跃向导数：{len(self._wizard_sessions)}",
            f"最近轮询错误：{self._last_poll_error or '无'}",
        ]
        if runtime_snapshot is not None:
            stale_seconds = runtime_snapshot.get("stale_seconds", {})
            lines.extend(
                [
                    f"Runtime 运行中：{self._format_bool_label(runtime_snapshot.get('running') is True)}",
                    f"Runner 数量：{runtime_snapshot.get('runner_count')}",
                    f"跟踪订单数：{runtime_snapshot.get('tracked_order_count')}",
                    f"Market 陈旧秒数：{stale_seconds.get('market') if stale_seconds.get('market') is not None else '无'}",
                    f"User 陈旧秒数：{stale_seconds.get('user') if stale_seconds.get('user') is not None else '无'}",
                ]
            )
        return self._compose_message("系统状态", "\n".join(lines))

    def _render_tasks(self, arguments: list[str]) -> str:
        status = None
        if arguments:
            status = self._parse_task_status(arguments[0])
        tasks = self.service.list_tasks(status=status)
        if not tasks:
            return self._compose_message("任务列表", "当前没有任务。")
        visible_tasks = tasks[:20]
        blocks = [
            self._format_task_summary_block(task, index)
            for index, task in enumerate(visible_tasks, start=1)
        ]
        suffix = None
        if len(tasks) > len(visible_tasks):
            suffix = f"仅显示前 {len(visible_tasks)} 个任务，共 {len(tasks)} 个。"
        return self._compose_blocks_message(
            "任务列表",
            blocks,
            prefix=(
                f"筛选状态：{self._format_task_status(status)}" if status is not None else f"共 {len(tasks)} 个任务"),
            suffix=suffix,
        )

    def _render_task(self, arguments: list[str]) -> str:
        if not arguments:
            raise ValueError("用法：/task <task_id>")
        task = self.service.get_task(arguments[0])
        states = self.service.load_rule_states(task.task_id)
        summary_lines = [
            f"名称：{task.title or task.token_id}",
            f"任务ID：{task.task_id}",
            f"Token：{task.token_id}",
            f"状态：{self._format_task_status(task.status)}",
            f"模式：{'模拟执行' if task.dry_run else '实盘执行'}",
            f"滑点：{task.slippage_bps} bps",
        ]
        if task.position_size is not None:
            summary_lines.append(f"仓位覆盖：{task.position_size}")
        if task.average_cost is not None:
            summary_lines.append(f"均价覆盖：{task.average_cost}")
        blocks = ["\n".join(summary_lines)]
        for index, rule in enumerate(task.rules, start=1):
            state = states.get(rule.name)
            blocks.append(self._format_rule_block(rule, state, index))
        return self._compose_blocks_message("任务详情", blocks)

    def _render_positions(self, arguments: list[str]) -> str:
        keyword = " ".join(arguments).strip().lower() if arguments else ""
        try:
            reader = self._get_position_reader()
            positions = reader.list_positions(size_threshold=Decimal("0"))
        except PolymarketConfigurationError as exc:
            return self._compose_message(
                "仓位查询",
                f"仓位查询未就绪：{exc}\n请先确认私钥、funder 和代理配置。",
            )
        except PolymarketRequestError as exc:
            return self._compose_message(
                "仓位查询",
                f"仓位查询失败：{exc}\n如果当前网络受限，请检查 POLY_HTTPS_PROXY 是否可用。",
            )

        if keyword:
            positions = [
                position for position in positions
                if self._position_matches_keyword(position, keyword)
            ]
        if not positions:
            return self._compose_message("仓位查询", "当前没有匹配的仓位。")

        visible_positions = positions[:10]
        blocks = [
            self._format_position_block(position, index)
            for index, position in enumerate(visible_positions, start=1)
        ]
        suffix = None
        if len(positions) > len(visible_positions):
            suffix = f"仅显示前 {len(visible_positions)} 条仓位，共 {len(positions)} 条。"
        prefix = f"关键词：{keyword}" if keyword else f"共 {len(positions)} 条仓位"
        return self._compose_blocks_message("当前仓位", blocks, prefix=prefix, suffix=suffix)

    def _render_records(self, arguments: list[str]) -> str:
        task_id = arguments[0] if arguments else None
        records = self.service.list_execution_records(
            task_id=task_id, limit=10)
        if not records:
            return self._compose_message("执行记录", "暂无执行记录。")
        blocks = [
            self._format_record_block(record, index)
            for index, record in enumerate(records, start=1)
        ]
        prefix = f"任务ID：{task_id}" if task_id else f"最近 {len(records)} 条记录"
        return self._compose_blocks_message("执行记录", blocks, prefix=prefix)

    async def _change_task_status(self, arguments: list[str], *, action: str) -> str:
        if not arguments:
            raise ValueError(f"用法：/{action} <task_id>")
        task_id = arguments[0]
        if action == "pause":
            task = self.service.pause_task(task_id)
        elif action == "resume":
            task = self.service.resume_task(task_id)
        elif action == "delete":
            task = self.service.delete_task(task_id)
        else:  # pragma: no cover
            raise ValueError(f"不支持的任务操作：{action}")
        if self.refresh_runtime is not None:
            await self.refresh_runtime()
        return self._compose_message(
            "任务状态已更新",
            "\n".join(
                [
                    f"名称：{task.title or task.token_id}",
                    f"任务ID：{task.task_id}",
                    f"当前状态：{self._format_task_status(task.status)}",
                ]
            ),
        )

    def _start_create_wizard(self, context: TelegramMessageContext) -> str:
        self._wizard_sessions[context.user_id] = TelegramWizardSession(
            mode="create",
            chat_id=context.chat_id,
            step="token_id",
        )
        return self._prompt_for_current_step(
            self._wizard_sessions[context.user_id],
            prefix="已进入创建任务向导。",
        )

    def _start_edit_wizard(self, context: TelegramMessageContext, arguments: list[str]) -> str:
        if not arguments:
            raise ValueError("用法：/edit <task_id>")
        task = self.service.get_task(arguments[0])
        if task.status is not TaskStatus.PAUSED:
            return "请先暂停任务，再通过 Telegram 进行编辑。"
        self._wizard_sessions[context.user_id] = TelegramWizardSession(
            mode="edit",
            chat_id=context.chat_id,
            step="dry_run",
            task_id=task.task_id,
            token_id=task.token_id,
            title=task.title,
            dry_run=task.dry_run,
            slippage_bps=task.slippage_bps,
            position_size=task.position_size,
            average_cost=task.average_cost,
            rules=list(task.rules),
        )
        return self._prompt_for_current_step(
            self._wizard_sessions[context.user_id],
            prefix=f"已进入编辑向导：{task.task_id}",
        )

    async def _handle_wizard_message(self, context: TelegramMessageContext) -> str:
        session = self._wizard_sessions[context.user_id]
        text = context.text.strip()
        if not text:
            return self._prompt_for_current_step(session, prefix="请输入本步骤所需的内容。")

        if session.step == "token_id":
            session.token_id = text
            session.step = "dry_run"
            return self._prompt_for_current_step(session)

        if session.step == "dry_run":
            if not (session.mode == "edit" and self._is_keep(text)):
                session.dry_run = self._parse_yes_no(text)
            session.step = "slippage_bps"
            return self._prompt_for_current_step(session)

        if session.step == "slippage_bps":
            if not (session.mode == "edit" and self._is_keep(text)):
                session.slippage_bps = self._parse_decimal(
                    text, field_name="滑点 bps", allow_zero=False)
            session.step = "position_size"
            return self._prompt_for_current_step(session)

        if session.step == "position_size":
            if session.mode == "edit" and self._is_keep(text):
                pass
            elif self._is_skip(text) or (session.mode == "edit" and self._is_clear(text)):
                session.position_size = None
            else:
                session.position_size = self._parse_decimal(
                    text, field_name="仓位数量", allow_zero=False)
            session.step = "average_cost"
            return self._prompt_for_current_step(session)

        if session.step == "average_cost":
            if session.mode == "edit" and self._is_keep(text):
                pass
            elif self._is_skip(text) or (session.mode == "edit" and self._is_clear(text)):
                session.average_cost = None
            else:
                session.average_cost = self._parse_decimal(
                    text, field_name="持仓均价", allow_zero=True)
            if session.mode == "edit":
                session.step = "rules_mode"
            else:
                session.rules.clear()
                session.step = "rule_kind"
            return self._prompt_for_current_step(session)

        if session.step == "rules_mode":
            if self._is_keep(text):
                session.step = "confirm"
                return self._prompt_for_current_step(session)
            if self._is_replace(text):
                session.rules.clear()
                self._reset_pending_rule(session)
                session.step = "rule_kind"
                return self._prompt_for_current_step(session)
            return self._prompt_for_current_step(session, prefix="请输入“保留”或“替换”。")

        if session.step == "rule_kind":
            normalized = text.lower()
            if self._is_done(text):
                if not session.rules:
                    return self._prompt_for_current_step(session, prefix="至少需要配置一条规则。")
                session.step = "confirm"
                return self._prompt_for_current_step(session)
            rule_kind = RULE_KIND_ALIASES.get(normalized)
            if rule_kind is None:
                return self._prompt_for_current_step(session, prefix="未识别的规则类型。")
            session.pending_rule_kind = rule_kind
            session.step = "rule_sell_size"
            return self._prompt_for_current_step(session)

        if session.step == "rule_sell_size":
            session.pending_rule_sell_size = self._parse_decimal(
                text, field_name="卖出数量", allow_zero=False)
            assert session.pending_rule_kind is not None
            if session.pending_rule_kind in {RuleKind.PRICE_STOP, RuleKind.TAKE_PROFIT}:
                session.step = "rule_trigger_price"
            elif session.pending_rule_kind is RuleKind.TRAILING_TAKE_PROFIT:
                session.step = "rule_drawdown_ratio"
            else:
                session.step = "rule_label"
            return self._prompt_for_current_step(session)

        if session.step == "rule_trigger_price":
            session.pending_rule_trigger_price = self._parse_decimal(
                text, field_name="触发价格", allow_zero=True)
            session.step = "rule_label"
            return self._prompt_for_current_step(session)

        if session.step == "rule_drawdown_ratio":
            drawdown_ratio = self._parse_decimal(
                text, field_name="回撤比例", allow_zero=False)
            if drawdown_ratio >= Decimal("1"):
                return self._prompt_for_current_step(session, prefix="回撤比例必须在 0 到 1 之间。")
            session.pending_rule_drawdown_ratio = drawdown_ratio
            session.step = "rule_activation_price"
            return self._prompt_for_current_step(session)

        if session.step == "rule_activation_price":
            if self._is_skip(text):
                session.pending_rule_trigger_price = None
            else:
                session.pending_rule_trigger_price = self._parse_decimal(
                    text, field_name="激活价格", allow_zero=True)
            session.step = "rule_label"
            return self._prompt_for_current_step(session)

        if session.step == "rule_label":
            session.rules.append(
                ExitRule(
                    kind=self._require_pending_rule_value(
                        session.pending_rule_kind),
                    sell_size=self._require_pending_rule_value(
                        session.pending_rule_sell_size),
                    trigger_price=session.pending_rule_trigger_price,
                    drawdown_ratio=session.pending_rule_drawdown_ratio,
                    label=None if self._is_skip(text) else text,
                )
            )
            added_rule = session.rules[-1].name
            self._reset_pending_rule(session)
            session.step = "rule_kind"
            return self._prompt_for_current_step(session, prefix=f"已添加规则：{added_rule}")

        if session.step == "confirm":
            if not self._is_confirm(text):
                return self._prompt_for_current_step(session, prefix="如需保存，请回复“确认”；如需退出，请发送 /cancel。")
            task = await self._finalize_wizard(session)
            self._wizard_sessions.pop(context.user_id, None)
            title = "任务已创建" if session.mode == "create" else "任务已更新"
            return self._compose_message(
                title,
                "\n".join(
                    [
                        f"名称：{task.title or task.token_id}",
                        f"任务ID：{task.task_id}",
                        f"当前状态：{self._format_task_status(task.status)}",
                    ]
                ),
            )

        raise ValueError(f"不支持的向导步骤：{session.step}")

    async def _finalize_wizard(self, session: TelegramWizardSession):
        if not session.rules:
            raise ValueError("至少需要一条规则")
        if session.mode == "create":
            if session.token_id is None or session.dry_run is None or session.slippage_bps is None:
                raise ValueError("创建向导尚未填写完整")
            task = self.service.create_task(
                token_id=session.token_id,
                rules=tuple(session.rules),
                dry_run=session.dry_run,
                slippage_bps=session.slippage_bps,
                position_size=session.position_size,
                average_cost=session.average_cost,
            )
        else:
            if session.task_id is None or session.dry_run is None or session.slippage_bps is None:
                raise ValueError("编辑向导尚未填写完整")
            task = self.service.update_task(
                session.task_id,
                rules=tuple(session.rules),
                dry_run=session.dry_run,
                slippage_bps=session.slippage_bps,
                position_size=session.position_size,
                average_cost=session.average_cost,
            )
        if self.refresh_runtime is not None:
            await self.refresh_runtime()
        return task

    def _cancel_wizard(self, user_id: int) -> str:
        if user_id not in self._wizard_sessions:
            return "当前没有进行中的向导。"
        self._wizard_sessions.pop(user_id, None)
        return "已取消当前向导。"

    def _prompt_for_current_step(
        self,
        session: TelegramWizardSession,
        *,
        prefix: str | None = None,
    ) -> str:
        lines: list[str] = []
        if prefix:
            lines.append(prefix)
            lines.append("")
        if session.step == "token_id":
            lines.append("请输入要监控的 Polymarket token_id。")
        elif session.step == "dry_run":
            suffix = " 编辑模式下可回复“保留”沿用当前值。" if session.mode == "edit" else ""
            current = "" if session.dry_run is None else f" 当前值：{'是' if session.dry_run else '否'}。"
            lines.append(f"是否使用 dry-run？回复 是/否。{current}{suffix}")
        elif session.step == "slippage_bps":
            suffix = " 编辑模式下可回复“保留”沿用当前值。" if session.mode == "edit" else ""
            current = "" if session.slippage_bps is None else f" 当前值：{session.slippage_bps}。"
            lines.append(f"请输入滑点 bps，必须大于 0。{current}{suffix}")
        elif session.step == "position_size":
            if session.mode == "edit":
                current = "自动" if session.position_size is None else str(
                    session.position_size)
                lines.append(
                    f"请输入仓位覆盖值；可回复正数、保留、清空。当前值：{current}。"
                )
            else:
                lines.append("请输入仓位覆盖值；如需走自动持仓，请回复“跳过”。")
        elif session.step == "average_cost":
            if session.mode == "edit":
                current = "自动" if session.average_cost is None else str(
                    session.average_cost)
                lines.append(
                    f"请输入均价覆盖值；可回复非负数、保留、清空。当前值：{current}。"
                )
            else:
                lines.append("请输入均价覆盖值；如需走自动均价，请回复“跳过”。")
        elif session.step == "rules_mode":
            lines.append("规则处理方式：回复“保留”沿用现有规则，或回复“替换”重新配置。")
        elif session.step == "rule_kind":
            lines.append(
                "请输入规则类型：保本止损 / 价格止损 / 止盈 / 追踪止盈。也支持英文别名。全部配置完成后回复“完成”。"
            )
        elif session.step == "rule_sell_size":
            lines.append("请输入本条规则的卖出数量，必须大于 0。")
        elif session.step == "rule_trigger_price":
            lines.append("请输入触发价格，必须大于等于 0。")
        elif session.step == "rule_drawdown_ratio":
            lines.append("请输入回撤比例，必须大于 0 且小于 1，例如 0.1。")
        elif session.step == "rule_activation_price":
            lines.append("请输入激活价格；如果不需要，可回复“跳过”。")
        elif session.step == "rule_label":
            lines.append("可选规则标签：输入文本即可；如不需要，可回复“跳过”。")
        elif session.step == "confirm":
            lines.append(self._render_wizard_summary(session))
            lines.append("")
            lines.append("确认无误后回复“确认”保存；如需退出，请发送 /cancel。")
        else:
            raise ValueError(f"不支持的向导步骤：{session.step}")
        return "\n".join(lines)

    def _render_wizard_summary(self, session: TelegramWizardSession) -> str:
        lines = [
            "任务摘要：",
            f"模式：{'创建' if session.mode == 'create' else '编辑'}",
        ]
        if session.task_id:
            lines.append(f"任务ID：{session.task_id}")
        if session.token_id:
            lines.append(f"Token：{session.token_id}")
        if session.dry_run is not None:
            lines.append(f"模拟执行：{self._format_bool_label(session.dry_run)}")
        if session.slippage_bps is not None:
            lines.append(f"滑点：{session.slippage_bps} bps")
        lines.append(
            f"仓位覆盖：{'自动' if session.position_size is None else session.position_size}")
        lines.append(
            f"均价覆盖：{'自动' if session.average_cost is None else session.average_cost}")
        lines.append("规则：")
        for index, rule in enumerate(session.rules, start=1):
            line = f"{index}. {rule.name} | {self._format_rule_kind_label(rule.kind)} | 卖出={rule.sell_size}"
            if rule.trigger_price is not None:
                line += f" | 触发={rule.trigger_price}"
            if rule.drawdown_ratio is not None:
                line += f" | 回撤={rule.drawdown_ratio}"
            lines.append(line)
        return "\n".join(lines)

    def _reset_pending_rule(self, session: TelegramWizardSession) -> None:
        session.pending_rule_kind = None
        session.pending_rule_sell_size = None
        session.pending_rule_trigger_price = None
        session.pending_rule_drawdown_ratio = None

    def _parse_yes_no(self, raw: str) -> bool:
        normalized = raw.strip().lower()
        if normalized in {"yes", "y", "true", "1", "on", "是", "好", "确认"}:
            return True
        if normalized in {"no", "n", "false", "0", "off", "否", "不要"}:
            return False
        raise ValueError("请输入“是”或“否”")

    def _parse_decimal(self, raw: str, *, field_name: str, allow_zero: bool) -> Decimal:
        try:
            value = Decimal(raw.strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field_name}必须是数字") from exc
        if allow_zero:
            if value < Decimal("0"):
                raise ValueError(f"{field_name}不能为负数")
        else:
            if value <= Decimal("0"):
                raise ValueError(f"{field_name}必须大于 0")
        return value

    def _is_skip(self, raw: str) -> bool:
        return raw.strip().lower() in {"skip", "auto", "跳过", "自动"}

    def _is_keep(self, raw: str) -> bool:
        return raw.strip().lower() in {"keep", "保留"}

    def _is_clear(self, raw: str) -> bool:
        return raw.strip().lower() in {"clear", "清空"}

    def _is_replace(self, raw: str) -> bool:
        return raw.strip().lower() in {"replace", "替换"}

    def _is_done(self, raw: str) -> bool:
        return raw.strip().lower() in {"done", "完成"}

    def _is_confirm(self, raw: str) -> bool:
        return raw.strip().lower() in {"confirm", "确认"}

    def _require_pending_rule_value(self, value):
        if value is None:
            raise ValueError("规则向导尚未填写完整")
        return value

    def _extract_message_context(self, update: dict[str, Any]) -> TelegramMessageContext | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        sender = message.get("from")
        chat = message.get("chat")
        text = message.get("text")
        update_id = update.get("update_id")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return None
        if not isinstance(text, str) or not isinstance(update_id, int):
            return None
        user_id = sender.get("id")
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        if not isinstance(user_id, int) or not isinstance(chat_id, int) or not isinstance(chat_type, str):
            return None
        return TelegramMessageContext(
            update_id=update_id,
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            text=text.strip(),
        )

    def _parse_command(self, text: str) -> tuple[str, list[str]]:
        if not text:
            return "", []
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        return command, parts[1:]

    def _format_outbox_message(self, entry: NotificationOutboxEntry) -> str:
        payload: dict[str, Any] = {}
        if entry.payload_json:
            try:
                payload = json.loads(entry.payload_json)
            except json.JSONDecodeError:
                payload = {}
        title = self._localize_notification_title(entry, payload)
        body_lines = self._localize_notification_body_lines(entry)
        return self._compose_message(title, "\n".join(body_lines))

    def _compose_message(self, title: str, body: str, *, prefix: str | None = None) -> str:
        parts = [f"【{title}】"]
        if prefix:
            parts.extend([prefix, ""])
        if body:
            parts.append(body.strip())
        return "\n".join(parts).strip()

    def _compose_blocks_message(
        self,
        title: str,
        blocks: list[str],
        *,
        prefix: str | None = None,
        suffix: str | None = None,
    ) -> str:
        visible_blocks = [block.strip() for block in blocks if block.strip()]
        if suffix:
            visible_blocks.append(suffix)
        return self._compose_message(title, "\n\n".join(visible_blocks), prefix=prefix)

    def _format_task_summary_block(self, task, index: int) -> str:
        return "\n".join(
            [
                f"{index}. {task.title or task.token_id}",
                f"任务ID：{task.task_id}",
                f"Token：{task.token_id}",
                f"状态：{self._format_task_status(task.status)}",
                f"模式：{'模拟执行' if task.dry_run else '实盘执行'}",
            ]
        )

    def _format_rule_block(self, rule: ExitRule, state: Any, index: int) -> str:
        lines = [
            f"规则 {index}：{rule.name}",
            f"类型：{self._format_rule_kind_label(rule.kind)}",
            f"卖出数量：{rule.sell_size}",
        ]
        if rule.trigger_price is not None:
            lines.append(f"触发价格：{rule.trigger_price}")
        if rule.drawdown_ratio is not None:
            lines.append(f"回撤比例：{rule.drawdown_ratio}")
        if state is not None:
            lines.append(f"已卖出：{state.sold_size}")
            if state.locked_size is not None:
                lines.append(f"已锁定：{state.locked_size}")
            if state.trigger_bid is not None:
                lines.append(f"触发买一价：{state.trigger_bid}")
            if state.peak_bid is not None:
                lines.append(f"峰值买一价：{state.peak_bid}")
        return "\n".join(lines)

    def _format_record_block(self, record, index: int) -> str:
        lines = [
            f"{index}. {record.created_at.isoformat()}",
            f"事件类型：{EVENT_TYPE_LABELS.get(record.event_type, record.event_type)}",
            f"规则：{record.rule_name}",
            f"状态：{self._format_generic_status(record.status)}",
        ]
        if record.message:
            lines.append(f"说明：{record.message}")
        return "\n".join(lines)

    def _format_position_block(self, position: PositionRecord, index: int) -> str:
        title = position.title or position.market or position.token_id
        lines = [
            f"{index}. {title}",
            f"结果方向：{position.outcome or '-'}",
            f"仓位：{position.size}",
            f"均价：{position.average_cost}",
            f"现价：{position.current_price}",
            f"市值：{position.current_value}",
            f"浮盈亏：{position.cash_pnl}",
            f"收益率：{self._format_percent(position.percent_pnl)}",
            f"Token：{position.token_id}",
        ]
        return "\n".join(lines)

    def _position_matches_keyword(self, position: PositionRecord, keyword: str) -> bool:
        haystacks = (
            position.token_id,
            position.title or "",
            position.market or "",
            position.outcome or "",
        )
        return any(keyword in haystack.lower() for haystack in haystacks)

    def _get_position_reader(self) -> PositionReader:
        if self.position_reader is not None:
            return self.position_reader
        if self._default_position_reader is None:
            self._default_position_reader = PolymarketGateway(
                PolymarketCredentials.from_env())
        return self._default_position_reader

    def _parse_task_status(self, raw: str) -> TaskStatus:
        normalized = raw.strip().lower()
        aliases = {
            "active": TaskStatus.ACTIVE,
            "运行中": TaskStatus.ACTIVE,
            "paused": TaskStatus.PAUSED,
            "已暂停": TaskStatus.PAUSED,
            "completed": TaskStatus.COMPLETED,
            "已完成": TaskStatus.COMPLETED,
            "failed": TaskStatus.FAILED,
            "失败": TaskStatus.FAILED,
            "cancelled": TaskStatus.CANCELLED,
            "canceled": TaskStatus.CANCELLED,
            "已取消": TaskStatus.CANCELLED,
            "deleted": TaskStatus.DELETED,
            "已删除": TaskStatus.DELETED,
        }
        if normalized not in aliases:
            raise ValueError(
                "任务状态只支持 active/paused/completed/failed/cancelled/deleted，或对应中文")
        return aliases[normalized]

    def _format_task_status(self, status: TaskStatus) -> str:
        return TASK_STATUS_LABELS.get(status, status.value)

    def _format_rule_kind_label(self, kind: RuleKind) -> str:
        return RULE_KIND_LABELS.get(kind, kind.value)

    def _format_generic_status(self, value: str) -> str:
        normalized = value.strip().lower()
        return GENERIC_STATUS_LABELS.get(normalized, value)

    def _format_bool_label(self, value: bool) -> str:
        return "是" if value else "否"

    def _format_percent(self, value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"

    def _localize_notification_title(self, entry: NotificationOutboxEntry, payload: dict[str, Any]) -> str:
        if entry.category == "task-lifecycle":
            action = str(payload.get("action") or "")
            if action == "created":
                return "任务创建通知"
            if action == "updated":
                return "任务更新通知"
            if action.startswith("status:"):
                status_value = action.split(":", 1)[1]
                return f"任务状态通知 · {self._format_generic_status(status_value)}"
            return "任务通知"
        if entry.category.startswith("record:"):
            event_type = entry.category.split(":", 1)[1]
            return f"执行通知 · {EVENT_TYPE_LABELS.get(event_type, event_type)}"
        return entry.title or "通知"

    def _localize_notification_body_lines(self, entry: NotificationOutboxEntry) -> list[str]:
        if not entry.body:
            return []
        localized: list[str] = []
        for line in entry.body.splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            if ":" not in cleaned:
                localized.append(f"标的：{cleaned}")
                continue
            key, raw_value = cleaned.split(":", 1)
            label = NOTIFICATION_BODY_LABELS.get(key.strip(), key.strip())
            value = raw_value.strip()
            if key.strip() == "status":
                value = self._format_generic_status(value)
            elif key.strip() == "dry_run":
                value = self._format_bool_label(
                    value.lower() in {"true", "1", "yes", "on"})
            elif key.strip() == "event_type":
                value = EVENT_TYPE_LABELS.get(value, value)
            localized.append(f"{label}：{value}")
        return localized

    def _retry_delay_seconds(self, entry: NotificationOutboxEntry) -> int:
        return min(300, max(5, 2 ** max(entry.attempt_count, 1)))

    async def _sleep_or_stop(self, timeout_seconds: float) -> None:
        if self._stop_event is None:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return
