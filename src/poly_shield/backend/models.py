from __future__ import annotations

"""后端任务、状态和审计记录的数据模型。"""

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from poly_shield.rules import ExitRule, RuleState, ZERO


def utc_now() -> datetime:
    """统一生成带时区的 UTC 时间戳。"""
    return datetime.now(timezone.utc)


def new_identifier() -> str:
    """生成稳定可序列化的主键。"""
    return uuid4().hex


class TaskStatus(StrEnum):
    """任务生命周期状态。"""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETED = "deleted"


class ExecutionAttemptStatus(StrEnum):
    """执行意图的生命周期状态。"""

    PREPARED = "prepared"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs-review"


@dataclass(frozen=True)
class ManagedTask:
    """持久化后的任务定义。"""

    task_id: str
    token_id: str
    rules: tuple[ExitRule, ...]
    status: TaskStatus
    dry_run: bool
    slippage_bps: Decimal
    position_size: Decimal | None
    average_cost: Decimal | None
    created_at: datetime
    updated_at: datetime
    title: str | None = None


@dataclass(frozen=True)
class PersistedRuleState:
    """可落库的规则运行态。"""

    rule_name: str
    locked_size: Decimal | None = None
    sold_size: Decimal = ZERO
    trigger_bid: Decimal | None = None
    peak_bid: Decimal | None = None
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def from_rule_state(cls, rule_name: str, state: RuleState) -> "PersistedRuleState":
        """把内存态 RuleState 转换成可持久化结构。"""
        return cls(
            rule_name=rule_name,
            locked_size=state.locked_size,
            sold_size=state.sold_size,
            trigger_bid=state.trigger_bid,
            peak_bid=state.peak_bid,
        )

    def to_rule_state(self) -> RuleState:
        """从持久化结构恢复内存态 RuleState。"""
        return RuleState(
            locked_size=self.locked_size,
            sold_size=self.sold_size,
            trigger_bid=self.trigger_bid,
            peak_bid=self.peak_bid,
        )


@dataclass(frozen=True)
class ExecutionRecord:
    """单次触发/执行的基础审计记录。"""

    record_id: str
    task_id: str
    token_id: str
    rule_name: str
    status: str
    best_bid: Decimal
    event_type: str = "rule"
    order_id: str | None = None
    market_id: str | None = None
    event_price: Decimal = ZERO
    best_ask: Decimal = ZERO
    trigger_price: Decimal = ZERO
    requested_size: Decimal = ZERO
    filled_size: Decimal = ZERO
    message: str = ""
    created_at: datetime = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        token_id: str,
        rule_name: str,
        event_type: str = "rule",
        status: str,
        order_id: str | None = None,
        market_id: str | None = None,
        event_price: Decimal = ZERO,
        best_bid: Decimal,
        best_ask: Decimal = ZERO,
        trigger_price: Decimal = ZERO,
        requested_size: Decimal = ZERO,
        filled_size: Decimal = ZERO,
        message: str = "",
    ) -> "ExecutionRecord":
        """为一条新审计记录生成主键。"""
        return cls(
            record_id=new_identifier(),
            task_id=task_id,
            token_id=token_id,
            rule_name=rule_name,
            event_type=event_type,
            status=status,
            order_id=order_id,
            market_id=market_id,
            event_price=event_price,
            best_bid=best_bid,
            best_ask=best_ask,
            trigger_price=trigger_price,
            requested_size=requested_size,
            filled_size=filled_size,
            message=message,
        )


@dataclass(frozen=True)
class ExecutionAttempt:
    """真实下单前后的执行意图，防止进程中断后丢失上下文。"""

    attempt_id: str
    task_id: str
    token_id: str
    rule_name: str
    status: ExecutionAttemptStatus
    requested_size: Decimal
    trigger_price: Decimal = ZERO
    best_bid: Decimal = ZERO
    best_ask: Decimal = ZERO
    filled_size: Decimal = ZERO
    order_id: str | None = None
    market_id: str | None = None
    message: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def create_prepared(
        cls,
        *,
        task_id: str,
        token_id: str,
        rule_name: str,
        requested_size: Decimal,
        trigger_price: Decimal,
        best_bid: Decimal,
        best_ask: Decimal,
        market_id: str | None,
    ) -> "ExecutionAttempt":
        timestamp = utc_now()
        return cls(
            attempt_id=new_identifier(),
            task_id=task_id,
            token_id=token_id,
            rule_name=rule_name,
            status=ExecutionAttemptStatus.PREPARED,
            requested_size=requested_size,
            trigger_price=trigger_price,
            best_bid=best_bid,
            best_ask=best_ask,
            market_id=market_id,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def evolve(
        self,
        *,
        status: ExecutionAttemptStatus,
        filled_size: Decimal | None = None,
        order_id: str | None = None,
        market_id: str | None = None,
        message: str | None = None,
    ) -> "ExecutionAttempt":
        return replace(
            self,
            status=status,
            filled_size=self.filled_size if filled_size is None else filled_size,
            order_id=self.order_id if order_id is None else order_id,
            market_id=self.market_id if market_id is None else market_id,
            message=self.message if message is None else message,
            updated_at=utc_now(),
        )


@dataclass(frozen=True)
class RuntimeLease:
    """单实例运行时租约。"""

    lease_key: str
    owner_id: str
    expires_at: datetime
    updated_at: datetime


class NotificationChannel(StrEnum):
    """通知投递通道。"""

    TELEGRAM = "telegram"


class NotificationDeliveryStatus(StrEnum):
    """通知出站队列状态。"""

    PENDING = "pending"
    DELIVERED = "delivered"


@dataclass(frozen=True)
class TelegramRecipient:
    """允许接收 Telegram 通知的已注册用户。"""

    recipient_id: str
    telegram_user_id: int
    chat_id: int
    chat_type: str = "private"
    is_active: bool = True
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        telegram_user_id: int,
        chat_id: int,
        chat_type: str = "private",
        is_active: bool = True,
    ) -> "TelegramRecipient":
        timestamp = utc_now()
        return cls(
            recipient_id=new_identifier(),
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            is_active=is_active,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def refresh_registration(
        self,
        *,
        chat_id: int,
        chat_type: str = "private",
        is_active: bool = True,
    ) -> "TelegramRecipient":
        return replace(
            self,
            chat_id=chat_id,
            chat_type=chat_type,
            is_active=is_active,
            updated_at=utc_now(),
        )


@dataclass(frozen=True)
class NotificationOutboxEntry:
    """待投递的通知出站记录。"""

    notification_id: str
    channel: NotificationChannel
    recipient_id: str
    dedupe_key: str
    category: str
    title: str
    body: str
    task_id: str | None = None
    record_id: str | None = None
    payload_json: str = "{}"
    status: NotificationDeliveryStatus = NotificationDeliveryStatus.PENDING
    attempt_count: int = 0
    created_at: datetime = field(default_factory=utc_now)
    available_at: datetime = field(default_factory=utc_now)
    delivered_at: datetime | None = None
    last_error: str = ""

    @classmethod
    def create(
        cls,
        *,
        channel: NotificationChannel,
        recipient_id: str,
        dedupe_key: str,
        category: str,
        title: str,
        body: str,
        task_id: str | None = None,
        record_id: str | None = None,
        payload: dict[str, object] | None = None,
        available_at: datetime | None = None,
    ) -> "NotificationOutboxEntry":
        timestamp = utc_now()
        return cls(
            notification_id=new_identifier(),
            channel=channel,
            recipient_id=recipient_id,
            dedupe_key=dedupe_key,
            category=category,
            title=title,
            body=body,
            task_id=task_id,
            record_id=record_id,
            payload_json=json.dumps(payload or {}, sort_keys=True),
            created_at=timestamp,
            available_at=available_at or timestamp,
        )

    def mark_delivered(self) -> "NotificationOutboxEntry":
        return replace(
            self,
            status=NotificationDeliveryStatus.DELIVERED,
            attempt_count=self.attempt_count + 1,
            delivered_at=utc_now(),
            last_error="",
        )

    def mark_for_retry(
        self,
        *,
        last_error: str,
        available_at: datetime,
    ) -> "NotificationOutboxEntry":
        return replace(
            self,
            status=NotificationDeliveryStatus.PENDING,
            attempt_count=self.attempt_count + 1,
            available_at=available_at,
            delivered_at=None,
            last_error=last_error,
        )
