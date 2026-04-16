from decimal import Decimal

import asyncio
import pytest

from poly_shield.backend.models import ExecutionAttempt, ExecutionAttemptStatus, TaskStatus
from poly_shield.backend.runtime import ManagedTaskRunner, ManagedTaskRuntime, build_default_task_runner
from poly_shield.backend.user_stream import UserStreamEvent
from poly_shield.backend.service import RuntimeLeaseConflictError, TaskService
from poly_shield.executor import ExecutionResult, ExitExecutor, SellExecutionRequest
from poly_shield.positions import GatewayPositionProvider, ManualPositionProvider
from poly_shield.quotes import OrderBookLevel, QuoteSnapshot
from poly_shield.rules import ExitRule, RuleKind


class FakeSellGateway:
    def __init__(self, *, fill_size: str = "25", tick_size: str = "0.01", order_id: str | None = "order-1", status: str = "matched") -> None:
        self.fill_size = Decimal(fill_size)
        self.tick_size = Decimal(tick_size)
        self.order_id = order_id
        self.status = status

    def get_tick_size(self, token_id: str) -> Decimal:
        return self.tick_size

    def submit_market_sell(self, request) -> ExecutionResult:
        return ExecutionResult(
            status=self.status,
            requested_size=request.size,
            filled_size=self.fill_size,
            price_floor=request.price_floor,
            order_id=self.order_id,
            details="matched in full",
        )


class FakeMarketStream:
    def __init__(self, token_ids: tuple[str, ...], quote: QuoteSnapshot) -> None:
        self.token_ids = token_ids
        self.quote = quote

    async def pump_quotes(self, *, stop_event, on_quote, on_heartbeat=None) -> None:
        await on_quote(self.token_ids[0], self.quote)
        await stop_event.wait()


class FakeUserStream:
    def __init__(self, market_ids: tuple[str, ...], event: UserStreamEvent) -> None:
        self.market_ids = market_ids
        self.event = event
        self.sent = False

    async def pump_events(self, *, stop_event, on_event) -> None:
        if not self.sent:
            self.sent = True
            await on_event(self.event)
        await stop_event.wait()


class CrashingUserStream:
    async def pump_events(self, *, stop_event, on_event) -> None:
        raise RuntimeError("user websocket disconnected")


class IdleMarketStream:
    async def pump_quotes(self, *, stop_event, on_quote, on_heartbeat=None) -> None:
        await stop_event.wait()


class IdleUserStream:
    async def pump_events(self, *, stop_event, on_event) -> None:
        await stop_event.wait()


def test_managed_task_runner_does_not_persist_waiting_records(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-waiting",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.90"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )
    runner = ManagedTaskRunner(
        service=service,
        task=task,
        position_provider=ManualPositionProvider(
            size=Decimal("100"), average_cost=Decimal("0.40")),
        executor=ExitExecutor(gateway=FakeSellGateway(),
                              slippage_bps=Decimal("50")),
    )

    events = runner.process_quote(
        QuoteSnapshot(
            best_bid=Decimal("0.71"),
            best_ask=Decimal("0.72"),
            top_bids=(OrderBookLevel(price=Decimal(
                "0.71"), size=Decimal("100")),),
            top_asks=(OrderBookLevel(
                price=Decimal("0.72"), size=Decimal("40")),),
        )
    )

    records = service.list_execution_records(task_id=task.task_id)
    states = service.load_rule_states(task.task_id)

    assert len(events) == 1
    assert events[0].status == "waiting"
    assert records == []
    assert states["take-profit"].is_triggered is False


def test_managed_task_runner_persists_only_first_dry_run_trigger_record(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-dry-run",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.70"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )
    runner = ManagedTaskRunner(
        service=service,
        task=task,
        position_provider=ManualPositionProvider(
            size=Decimal("100"), average_cost=Decimal("0.40")),
        executor=ExitExecutor(gateway=FakeSellGateway(),
                              slippage_bps=Decimal("50")),
    )
    quote = QuoteSnapshot(
        best_bid=Decimal("0.71"),
        best_ask=Decimal("0.72"),
        top_bids=(OrderBookLevel(price=Decimal("0.71"), size=Decimal("100")),),
        top_asks=(OrderBookLevel(price=Decimal("0.72"), size=Decimal("40")),),
    )

    first_events = runner.process_quote(quote)
    second_events = runner.process_quote(quote)

    records = service.list_execution_records(task_id=task.task_id)

    assert len(first_events) == 1
    assert len(second_events) == 1
    assert first_events[0].status == "dry-run"
    assert second_events[0].status == "dry-run"
    assert len(records) == 1
    assert records[0].status == "dry-run"


def test_managed_task_runner_persists_records_and_completion(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-1",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.70"),
            ),
        ),
        dry_run=False,
        slippage_bps=Decimal("50"),
    )
    runner = ManagedTaskRunner(
        service=service,
        task=task,
        position_provider=ManualPositionProvider(
            size=Decimal("100"), average_cost=Decimal("0.40")),
        executor=ExitExecutor(gateway=FakeSellGateway(),
                              slippage_bps=Decimal("50")),
    )

    events = runner.process_quote(
        QuoteSnapshot(
            best_bid=Decimal("0.71"),
            best_ask=Decimal("0.72"),
            top_bids=(OrderBookLevel(price=Decimal(
                "0.71"), size=Decimal("100")),),
            top_asks=(OrderBookLevel(
                price=Decimal("0.72"), size=Decimal("40")),),
        )
    )

    records = service.list_execution_records(task_id=task.task_id)
    states = service.load_rule_states(task.task_id)
    updated_task = service.get_task(task.task_id)

    assert len(events) == 1
    assert events[0].status == "matched"
    assert len(records) == 1
    assert records[0].status == "matched"
    assert states["take-profit"].is_complete is True
    assert updated_task.status is TaskStatus.COMPLETED
    assert task.task_id not in service.active_tasks


@pytest.mark.anyio
async def test_managed_task_runtime_dispatches_quotes_to_active_tasks(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-1",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.70"),
            ),
        ),
        dry_run=False,
        slippage_bps=Decimal("50"),
    )

    def build_runner(managed_task):
        return ManagedTaskRunner(
            service=service,
            task=managed_task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(gateway=FakeSellGateway(),
                                  slippage_bps=Decimal("50")),
        )

    runtime = ManagedTaskRuntime(
        service=service,
        stream_factory=lambda token_ids: FakeMarketStream(
            token_ids,
            QuoteSnapshot(
                best_bid=Decimal("0.71"),
                best_ask=Decimal("0.72"),
                top_bids=(OrderBookLevel(price=Decimal(
                    "0.71"), size=Decimal("100")),),
                top_asks=(OrderBookLevel(
                    price=Decimal("0.72"), size=Decimal("40")),),
            ),
        ),
        runner_factory=build_runner,
    )

    await runtime.start()
    await asyncio.sleep(0.05)
    await runtime.stop()

    updated_task = service.get_task(task.task_id)
    records = service.list_execution_records(task_id=task.task_id)

    assert updated_task.status is TaskStatus.COMPLETED
    assert len(records) == 1
    assert runtime.snapshot()["runner_count"] == 0


@pytest.mark.anyio
async def test_managed_task_runtime_tracks_user_trade_updates(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-1",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.70"),
            ),
        ),
        dry_run=False,
        slippage_bps=Decimal("50"),
    )

    def build_runner(managed_task):
        return ManagedTaskRunner(
            service=service,
            task=managed_task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(
                gateway=FakeSellGateway(order_id="order-1"),
                slippage_bps=Decimal("50"),
            ),
        )

    runtime = ManagedTaskRuntime(
        service=service,
        stream_factory=lambda token_ids: FakeMarketStream(
            token_ids,
            QuoteSnapshot(
                market_id="0xmarket-1",
                best_bid=Decimal("0.71"),
                best_ask=Decimal("0.72"),
                top_bids=(OrderBookLevel(price=Decimal(
                    "0.71"), size=Decimal("100")),),
                top_asks=(OrderBookLevel(
                    price=Decimal("0.72"), size=Decimal("40")),),
            ),
        ),
        runner_factory=build_runner,
        user_stream_factory=lambda market_ids: FakeUserStream(
            market_ids,
            UserStreamEvent(
                event_type="trade",
                status="confirmed",
                order_id="order-1",
                related_order_ids=("order-1",),
                token_id="token-1",
                market_id="0xmarket-1",
                requested_size=Decimal("25"),
                filled_size=Decimal("25"),
                event_price=Decimal("0.71"),
                message="trade confirmed",
            ),
        ),
    )

    await runtime.start()
    await asyncio.sleep(0.1)
    await runtime.stop()

    records = service.list_execution_records(task_id=task.task_id)

    assert [record.event_type for record in records] == ["trade", "rule"]
    assert records[0].status == "confirmed"
    assert records[0].order_id == "order-1"
    assert records[0].market_id == "0xmarket-1"
    assert records[0].event_price == Decimal("0.71")
    assert runtime.snapshot()["tracked_order_count"] == 0
    assert runtime.snapshot()["last_market_message_at"] is None
    assert runtime.snapshot()["last_user_message_at"] is None
    assert "market" in runtime.snapshot()["stale_seconds"]


@pytest.mark.anyio
async def test_managed_task_runtime_prefetches_quote_before_stream(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    service.create_task(
        token_id="token-1",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.90"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )
    sequence: list[str] = []

    class IdleMarketStream:
        async def pump_quotes(self, *, stop_event, on_quote, on_heartbeat=None) -> None:
            sequence.append("stream")
            await stop_event.wait()

    def build_runner(managed_task):
        return ManagedTaskRunner(
            service=service,
            task=managed_task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(gateway=FakeSellGateway(),
                                  slippage_bps=Decimal("50")),
        )

    def load_quote(token_id: str) -> QuoteSnapshot:
        sequence.append("prefetch")
        return QuoteSnapshot(
            market_id="0xmarket-prefetch",
            best_bid=Decimal("0.71"),
            best_ask=Decimal("0.72"),
            top_bids=(OrderBookLevel(price=Decimal(
                "0.71"), size=Decimal("100")),),
            top_asks=(OrderBookLevel(
                price=Decimal("0.72"), size=Decimal("40")),),
        )

    runtime = ManagedTaskRuntime(
        service=service,
        stream_factory=lambda token_ids: IdleMarketStream(),
        runner_factory=build_runner,
        quote_loader=load_quote,
    )

    await runtime.start()
    await asyncio.sleep(0.05)
    await runtime.stop()

    assert sequence[:2] == ["prefetch", "stream"]
    snapshot = runtime.snapshot()
    assert snapshot["last_market_message_at"] is not None
    assert snapshot["stale_seconds"]["market"] is not None


@pytest.mark.anyio
async def test_managed_task_runtime_reconciles_tracked_orders_after_user_disconnect(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-1",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.70"),
            ),
        ),
        dry_run=False,
        slippage_bps=Decimal("50"),
    )

    def build_runner(managed_task):
        return ManagedTaskRunner(
            service=service,
            task=managed_task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(
                gateway=FakeSellGateway(order_id="order-1"),
                slippage_bps=Decimal("50"),
            ),
        )

    def reconcile_order(order_id: str, tracked_order) -> list[UserStreamEvent]:
        return [
            UserStreamEvent(
                event_type="trade",
                status="confirmed",
                order_id=order_id,
                related_order_ids=(order_id,),
                token_id=tracked_order.token_id,
                market_id=tracked_order.market_id,
                requested_size=Decimal("25"),
                filled_size=Decimal("25"),
                event_price=Decimal("0.71"),
                message="rest reconciled trade confirmed",
            )
        ]

    runtime = ManagedTaskRuntime(
        service=service,
        stream_factory=lambda token_ids: FakeMarketStream(
            token_ids,
            QuoteSnapshot(
                market_id="0xmarket-1",
                best_bid=Decimal("0.71"),
                best_ask=Decimal("0.72"),
                top_bids=(OrderBookLevel(price=Decimal(
                    "0.71"), size=Decimal("100")),),
                top_asks=(OrderBookLevel(
                    price=Decimal("0.72"), size=Decimal("40")),),
            ),
        ),
        runner_factory=build_runner,
        user_stream_factory=lambda market_ids: CrashingUserStream(),
        order_reconciler=reconcile_order,
    )

    await runtime.start()
    await asyncio.sleep(0.2)
    await runtime.stop()

    records = service.list_execution_records(task_id=task.task_id)

    assert [record.event_type for record in records] == ["trade", "rule"]
    assert records[0].message == "rest reconciled trade confirmed"
    assert runtime.snapshot()["tracked_order_count"] == 0
    assert runtime.snapshot()["last_user_message_at"] is None


def test_build_default_task_runner_passes_manual_position_overrides(tmp_path, monkeypatch) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-override",
        rules=(
            ExitRule(
                kind=RuleKind.BREAKEVEN_STOP,
                sell_size=Decimal("44"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("25"),
        position_size=Decimal("88"),
        average_cost=Decimal("0.33"),
    )

    class FakeGateway:
        pass

    fake_gateway = FakeGateway()

    monkeypatch.setattr(
        "poly_shield.backend.runtime.PolymarketCredentials.from_env", lambda: object())
    monkeypatch.setattr(
        "poly_shield.backend.runtime.PolymarketGateway", lambda credentials: fake_gateway)

    runner = build_default_task_runner(service, task)

    assert isinstance(runner.position_provider, GatewayPositionProvider)
    assert runner.position_provider.gateway is fake_gateway
    assert runner.position_provider.size_override == Decimal("88")
    assert runner.position_provider.average_cost_override == Decimal("0.33")


@pytest.mark.anyio
async def test_runtime_enforces_single_instance_lease(tmp_path) -> None:
    first_service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    second_service = TaskService.from_db_path(tmp_path / "poly-shield.db")

    runtime_one = ManagedTaskRuntime(
        service=first_service,
        stream_factory=lambda token_ids: IdleMarketStream(),
        runner_factory=lambda task: ManagedTaskRunner(
            service=first_service,
            task=task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(gateway=FakeSellGateway(),
                                  slippage_bps=Decimal("50")),
        ),
    )
    runtime_two = ManagedTaskRuntime(
        service=second_service,
        stream_factory=lambda token_ids: IdleMarketStream(),
        runner_factory=lambda task: ManagedTaskRunner(
            service=second_service,
            task=task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(gateway=FakeSellGateway(),
                                  slippage_bps=Decimal("50")),
        ),
    )

    await runtime_one.start()
    with pytest.raises(RuntimeLeaseConflictError):
        await runtime_two.start()
    await runtime_one.stop()


@pytest.mark.anyio
async def test_runtime_pauses_task_when_market_data_stales(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-stale",
        rules=(
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.90")),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )

    runtime = ManagedTaskRuntime(
        service=service,
        stream_factory=lambda token_ids: IdleMarketStream(),
        runner_factory=lambda managed_task: ManagedTaskRunner(
            service=service,
            task=managed_task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(gateway=FakeSellGateway(),
                                  slippage_bps=Decimal("50")),
        ),
        market_stale_pause_seconds=0.05,
        maintenance_interval_seconds=0.02,
    )

    await runtime.start()
    await asyncio.sleep(0.15)
    await runtime.stop()

    updated_task = service.get_task(task.task_id)
    records = service.list_execution_records(task_id=task.task_id)

    assert updated_task.status is TaskStatus.PAUSED
    assert records[0].event_type == "system"
    assert "market data stale" in records[0].message


@pytest.mark.anyio
async def test_runtime_restores_prepared_attempts_as_needing_review(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-prepared",
        rules=(
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.70")),
        ),
        dry_run=False,
        slippage_bps=Decimal("50"),
    )
    prepared_attempt = ExecutionAttempt.create_prepared(
        task_id=task.task_id,
        token_id=task.token_id,
        rule_name="take-profit",
        requested_size=Decimal("25"),
        trigger_price=Decimal("0.70"),
        best_bid=Decimal("0.71"),
        best_ask=Decimal("0.72"),
        market_id="0xmarket-1",
    )
    service.upsert_execution_attempt(prepared_attempt)

    runtime = ManagedTaskRuntime(
        service=service,
        stream_factory=lambda token_ids: IdleMarketStream(),
        runner_factory=lambda managed_task: ManagedTaskRunner(
            service=service,
            task=managed_task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(gateway=FakeSellGateway(),
                                  slippage_bps=Decimal("50")),
        ),
    )

    await runtime.start()
    await asyncio.sleep(0.05)
    await runtime.stop()

    updated_task = service.get_task(task.task_id)
    attempts = service.list_execution_attempts(task_id=task.task_id)
    records = service.list_execution_records(task_id=task.task_id)

    assert updated_task.status is TaskStatus.PAUSED
    assert attempts[0].status is ExecutionAttemptStatus.NEEDS_REVIEW
    assert records[0].status == "needs-review"


@pytest.mark.anyio
async def test_runtime_pauses_task_when_user_updates_stale(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-user-stale",
        rules=(
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.70")),
        ),
        dry_run=False,
        slippage_bps=Decimal("50"),
    )

    def build_runner(managed_task):
        return ManagedTaskRunner(
            service=service,
            task=managed_task,
            position_provider=ManualPositionProvider(
                size=Decimal("100"), average_cost=Decimal("0.40")),
            executor=ExitExecutor(
                gateway=FakeSellGateway(
                    fill_size="0", order_id="order-1", status="live"),
                slippage_bps=Decimal("50"),
            ),
        )

    runtime = ManagedTaskRuntime(
        service=service,
        stream_factory=lambda token_ids: FakeMarketStream(
            token_ids,
            QuoteSnapshot(
                market_id="0xmarket-1",
                best_bid=Decimal("0.71"),
                best_ask=Decimal("0.72"),
                top_bids=(OrderBookLevel(price=Decimal(
                    "0.71"), size=Decimal("100")),),
                top_asks=(OrderBookLevel(
                    price=Decimal("0.72"), size=Decimal("40")),),
            ),
        ),
        runner_factory=build_runner,
        user_stream_factory=lambda market_ids: IdleUserStream(),
        user_stale_pause_seconds=0.05,
        maintenance_interval_seconds=0.02,
    )

    await runtime.start()
    await asyncio.sleep(0.15)
    await runtime.stop()

    updated_task = service.get_task(task.task_id)
    attempts = service.list_execution_attempts(task_id=task.task_id)
    records = service.list_execution_records(task_id=task.task_id)

    assert updated_task.status is TaskStatus.PAUSED
    assert attempts[0].status is ExecutionAttemptStatus.SUBMITTED
    assert records[0].event_type == "system"
    assert "user execution updates stale" in records[0].message
