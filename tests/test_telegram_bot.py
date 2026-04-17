from __future__ import annotations

from decimal import Decimal

import pytest

from poly_shield.backend.models import NotificationChannel, NotificationDeliveryStatus, TaskStatus
from poly_shield.backend.security import LocalAccessSecuritySettings
from poly_shield.backend.service import TaskService
from poly_shield.backend.telegram_bot import TelegramBotController
from poly_shield.positions import PositionRecord
from poly_shield.rules import ExitRule, RuleKind


class FakeTelegramTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def get_updates(self, *, offset: int | None, timeout_seconds: int):
        return []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class FakePositionReader:
    def __init__(self, positions: list[PositionRecord]) -> None:
        self.positions = positions

    def list_positions(self, *, size_threshold: Decimal = Decimal("0")) -> list[PositionRecord]:
        return [position for position in self.positions if position.size >= size_threshold]

    def get_position(self, token_id: str) -> PositionRecord:
        for position in self.positions:
            if position.token_id == token_id:
                return position
        raise KeyError(token_id)


def _message_update(*, update_id: int, user_id: int, chat_id: int, text: str, chat_type: str = "private") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": chat_id, "type": chat_type},
            "date": 0,
            "text": text,
        },
    }


async def _send_updates(
    controller: TelegramBotController,
    *,
    user_id: int,
    chat_id: int,
    texts: list[str],
    starting_update_id: int = 1,
) -> None:
    for offset, text in enumerate(texts):
        await controller.handle_update(
            _message_update(
                update_id=starting_update_id + offset,
                user_id=user_id,
                chat_id=chat_id,
                text=text,
            )
        )


@pytest.mark.anyio
async def test_telegram_bot_rejects_non_whitelisted_users(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
    )

    await controller.handle_update(
        _message_update(update_id=1, user_id=999, chat_id=999, text="/tasks")
    )

    assert transport.sent == [
        (999, "当前账号不在 Telegram 白名单中，无法操作机器人。")]
    assert service.list_telegram_recipients() == []


@pytest.mark.anyio
async def test_telegram_bot_registers_and_lists_tasks_for_whitelisted_user(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    service.create_task(
        token_id="token-1",
        rules=(ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("50")),),
        dry_run=True,
        slippage_bps=Decimal("50"),
        title="Will it happen?",
    )
    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
    )

    await controller.handle_update(
        _message_update(update_id=1, user_id=101, chat_id=101, text="/start")
    )
    await controller.handle_update(
        _message_update(update_id=2, user_id=101, chat_id=101, text="/tasks")
    )

    assert len(service.list_telegram_recipients()) == 1
    assert transport.sent[0][0] == 101
    assert "已连接 Poly Shield Telegram 控制台。" in transport.sent[0][1]
    assert "/positions [关键词] - 查询当前仓位" in transport.sent[0][1]
    assert "Will it happen?" in transport.sent[1][1]


@pytest.mark.anyio
async def test_telegram_bot_delivers_pending_outbox_notifications(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    service.register_telegram_recipient(telegram_user_id=101, chat_id=101)
    service.create_task(
        token_id="token-2",
        rules=(ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("50")),),
        dry_run=True,
        slippage_bps=Decimal("50"),
        title="Notification task",
        status=TaskStatus.PAUSED,
    )
    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
    )

    await controller.deliver_pending_notifications_once()

    pending = service.list_notification_outbox(
        status=NotificationDeliveryStatus.PENDING,
        channel=NotificationChannel.TELEGRAM,
        ready_only=True,
        limit=20,
    )
    delivered = service.list_notification_outbox(
        status=NotificationDeliveryStatus.DELIVERED,
        channel=NotificationChannel.TELEGRAM,
        ready_only=False,
        limit=20,
    )

    assert pending == []
    assert len(delivered) == 1
    assert delivered[0].attempt_count == 1
    assert transport.sent[0][0] == 101
    assert "任务创建通知" in transport.sent[0][1]
    assert "任务ID：" in transport.sent[0][1]


@pytest.mark.anyio
async def test_telegram_bot_pause_command_updates_task_and_refreshes_runtime(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-3",
        rules=(ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("50")),),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )
    refreshes = {"count": 0}

    async def fake_refresh_runtime() -> None:
        refreshes["count"] += 1

    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
        refresh_runtime=fake_refresh_runtime,
    )

    await controller.handle_update(
        _message_update(update_id=1, user_id=101, chat_id=101,
                        text=f"/pause {task.task_id}")
    )

    assert service.get_task(task.task_id).status is TaskStatus.PAUSED
    assert refreshes["count"] == 1
    assert transport.sent[-1][0] == 101
    assert "任务状态已更新" in transport.sent[-1][1]
    assert "当前状态：已暂停" in transport.sent[-1][1]


@pytest.mark.anyio
async def test_telegram_bot_create_wizard_creates_task_and_refreshes_runtime(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    refreshes = {"count": 0}

    async def fake_refresh_runtime() -> None:
        refreshes["count"] += 1

    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
        refresh_runtime=fake_refresh_runtime,
    )

    await _send_updates(
        controller,
        user_id=101,
        chat_id=101,
        texts=[
            "/create",
            "token-create-1",
            "yes",
            "50",
            "skip",
            "skip",
            "take-profit",
            "25",
            "0.62",
            "first tp",
            "breakeven-stop",
            "75",
            "skip",
            "done",
            "confirm",
        ],
    )

    tasks = service.list_tasks()

    assert len(tasks) == 1
    assert refreshes["count"] == 1
    assert "回复“确认”保存" in transport.sent[-2][1]
    assert "任务已创建" in transport.sent[-1][1]
    assert "当前状态：运行中" in transport.sent[-1][1]
    assert tasks[0].token_id == "token-create-1"
    assert tasks[0].dry_run is True
    assert tasks[0].slippage_bps == Decimal("50")
    assert tasks[0].position_size is None
    assert tasks[0].average_cost is None
    assert len(tasks[0].rules) == 2
    assert tasks[0].rules[0].kind is RuleKind.TAKE_PROFIT
    assert tasks[0].rules[0].sell_size == Decimal("25")
    assert tasks[0].rules[0].trigger_price == Decimal("0.62")
    assert tasks[0].rules[0].label == "first tp"
    assert tasks[0].rules[1].kind is RuleKind.BREAKEVEN_STOP
    assert tasks[0].rules[1].sell_size == Decimal("75")


@pytest.mark.anyio
async def test_telegram_bot_edit_wizard_updates_paused_task_and_refreshes_runtime(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-edit-1",
        rules=(ExitRule(kind=RuleKind.PRICE_STOP, sell_size=Decimal(
            "100"), trigger_price=Decimal("0.31")),),
        dry_run=True,
        slippage_bps=Decimal("35"),
        position_size=Decimal("250"),
        average_cost=Decimal("0.44"),
        status=TaskStatus.PAUSED,
    )
    refreshes = {"count": 0}

    async def fake_refresh_runtime() -> None:
        refreshes["count"] += 1

    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
        refresh_runtime=fake_refresh_runtime,
    )

    await _send_updates(
        controller,
        user_id=101,
        chat_id=101,
        texts=[
            f"/edit {task.task_id}",
            "no",
            "75",
            "keep",
            "clear",
            "replace",
            "trailing-take-profit",
            "60",
            "0.10",
            "0.55",
            "trailing one",
            "done",
            "confirm",
        ],
    )

    updated = service.get_task(task.task_id)

    assert refreshes["count"] == 1
    assert "回复“确认”保存" in transport.sent[-2][1]
    assert transport.sent[-1][0] == 101
    assert "任务已更新" in transport.sent[-1][1]
    assert f"任务ID：{task.task_id}" in transport.sent[-1][1]
    assert "当前状态：已暂停" in transport.sent[-1][1]
    assert updated.dry_run is False
    assert updated.slippage_bps == Decimal("75")
    assert updated.position_size == Decimal("250")
    assert updated.average_cost is None
    assert len(updated.rules) == 1
    assert updated.rules[0].kind is RuleKind.TRAILING_TAKE_PROFIT
    assert updated.rules[0].sell_size == Decimal("60")
    assert updated.rules[0].drawdown_ratio == Decimal("0.10")
    assert updated.rules[0].trigger_price == Decimal("0.55")
    assert updated.rules[0].label == "trailing one"


@pytest.mark.anyio
async def test_telegram_bot_cancel_command_aborts_active_wizard(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
    )

    await _send_updates(
        controller,
        user_id=101,
        chat_id=101,
        texts=["/create", "token-cancel-1", "/cancel"],
    )

    assert service.list_tasks() == []
    assert transport.sent[-1] == (101, "已取消当前向导。")
    assert controller.snapshot()["active_wizard_count"] == 0


@pytest.mark.anyio
async def test_telegram_bot_edit_requires_paused_task(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-active-1",
        rules=(ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("50")),),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )
    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
    )

    await controller.handle_update(
        _message_update(update_id=1, user_id=101, chat_id=101,
                        text=f"/edit {task.task_id}")
    )

    assert transport.sent[-1] == (101,
                                  "请先暂停任务，再通过 Telegram 进行编辑。")
    assert controller.snapshot()["active_wizard_count"] == 0


@pytest.mark.anyio
async def test_telegram_bot_positions_command_renders_current_positions(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    transport = FakeTelegramTransport()
    controller = TelegramBotController(
        service=service,
        settings=LocalAccessSecuritySettings(
            telegram_enabled=True,
            telegram_allowed_user_ids=frozenset({101}),
        ),
        transport=transport,
        position_reader=FakePositionReader(
            [
                PositionRecord(
                    token_id="token-positions-1",
                    size=Decimal("120"),
                    average_cost=Decimal("0.42"),
                    current_price=Decimal("0.58"),
                    current_value=Decimal("69.6"),
                    cash_pnl=Decimal("19.2"),
                    percent_pnl=Decimal("0.38095238"),
                    outcome="YES",
                    title="Will it pass?",
                    market="Will it pass?",
                )
            ]
        ),
    )

    await controller.handle_update(
        _message_update(update_id=1, user_id=101,
                        chat_id=101, text="/positions")
    )

    assert transport.sent[-1][0] == 101
    assert "当前仓位" in transport.sent[-1][1]
    assert "Will it pass?" in transport.sent[-1][1]
    assert "仓位：120" in transport.sent[-1][1]
    assert "收益率：38.10%" in transport.sent[-1][1]
