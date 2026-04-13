from __future__ import annotations

"""后端运行时：把事件流行情接到现有 Watcher，并持久化状态与记录。"""

import asyncio
from datetime import datetime
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from poly_shield.backend.models import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionRecord,
    ManagedTask,
    RuntimeLease,
    TaskStatus,
    new_identifier,
    utc_now,
)
from poly_shield.backend.user_stream import PolymarketUserStream, UserStreamAuth, UserStreamEvent
from poly_shield.backend.service import RuntimeLeaseConflictError, TaskService
from poly_shield.config import PolymarketCredentials
from poly_shield.executor import ExitExecutor
from poly_shield.polymarket import PolymarketGateway, _extract_field
from poly_shield.positions import GatewayPositionProvider, PositionProvider
from poly_shield.quotes import QuoteSnapshot
from poly_shield.rules import PositionSnapshot, RuleState, ZERO, evaluate_rule
from poly_shield.watcher import WatchEvent, WatchTask, Watcher

from poly_shield.backend.market_stream import PolymarketMarketStream


@dataclass
class QuoteSnapshotCache:
    """把最新盘口快照缓存成 Watcher 可读取的接口。"""

    snapshots: dict[str, QuoteSnapshot] = field(default_factory=dict)

    def update(self, token_id: str, quote: QuoteSnapshot) -> None:
        self.snapshots[token_id] = quote

    def get_quote_snapshot(self, token_id: str) -> QuoteSnapshot:
        try:
            return self.snapshots[token_id]
        except KeyError as exc:
            raise RuntimeError(
                f"missing quote snapshot for token {token_id}") from exc


@dataclass
class ManagedTaskRunner:
    """单任务执行器：接收行情事件，运行规则并持久化结果。"""

    service: TaskService
    task: ManagedTask
    position_provider: PositionProvider
    executor: ExitExecutor
    quote_cache: QuoteSnapshotCache = field(default_factory=QuoteSnapshotCache)

    def __post_init__(self) -> None:
        self.watcher = Watcher(
            quote_reader=self.quote_cache,
            position_provider=self.position_provider,
            executor=self.executor,
        )
        self.watcher.rule_states.update(
            self.service.load_rule_states(self.task.task_id))

    def process_quote(self, quote: QuoteSnapshot) -> list[WatchEvent]:
        """处理一条最新盘口快照。"""
        self.quote_cache.update(self.task.token_id, quote)
        position = self.position_provider.get_position(self.task.token_id)
        snapshot = PositionSnapshot(
            token_id=self.task.token_id,
            size=position.size,
            average_cost=position.average_cost,
            best_bid=quote.best_bid,
        )
        events: list[WatchEvent] = []
        attempts: list[ExecutionAttempt] = []
        task_status: TaskStatus | None = None

        for rule in self.task.rules:
            state = self.watcher.rule_states.setdefault(rule.name, RuleState())
            decision = evaluate_rule(
                rule,
                snapshot,
                state,
                available_size=self.watcher._available_size_for_rule(
                    self._watch_task(), rule.name, position.size
                ),
            )
            if not decision.triggered:
                events.append(self.watcher._non_trigger_event(self._watch_task(), rule, decision, quote))
                continue

            request = self.executor.build_request(
                token_id=self.task.token_id,
                size=decision.remaining_size,
                best_bid=quote.best_bid,
                rule_name=rule.name,
                dry_run=self.task.dry_run,
            )
            if self.task.dry_run:
                result = self.executor.execute(request)
                if result.filled_size > ZERO:
                    state.register_fill(result.filled_size)
                events.append(self.watcher._trigger_event(self._watch_task(), rule, decision, result, quote))
                continue

            prepared_attempt = ExecutionAttempt.create_prepared(
                task_id=self.task.task_id,
                token_id=self.task.token_id,
                rule_name=rule.name,
                requested_size=request.size,
                trigger_price=decision.trigger_price,
                best_bid=quote.best_bid,
                best_ask=quote.best_ask,
                market_id=quote.market_id,
            )
            self.task = self.service.persist_runtime_changes(
                self.task.task_id,
                attempts=(prepared_attempt,),
            )
            try:
                result = self.executor.execute(request)
            except Exception as exc:
                attempts.append(
                    prepared_attempt.evolve(
                        status=ExecutionAttemptStatus.NEEDS_REVIEW,
                        message=f"execution interrupted after prepared attempt: {exc}",
                    )
                )
                task_status = TaskStatus.PAUSED
                events.append(
                    self._review_required_event(
                        quote=quote,
                        rule_name=rule.name,
                        trigger_price=decision.trigger_price,
                        requested_size=request.size,
                        message=f"execution interrupted; task paused for review: {exc}",
                    )
                )
                break

            if result.order_id is None:
                attempts.append(
                    prepared_attempt.evolve(
                        status=ExecutionAttemptStatus.NEEDS_REVIEW,
                        filled_size=result.filled_size,
                        message="live execution response missing order_id; task paused for review",
                    )
                )
                task_status = TaskStatus.PAUSED
                events.append(
                    self._review_required_event(
                        quote=quote,
                        rule_name=rule.name,
                        trigger_price=decision.trigger_price,
                        requested_size=request.size,
                        message="live execution response missing order_id; task paused for review",
                    )
                )
                break

            if result.filled_size > ZERO:
                state.register_fill(result.filled_size)
            attempts.append(
                prepared_attempt.evolve(
                    status=ExecutionAttemptStatus.SUBMITTED,
                    filled_size=result.filled_size,
                    order_id=result.order_id,
                    market_id=quote.market_id,
                    message=result.details or result.status,
                )
            )
            events.append(self.watcher._trigger_event(self._watch_task(), rule, decision, result, quote))

        if task_status is None and self._all_rules_complete():
            task_status = TaskStatus.COMPLETED

        records = tuple(
            self._build_record(
                event,
                event_type="attempt" if event.status == "needs-review" else "rule",
            )
            for event in events
        )
        self.task = self.service.persist_runtime_changes(
            self.task.task_id,
            states=self.watcher.rule_states,
            records=records,
            attempts=tuple(attempts),
            task_status=task_status,
        )
        return events

    def _watch_task(self) -> WatchTask:
        return WatchTask(
            token_id=self.task.token_id,
            rules=self.task.rules,
            dry_run=self.task.dry_run,
        )

    def _build_record(self, event: WatchEvent, *, event_type: str = "rule") -> ExecutionRecord:
        return ExecutionRecord.create(
            task_id=self.task.task_id,
            token_id=event.token_id,
            rule_name=event.rule_name,
            event_type=event_type,
            status=event.status,
            order_id=event.order_id,
            market_id=event.market_id,
            best_bid=event.best_bid,
            best_ask=event.best_ask,
            trigger_price=event.trigger_price,
            requested_size=event.requested_size,
            filled_size=event.filled_size,
            message=event.message,
        )

    def _review_required_event(
        self,
        *,
        quote: QuoteSnapshot,
        rule_name: str,
        trigger_price,
        requested_size,
        message: str,
    ) -> WatchEvent:
        return WatchEvent(
            token_id=self.task.token_id,
            rule_name=rule_name,
            status="needs-review",
            best_bid=quote.best_bid,
            market_id=quote.market_id,
            best_ask=quote.best_ask,
            top_bids=quote.top_bids,
            top_asks=quote.top_asks,
            requested_size=requested_size,
            message=message,
            trigger_price=trigger_price,
        )

    def _all_rules_complete(self) -> bool:
        states = self.watcher.rule_states
        if not states:
            return False
        return all(state.is_complete for state in states.values())


class MarketQuoteStream(Protocol):
    """运行时依赖的最小行情流接口。"""

    async def pump_quotes(
        self,
        *,
        stop_event: asyncio.Event,
        on_quote: Callable[[str, QuoteSnapshot], Awaitable[None]],
    ) -> None: ...


class UserEventStream(Protocol):
    """运行时依赖的最小用户事件流接口。"""

    async def pump_events(
        self,
        *,
        stop_event: asyncio.Event,
        on_event: Callable[[UserStreamEvent], Awaitable[None]],
    ) -> None: ...


class QuoteLoader(Protocol):
    """运行时依赖的最小盘口快照加载接口。"""

    def __call__(self, token_id: str) -> QuoteSnapshot: ...


class OrderReconciler(Protocol):
    """运行时依赖的订单对账接口。"""

    def __call__(self, order_id: str, tracked_order: "TrackedOrder") -> list[UserStreamEvent]: ...


@dataclass(frozen=True)
class TrackedOrder:
    """运行时内存中的订单跟踪上下文。"""

    task_id: str
    token_id: str
    rule_name: str
    market_id: str


@dataclass
class ManagedTaskRuntime:
    """管理 active 任务的实时执行运行时。"""

    service: TaskService
    stream_factory: Callable[[tuple[str, ...]], MarketQuoteStream]
    runner_factory: Callable[[ManagedTask], ManagedTaskRunner]
    user_stream_factory: Callable[[
        tuple[str, ...]], UserEventStream] | None = None
    quote_loader: QuoteLoader | None = None
    order_reconciler: OrderReconciler | None = None
    reconnect_delay_seconds: float = 2.0
    lease_key: str = "backend-runtime"
    lease_owner_id: str = field(default_factory=new_identifier)
    lease_ttl_seconds: int = 15
    maintenance_interval_seconds: float = 1.0
    market_stale_pause_seconds: float | None = 15.0
    user_stale_pause_seconds: float | None = 30.0
    runners: dict[str, ManagedTaskRunner] = field(default_factory=dict)
    tracked_orders: dict[str, TrackedOrder] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._user_refresh_event = asyncio.Event()
        self._market_session_stop_event: asyncio.Event | None = None
        self._user_session_stop_event: asyncio.Event | None = None
        self._quote_task: asyncio.Task[None] | None = None
        self._user_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._subscribed_token_ids: tuple[str, ...] = ()
        self._subscribed_market_ids: tuple[str, ...] = ()
        self._last_market_message_at: datetime | None = None
        self._last_user_message_at: datetime | None = None
        self._lease: RuntimeLease | None = None

    async def start(self) -> None:
        """启动运行时主循环。"""
        if self._quote_task is not None:
            return
        self._lease = self.service.acquire_runtime_lease(
            self.lease_key,
            self.lease_owner_id,
            self.lease_ttl_seconds,
        )
        self._stop_event.clear()
        self._restore_pending_execution_attempts()
        self._restore_tracked_orders()
        self._user_refresh_event.set()
        self._quote_task = asyncio.create_task(self._run_quotes())
        if self.user_stream_factory is not None:
            self._user_task = asyncio.create_task(self._run_user_events())
        self._maintenance_task = asyncio.create_task(self._run_maintenance())

    async def stop(self) -> None:
        """停止运行时主循环。"""
        if self._quote_task is None and self._user_task is None and self._maintenance_task is None:
            return
        self._stop_event.set()
        self._stop_sessions()
        tasks = [task for task in (
            self._quote_task, self._user_task, self._maintenance_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks)
        self._quote_task = None
        self._user_task = None
        self._maintenance_task = None
        if self._lease is not None:
            self.service.release_runtime_lease(self.lease.lease_key, self.lease.owner_id)
            self._lease = None

    async def refresh_active_tasks(self) -> None:
        """同步 active 任务，并在需要时重建 websocket 订阅。"""
        self._sync_runners()
        self._restore_tracked_orders()
        self._user_refresh_event.set()
        if self._market_session_stop_event is not None:
            self._market_session_stop_event.set()
        if self._user_session_stop_event is not None:
            self._user_session_stop_event.set()

    def snapshot(self) -> dict[str, object]:
        """输出运行时快照，便于健康检查。"""
        market_stale_seconds = self._compute_stale_seconds(
            self._last_market_message_at,
            relevant=bool(self.runners) or bool(self._subscribed_token_ids),
        )
        user_stale_seconds = self._compute_stale_seconds(
            self._last_user_message_at,
            relevant=bool(self.tracked_orders) or bool(self._subscribed_market_ids),
        )
        stale_candidates = [
            value for value in (market_stale_seconds, user_stale_seconds) if value is not None
        ]
        return {
            "running": self._quote_task is not None,
            "runner_count": len(self.runners),
            "subscribed_token_ids": list(self._subscribed_token_ids),
            "tracked_order_count": len(self.tracked_orders),
            "subscribed_market_ids": list(self._subscribed_market_ids),
            "lease_owner_id": None if self.lease is None else self.lease.owner_id,
            "lease_expires_at": None if self.lease is None else self.lease.expires_at.isoformat(),
            "last_market_message_at": self._serialize_timestamp(self._last_market_message_at),
            "last_user_message_at": self._serialize_timestamp(self._last_user_message_at),
            "stale_seconds": {
                "market": market_stale_seconds,
                "user": user_stale_seconds,
                "max": max(stale_candidates) if stale_candidates else None,
            },
        }

    async def _run_quotes(self) -> None:
        while not self._stop_event.is_set():
            self._sync_runners()
            token_ids = tuple(
                sorted({runner.task.token_id for runner in self.runners.values()}))
            self._subscribed_token_ids = token_ids
            if not token_ids:
                self._last_market_message_at = None
                await asyncio.sleep(0.1)
                continue
            if self._last_market_message_at is None:
                self._last_market_message_at = utc_now()
            if self.quote_loader is not None:
                try:
                    await self._prefetch_quotes(token_ids)
                except Exception:
                    if self._stop_event.is_set():
                        break
                    await asyncio.sleep(self.reconnect_delay_seconds)
                    continue
            self._market_session_stop_event = asyncio.Event()
            stream = self.stream_factory(token_ids)
            try:
                await stream.pump_quotes(
                    stop_event=self._market_session_stop_event,
                    on_quote=self._dispatch_quote,
                )
            except Exception:
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(self.reconnect_delay_seconds)
            finally:
                self._market_session_stop_event = None

    async def _run_user_events(self) -> None:
        while not self._stop_event.is_set():
            if self.order_reconciler is not None and self.tracked_orders:
                try:
                    await self._reconcile_tracked_orders()
                except Exception:
                    if self._stop_event.is_set():
                        break
                    await asyncio.sleep(self.reconnect_delay_seconds)
                    continue
            market_ids = self._tracked_market_ids()
            self._subscribed_market_ids = market_ids
            if self.user_stream_factory is None or not market_ids:
                if not self.tracked_orders:
                    self._last_user_message_at = None
                self._user_refresh_event.clear()
                try:
                    await asyncio.wait_for(self._user_refresh_event.wait(), timeout=0.1)
                except TimeoutError:
                    pass
                continue
            if self._last_user_message_at is None:
                self._last_user_message_at = utc_now()
            self._user_session_stop_event = asyncio.Event()
            stream = self.user_stream_factory(market_ids)
            try:
                await stream.pump_events(
                    stop_event=self._user_session_stop_event,
                    on_event=self._dispatch_user_event,
                )
            except Exception:
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(self.reconnect_delay_seconds)
            finally:
                self._user_session_stop_event = None

    async def _dispatch_quote(self, token_id: str, quote: QuoteSnapshot) -> None:
        self._last_market_message_at = utc_now()
        runners = [runner for runner in self.runners.values(
        ) if runner.task.token_id == token_id]
        for runner in runners:
            events = await asyncio.to_thread(runner.process_quote, quote)
            self._register_tracked_orders(runner.task, events, quote.market_id)
        self._sync_runners()
        next_token_ids = tuple(
            sorted({runner.task.token_id for runner in self.runners.values()}))
        if next_token_ids != self._subscribed_token_ids and self._market_session_stop_event is not None:
            self._market_session_stop_event.set()

    async def _dispatch_user_event(self, event: UserStreamEvent) -> None:
        self._last_user_message_at = utc_now()
        match = self._match_tracked_order(event)
        if match is None:
            return
        matched_order_id, tracked_order = match
        attempt = self.service.get_latest_execution_attempt_by_order_id(matched_order_id)
        attempt_updates: tuple[ExecutionAttempt, ...] = ()
        task_status: TaskStatus | None = None
        if attempt is not None:
            next_attempt_status = ExecutionAttemptStatus.SUBMITTED
            if event.event_type == "trade" and event.status == "confirmed":
                next_attempt_status = ExecutionAttemptStatus.CONFIRMED
            elif event.event_type == "trade" and event.status == "failed":
                next_attempt_status = ExecutionAttemptStatus.FAILED
                task_status = TaskStatus.FAILED
            elif event.event_type == "order" and event.status == "cancellation":
                next_attempt_status = ExecutionAttemptStatus.FAILED
                task_status = TaskStatus.FAILED
            attempt_updates = (
                attempt.evolve(
                    status=next_attempt_status,
                    filled_size=event.filled_size,
                    order_id=matched_order_id,
                    market_id=event.market_id or tracked_order.market_id,
                    message=event.message,
                ),
            )
        self.service.persist_runtime_changes(
            tracked_order.task_id,
            records=(
                ExecutionRecord.create(
                    task_id=tracked_order.task_id,
                    token_id=event.token_id or tracked_order.token_id,
                    rule_name=tracked_order.rule_name,
                    event_type=event.event_type,
                    status=event.status,
                    order_id=matched_order_id,
                    market_id=event.market_id or tracked_order.market_id,
                    event_price=event.event_price,
                    best_bid=ZERO,
                    best_ask=ZERO,
                    requested_size=event.requested_size,
                    filled_size=event.filled_size,
                    message=event.message,
                ),
            ),
            attempts=attempt_updates,
            task_status=task_status,
        )
        if event.is_terminal:
            for order_id in event.related_order_ids:
                self.tracked_orders.pop(order_id, None)
            if not self.tracked_orders:
                self._last_user_message_at = None
            self._user_refresh_event.set()
            next_market_ids = self._tracked_market_ids()
            if next_market_ids != self._subscribed_market_ids and self._user_session_stop_event is not None:
                self._user_session_stop_event.set()

    async def _prefetch_quotes(self, token_ids: tuple[str, ...]) -> None:
        for token_id in token_ids:
            quote = await asyncio.to_thread(self.quote_loader, token_id)
            await self._dispatch_quote(token_id, quote)

    async def _reconcile_tracked_orders(self) -> None:
        for order_id, tracked_order in list(self.tracked_orders.items()):
            events = await asyncio.to_thread(
                self.order_reconciler,
                order_id,
                tracked_order,
            )
            for event in events:
                await self._dispatch_user_event(event)

    def _sync_runners(self) -> None:
        active_tasks = dict(self.service.active_tasks)
        stale_task_ids = set(self.runners) - set(active_tasks)
        for task_id in stale_task_ids:
            self.runners.pop(task_id, None)
        if not active_tasks:
            self._last_market_message_at = None
        for task_id, task in active_tasks.items():
            runner = self.runners.get(task_id)
            if runner is None:
                self.runners[task_id] = self.runner_factory(task)
            else:
                runner.task = task

    def _register_tracked_orders(
        self,
        task: ManagedTask,
        events: list[WatchEvent],
        market_id: str | None,
    ) -> None:
        tracked_market_ids_before = self._tracked_market_ids()
        for event in events:
            resolved_market_id = event.market_id or market_id
            if event.order_id is None or not resolved_market_id:
                continue
            self.tracked_orders[event.order_id] = TrackedOrder(
                task_id=task.task_id,
                token_id=event.token_id,
                rule_name=event.rule_name,
                market_id=resolved_market_id,
            )
        if self.tracked_orders and self._last_user_message_at is None:
            self._last_user_message_at = utc_now()
        self._user_refresh_event.set()
        if self._tracked_market_ids() != tracked_market_ids_before and self._user_session_stop_event is not None:
            self._user_session_stop_event.set()

    def _tracked_market_ids(self) -> tuple[str, ...]:
        return tuple(sorted({order.market_id for order in self.tracked_orders.values() if order.market_id}))

    def _match_tracked_order(self, event: UserStreamEvent) -> tuple[str, TrackedOrder] | None:
        for order_id in event.related_order_ids:
            tracked_order = self.tracked_orders.get(order_id)
            if tracked_order is not None:
                return order_id, tracked_order
        return None

    def _restore_tracked_orders(self) -> None:
        latest_records: dict[str, ExecutionRecord] = {}
        for record in self.service.list_execution_records(limit=1000):
            if record.order_id is None or record.market_id is None:
                continue
            if record.order_id in latest_records:
                continue
            latest_records[record.order_id] = record
        for order_id, record in latest_records.items():
            if self._is_terminal_record(record):
                self.tracked_orders.pop(order_id, None)
                continue
            self.tracked_orders.setdefault(
                order_id,
                TrackedOrder(
                    task_id=record.task_id,
                    token_id=record.token_id,
                    rule_name=record.rule_name,
                    market_id=record.market_id,
                ),
            )
        if self.tracked_orders and self._last_user_message_at is None:
            self._last_user_message_at = utc_now()

    def _is_terminal_record(self, record: ExecutionRecord) -> bool:
        if record.event_type == "trade":
            return record.status in {"confirmed", "failed"}
        if record.event_type == "order":
            return record.status == "cancellation"
        return False

    def _serialize_timestamp(self, timestamp: datetime | None) -> str | None:
        return timestamp.isoformat() if timestamp is not None else None

    def _compute_stale_seconds(self, timestamp: datetime | None, *, relevant: bool) -> float | None:
        if not relevant or timestamp is None:
            return None
        return (utc_now() - timestamp).total_seconds()

    @property
    def lease(self) -> RuntimeLease | None:
        return self._lease

    async def _run_maintenance(self) -> None:
        while not self._stop_event.is_set():
            lease = self.service.renew_runtime_lease(
                self.lease_key,
                self.lease_owner_id,
                self.lease_ttl_seconds,
            )
            if lease is None:
                self._stop_event.set()
                self._stop_sessions()
                break
            self._lease = lease
            await self._enforce_staleness_guards()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.maintenance_interval_seconds)
            except TimeoutError:
                pass

    async def _enforce_staleness_guards(self) -> None:
        market_stale_seconds = self._compute_stale_seconds(
            self._last_market_message_at,
            relevant=bool(self.runners) or bool(self._subscribed_token_ids),
        )
        if (
            self.market_stale_pause_seconds is not None
            and market_stale_seconds is not None
            and market_stale_seconds >= self.market_stale_pause_seconds
        ):
            await self._pause_tasks_for_reason(
                tuple(self.service.active_tasks),
                reason=f"market data stale for {market_stale_seconds:.1f}s; task paused automatically",
            )
        user_stale_seconds = self._compute_stale_seconds(
            self._last_user_message_at,
            relevant=bool(self.tracked_orders) or bool(self._subscribed_market_ids),
        )
        if (
            self.user_stale_pause_seconds is not None
            and user_stale_seconds is not None
            and user_stale_seconds >= self.user_stale_pause_seconds
        ):
            await self._pause_tasks_for_reason(
                tuple(sorted({order.task_id for order in self.tracked_orders.values()})),
                reason=f"user execution updates stale for {user_stale_seconds:.1f}s; task paused automatically",
            )

    async def _pause_tasks_for_reason(self, task_ids: tuple[str, ...], *, reason: str) -> None:
        paused_any = False
        for task_id in task_ids:
            task = self.service.active_tasks.get(task_id)
            if task is None:
                continue
            self.service.persist_runtime_changes(
                task.task_id,
                records=(
                    ExecutionRecord.create(
                        task_id=task.task_id,
                        token_id=task.token_id,
                        rule_name="system",
                        event_type="system",
                        status="paused",
                        best_bid=ZERO,
                        best_ask=ZERO,
                        message=reason,
                    ),
                ),
                task_status=TaskStatus.PAUSED,
            )
            paused_any = True
        if paused_any:
            self._stop_sessions()

    def _stop_sessions(self) -> None:
        if self._market_session_stop_event is not None:
            self._market_session_stop_event.set()
        if self._user_session_stop_event is not None:
            self._user_session_stop_event.set()

    def _restore_pending_execution_attempts(self) -> None:
        pending_attempts = reversed(
            self.service.list_execution_attempts(
                statuses=(
                    ExecutionAttemptStatus.PREPARED,
                    ExecutionAttemptStatus.SUBMITTED,
                    ExecutionAttemptStatus.NEEDS_REVIEW,
                ),
                limit=1000,
            )
        )
        for attempt in pending_attempts:
            if attempt.order_id and attempt.market_id and attempt.status is ExecutionAttemptStatus.SUBMITTED:
                self.tracked_orders.setdefault(
                    attempt.order_id,
                    TrackedOrder(
                        task_id=attempt.task_id,
                        token_id=attempt.token_id,
                        rule_name=attempt.rule_name,
                        market_id=attempt.market_id,
                    ),
                )
                continue
            updated_attempt = attempt
            records: tuple[ExecutionRecord, ...] = ()
            task_status: TaskStatus | None = None
            if attempt.status is not ExecutionAttemptStatus.NEEDS_REVIEW:
                updated_attempt = attempt.evolve(
                    status=ExecutionAttemptStatus.NEEDS_REVIEW,
                    message="runtime restarted before execution attempt was fully reconciled; task paused for review",
                )
                records = (
                    ExecutionRecord.create(
                        task_id=attempt.task_id,
                        token_id=attempt.token_id,
                        rule_name=attempt.rule_name,
                        event_type="attempt",
                        status="needs-review",
                        best_bid=attempt.best_bid,
                        best_ask=attempt.best_ask,
                        trigger_price=attempt.trigger_price,
                        requested_size=attempt.requested_size,
                        filled_size=attempt.filled_size,
                        order_id=attempt.order_id,
                        market_id=attempt.market_id,
                        message=updated_attempt.message,
                    ),
                )
            task = self.service.get_task(attempt.task_id)
            if task.status is TaskStatus.ACTIVE:
                task_status = TaskStatus.PAUSED
            self.service.persist_runtime_changes(
                attempt.task_id,
                records=records,
                attempts=(updated_attempt,),
                task_status=task_status,
            )


def build_default_task_runner(
    service: TaskService,
    task: ManagedTask,
    credentials: PolymarketCredentials | None = None,
) -> ManagedTaskRunner:
    """为单账号后端构造默认任务执行器。"""
    gateway = PolymarketGateway(
        credentials or PolymarketCredentials.from_env())
    return ManagedTaskRunner(
        service=service,
        task=task,
        position_provider=GatewayPositionProvider(
            gateway=gateway,
            average_cost_override=task.average_cost,
            size_override=task.position_size,
        ),
        executor=ExitExecutor(gateway=gateway, slippage_bps=task.slippage_bps),
    )


def build_default_runtime(service: TaskService) -> ManagedTaskRuntime:
    """构造默认后端运行时。"""
    credentials = PolymarketCredentials.from_env()
    user_auth: UserStreamAuth | None = None
    quote_gateway = PolymarketGateway(credentials)
    reconcile_gateway = PolymarketGateway(credentials)

    def build_user_stream(market_ids: tuple[str, ...]) -> UserEventStream:
        nonlocal user_auth
        if user_auth is None:
            auth = reconcile_gateway.get_user_channel_auth()
            user_auth = UserStreamAuth(
                api_key=auth["apiKey"],
                api_secret=auth["secret"],
                api_passphrase=auth["passphrase"],
            )
        return PolymarketUserStream(market_ids=market_ids, auth=user_auth)

    def reconcile_order(order_id: str, tracked_order: TrackedOrder) -> list[UserStreamEvent]:
        order = reconcile_gateway.get_order(order_id)
        associate_trades = _extract_field(order, "associate_trades", "associateTrades") or []
        if isinstance(associate_trades, str):
            associate_trades = [associate_trades]
        trade_statuses: list[str] = []
        latest_trade = None
        for trade_id in associate_trades:
            trade = reconcile_gateway.get_trade(str(trade_id))
            if trade is None:
                continue
            latest_trade = trade
            trade_status = str(_extract_field(trade, "status") or "").lower()
            if trade_status:
                trade_statuses.append(trade_status)
        if trade_statuses:
            if any(status not in {"confirmed", "failed"} for status in trade_statuses):
                return []
            terminal_status = "failed" if "failed" in trade_statuses else "confirmed"
            maker_orders = _extract_field(latest_trade, "maker_orders", "makerOrders") or []
            related_order_ids = tuple(
                order_id_value
                for order_id_value in [
                    str(_extract_field(latest_trade, "taker_order_id", "takerOrderId") or "") or None,
                    *[
                        str(_extract_field(maker_order, "order_id", "orderId") or "") or None
                        for maker_order in maker_orders
                    ],
                ]
                if order_id_value is not None
            )
            return [
                UserStreamEvent(
                    event_type="trade",
                    status=terminal_status,
                    order_id=order_id,
                    related_order_ids=related_order_ids or (order_id,),
                    token_id=str(_extract_field(latest_trade, "asset_id", "assetId") or tracked_order.token_id),
                    market_id=str(_extract_field(latest_trade, "market") or tracked_order.market_id) or None,
                    requested_size=_decimal_field(order, "original_size", "originalSize", default=ZERO),
                    filled_size=_decimal_field(order, "size_matched", "sizeMatched", default=ZERO),
                    event_price=_decimal_field(latest_trade, "price", default=ZERO),
                    message=f"rest reconciled trade {terminal_status}",
                )
            ]
        order_status = str(_extract_field(order, "status") or "").lower()
        if order_status in {"unmatched", "cancelled", "canceled", "rejected"}:
            return [
                UserStreamEvent(
                    event_type="trade",
                    status="failed",
                    order_id=order_id,
                    related_order_ids=(order_id,),
                    token_id=str(_extract_field(order, "asset_id", "assetId") or tracked_order.token_id),
                    market_id=str(_extract_field(order, "market") or tracked_order.market_id) or None,
                    requested_size=_decimal_field(order, "original_size", "originalSize", default=ZERO),
                    filled_size=_decimal_field(order, "size_matched", "sizeMatched", default=ZERO),
                    event_price=_decimal_field(order, "price", default=ZERO),
                    message=f"rest reconciled order status {order_status}",
                )
            ]
        return []

    return ManagedTaskRuntime(
        service=service,
        stream_factory=lambda token_ids: PolymarketMarketStream(
            token_ids=token_ids),
        runner_factory=lambda task: build_default_task_runner(
            service, task, credentials=credentials),
        user_stream_factory=build_user_stream,
        quote_loader=quote_gateway.get_quote_snapshot,
        order_reconciler=reconcile_order,
    )


def _decimal_field(payload: object, *names: str, default=ZERO):
    value = _extract_field(payload, *names)
    if value in {None, ""}:
        return default
    return type(default)(str(value))
