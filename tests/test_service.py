from decimal import Decimal

import pytest

from poly_shield.backend.models import ExecutionAttempt, ExecutionAttemptStatus, ExecutionRecord, NotificationChannel, NotificationDeliveryStatus, TaskStatus
from poly_shield.backend.service import RuntimeLeaseConflictError, TaskConflictError, TaskService
from poly_shield.rules import ExitRule, RuleKind


def test_task_service_restores_active_tasks_from_store(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    active = service.create_task(
        token_id="token-1",
        rules=(ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("50")),),
        dry_run=True,
        slippage_bps=Decimal("50"),
        status=TaskStatus.ACTIVE,
    )
    service.create_task(
        token_id="token-2",
        rules=(ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
            "25"), trigger_price=Decimal("0.7")),),
        dry_run=True,
        slippage_bps=Decimal("50"),
        status=TaskStatus.PAUSED,
    )

    restored = TaskService.from_db_path(tmp_path / "poly-shield.db")

    assert restored.restored_task_count == 1
    assert tuple(restored.active_tasks) == (active.task_id,)


def test_task_service_rejects_duplicate_active_token(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    service.create_task(
        token_id="token-1",
        rules=(ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("50")),),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )

    with pytest.raises(TaskConflictError, match="already has an active task"):
        service.create_task(
            token_id="token-1",
            rules=(ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.7")),),
            dry_run=True,
            slippage_bps=Decimal("50"),
        )


def test_task_service_pause_resume_and_record_append(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    service.register_telegram_recipient(telegram_user_id=101, chat_id=101)
    task = service.create_task(
        token_id="token-3",
        rules=(ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
            "25"), trigger_price=Decimal("0.7")),),
        dry_run=False,
        slippage_bps=Decimal("25"),
    )

    paused = service.pause_task(task.task_id)
    resumed = service.resume_task(task.task_id)
    service.append_execution_record(
        ExecutionRecord.create(
            task_id=task.task_id,
            token_id=task.token_id,
            rule_name="take-profit",
            status="matched",
            best_bid=Decimal("0.71"),
            best_ask=Decimal("0.72"),
            trigger_price=Decimal("0.70"),
            requested_size=Decimal("25"),
            filled_size=Decimal("25"),
            message="matched in full",
        )
    )

    records = service.list_execution_records(task_id=task.task_id)
    notifications = service.list_notification_outbox(
        status=NotificationDeliveryStatus.PENDING,
        channel=NotificationChannel.TELEGRAM,
        ready_only=True,
        limit=20,
    )

    assert paused.status is TaskStatus.PAUSED
    assert resumed.status is TaskStatus.ACTIVE
    assert len(records) == 1
    assert records[0].status == "matched"
    assert any(entry.category == "task-lifecycle" for entry in notifications)
    assert any(entry.category == "record:rule" for entry in notifications)


def test_task_service_runtime_changes_and_lease(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    service.register_telegram_recipient(telegram_user_id=202, chat_id=202)
    task = service.create_task(
        token_id="token-4",
        rules=(ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
            "25"), trigger_price=Decimal("0.7")),),
        dry_run=False,
        slippage_bps=Decimal("25"),
    )
    attempt = ExecutionAttempt.create_prepared(
        task_id=task.task_id,
        token_id=task.token_id,
        rule_name="take-profit",
        requested_size=Decimal("25"),
        trigger_price=Decimal("0.70"),
        best_bid=Decimal("0.71"),
        best_ask=Decimal("0.72"),
        market_id="0xmarket-1",
    )

    updated = service.persist_runtime_changes(
        task.task_id,
        records=(
            ExecutionRecord.create(
                task_id=task.task_id,
                token_id=task.token_id,
                rule_name="system",
                event_type="system",
                status="paused",
                best_bid=Decimal("0"),
                message="paused for test",
            ),
        ),
        attempts=(attempt.evolve(
            status=ExecutionAttemptStatus.NEEDS_REVIEW, message="needs review"),),
        task_status=TaskStatus.PAUSED,
    )
    lease = service.acquire_runtime_lease("backend-runtime", "owner-1", 15)
    notifications = service.list_notification_outbox(
        status=NotificationDeliveryStatus.PENDING,
        channel=NotificationChannel.TELEGRAM,
        ready_only=True,
        limit=20,
    )

    assert updated.status is TaskStatus.PAUSED
    assert service.get_latest_execution_attempt_by_order_id("missing") is None
    assert lease.owner_id == "owner-1"
    assert any(entry.category == "record:system" for entry in notifications)
    assert any("status: paused" in entry.body for entry in notifications)

    another_service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    with pytest.raises(RuntimeLeaseConflictError, match="existing Poly Shield runtime may still be running or the previous lease has not expired yet"):
        another_service.acquire_runtime_lease("backend-runtime", "owner-2", 15)


def test_task_service_create_and_update_publish_lifecycle_notifications(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    service.register_telegram_recipient(telegram_user_id=303, chat_id=303)

    created = service.create_task(
        token_id="token-5",
        rules=(ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("50")),),
        dry_run=True,
        slippage_bps=Decimal("50"),
        status=TaskStatus.PAUSED,
        title="Will it ship?",
    )
    updated = service.update_task(
        created.task_id,
        rules=(ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
            "25"), trigger_price=Decimal("0.80")),),
        dry_run=False,
        slippage_bps=Decimal("25"),
    )
    notifications = service.list_notification_outbox(
        status=NotificationDeliveryStatus.PENDING,
        channel=NotificationChannel.TELEGRAM,
        ready_only=True,
        limit=20,
    )

    assert updated.dry_run is False
    assert any(entry.title == "Task created" for entry in notifications)
    assert any(entry.title == "Task updated" for entry in notifications)
