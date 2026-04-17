from decimal import Decimal

from poly_shield.backend.models import ExecutionAttempt, ExecutionAttemptStatus, ExecutionRecord, NotificationChannel, NotificationDeliveryStatus, NotificationOutboxEntry, TaskStatus
from poly_shield.backend.store import SQLiteTaskStore
from poly_shield.rules import ExitRule, RuleKind, RuleState


def test_sqlite_store_round_trips_task_definition(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "poly-shield.db")

    created = store.create_task(
        token_id="token-1",
        rules=(
            ExitRule(kind=RuleKind.PRICE_STOP, sell_size=Decimal(
                "50"), trigger_price=Decimal("0.40")),
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.65"), label="tp-1"),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
        position_size=Decimal("100"),
        average_cost=Decimal("0.42"),
    )

    loaded = store.get_task(created.task_id)

    assert loaded is not None
    assert loaded.task_id == created.task_id
    assert loaded.token_id == "token-1"
    assert loaded.status is TaskStatus.ACTIVE
    assert loaded.dry_run is True
    assert loaded.slippage_bps == Decimal("50")
    assert loaded.position_size == Decimal("100")
    assert loaded.average_cost == Decimal("0.42")
    assert [rule.name for rule in loaded.rules] == ["price-stop", "tp-1"]


def test_sqlite_store_round_trips_rule_states(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "poly-shield.db")
    task = store.create_task(
        token_id="token-2",
        rules=(
            ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("50")),
            ExitRule(kind=RuleKind.TRAILING_TAKE_PROFIT,
                     sell_size=Decimal("25"), drawdown_ratio=Decimal("0.1")),
        ),
        dry_run=False,
        slippage_bps=Decimal("75"),
    )

    store.replace_rule_states(
        task.task_id,
        {
            "breakeven-stop": RuleState(
                locked_size=Decimal("50"),
                sold_size=Decimal("10"),
                trigger_bid=Decimal("0.42"),
            ),
            "trailing-take-profit": RuleState(
                peak_bid=Decimal("0.80"),
            ),
        },
    )

    loaded = store.load_rule_states(task.task_id)

    assert loaded["breakeven-stop"].locked_size == Decimal("50")
    assert loaded["breakeven-stop"].sold_size == Decimal("10")
    assert loaded["breakeven-stop"].trigger_bid == Decimal("0.42")
    assert loaded["trailing-take-profit"].peak_bid == Decimal("0.80")


def test_sqlite_store_tracks_status_changes_and_execution_records(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "poly-shield.db")
    task = store.create_task(
        token_id="token-3",
        rules=(
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.70")),
        ),
        dry_run=False,
        slippage_bps=Decimal("25"),
    )

    updated = store.update_task_status(task.task_id, TaskStatus.PAUSED)
    record = store.append_execution_record(
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

    records = store.list_execution_records(task_id=task.task_id)

    assert updated.status is TaskStatus.PAUSED
    assert len(records) == 1
    assert records[0].record_id == record.record_id
    assert records[0].message == "matched in full"


def test_sqlite_store_round_trips_execution_attempts_and_runtime_lease(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "poly-shield.db")
    task = store.create_task(
        token_id="token-4",
        rules=(
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.70")),
        ),
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
    store.upsert_execution_attempt(
        attempt.evolve(
            status=ExecutionAttemptStatus.SUBMITTED,
            order_id="order-1",
            filled_size=Decimal("5"),
            message="submitted",
        )
    )
    lease = store.acquire_runtime_lease("backend-runtime", "owner-1", 15)
    duplicate = store.acquire_runtime_lease("backend-runtime", "owner-2", 15)
    attempts = store.list_execution_attempts(task_id=task.task_id)
    latest_attempt = store.get_latest_execution_attempt_by_order_id("order-1")

    assert attempts[0].status is ExecutionAttemptStatus.SUBMITTED
    assert attempts[0].order_id == "order-1"
    assert latest_attempt is not None
    assert latest_attempt.attempt_id == attempt.attempt_id
    assert lease is not None
    assert lease.owner_id == "owner-1"
    assert duplicate is None


def test_sqlite_store_round_trips_telegram_recipients_and_notification_outbox(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "poly-shield.db")

    created = store.upsert_telegram_recipient(
        telegram_user_id=101,
        chat_id=101,
        chat_type="private",
    )
    updated = store.upsert_telegram_recipient(
        telegram_user_id=101,
        chat_id=202,
        chat_type="private",
    )
    entry = NotificationOutboxEntry.create(
        channel=NotificationChannel.TELEGRAM,
        recipient_id=updated.recipient_id,
        dedupe_key="task:task-1:paused",
        category="task-status",
        title="Task paused",
        body="task task-1 paused automatically",
        payload={"task_id": "task-1", "status": "paused"},
    )

    store.enqueue_notification_outbox((entry, entry))
    recipients = store.list_telegram_recipients()
    pending = store.list_notification_outbox(
        status=NotificationDeliveryStatus.PENDING,
        channel=NotificationChannel.TELEGRAM,
        ready_only=True,
    )
    delivered = pending[0].mark_delivered()
    store.update_notification_outbox_entry(delivered)
    sent = store.list_notification_outbox(
        status=NotificationDeliveryStatus.DELIVERED,
        channel=NotificationChannel.TELEGRAM,
    )

    assert created.recipient_id == updated.recipient_id
    assert recipients[0].chat_id == 202
    assert len(pending) == 1
    assert sent[0].notification_id == entry.notification_id
    assert sent[0].attempt_count == 1


def test_sqlite_store_persists_notifications_with_runtime_changes(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "poly-shield.db")
    recipient = store.upsert_telegram_recipient(
        telegram_user_id=303,
        chat_id=303,
    )
    task = store.create_task(
        token_id="token-5",
        rules=(
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.70")),
        ),
        dry_run=False,
        slippage_bps=Decimal("25"),
    )
    notification = NotificationOutboxEntry.create(
        channel=NotificationChannel.TELEGRAM,
        recipient_id=recipient.recipient_id,
        dedupe_key=f"task:{task.task_id}:paused",
        category="task-status",
        title="Task paused",
        body="task paused for review",
        task_id=task.task_id,
    )

    updated = store.persist_task_runtime_changes(
        task.task_id,
        task_status=TaskStatus.PAUSED,
        notifications=(notification,),
    )
    pending = store.list_notification_outbox(
        status=NotificationDeliveryStatus.PENDING,
        channel=NotificationChannel.TELEGRAM,
        ready_only=True,
    )

    assert updated.status is TaskStatus.PAUSED
    assert pending[0].notification_id == notification.notification_id
    assert pending[0].task_id == task.task_id
