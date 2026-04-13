from __future__ import annotations

"""watch 调度层：拼接持仓、盘口、规则和执行器。"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from poly_shield.executor import ExecutionResult, ExitExecutor
from poly_shield.positions import PositionProvider
from poly_shield.quotes import OrderBookLevel, QuoteSnapshot
from poly_shield.rules import ExitRule, PositionSnapshot, RuleKind, RuleState, TriggerDecision, ZERO, evaluate_rule


class QuoteReader(Protocol):
    """watch 流程依赖的最小盘口读取接口。"""

    def get_quote_snapshot(self, token_id: str) -> QuoteSnapshot: ...


@dataclass(frozen=True)
class WatchTask:
    """一次 watch 任务的静态配置。"""
    token_id: str
    rules: tuple[ExitRule, ...]
    poll_interval_seconds: float = 5.0
    dry_run: bool = True

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError("at least one exit rule is required")
        protective_kinds = {RuleKind.BREAKEVEN_STOP, RuleKind.PRICE_STOP}
        protective_count = sum(
            1 for rule in self.rules if rule.kind in protective_kinds)
        if protective_count > 1:
            raise ValueError(
                "only one protective stop rule can be active per task")
        names = [rule.name for rule in self.rules]
        if len(names) != len(set(names)):
            raise ValueError("rule labels must be unique")


@dataclass(frozen=True)
class WatchEvent:
    """单轮规则评估后的输出事件。"""
    token_id: str
    rule_name: str
    status: str
    best_bid: Decimal
    market_id: str | None = None
    order_id: str | None = None
    best_ask: Decimal = ZERO
    top_bids: tuple[OrderBookLevel, ...] = field(default_factory=tuple)
    top_asks: tuple[OrderBookLevel, ...] = field(default_factory=tuple)
    requested_size: Decimal = ZERO
    filled_size: Decimal = ZERO
    message: str = ""
    trigger_price: Decimal = ZERO


@dataclass
class Watcher:
    """负责执行单轮监控：读盘口、评规则、生成事件。"""
    quote_reader: QuoteReader
    position_provider: PositionProvider
    executor: ExitExecutor
    rule_states: dict[str, RuleState] = field(default_factory=dict)

    def run_cycle(self, task: WatchTask) -> list[WatchEvent]:
        """执行一轮 watch，并为每条规则生成一个事件。"""
        position = self.position_provider.get_position(task.token_id)
        quote = self.quote_reader.get_quote_snapshot(task.token_id)
        snapshot = PositionSnapshot(
            token_id=task.token_id,
            size=position.size,
            average_cost=position.average_cost,
            best_bid=quote.best_bid,
        )
        events: list[WatchEvent] = []
        for rule in task.rules:
            state = self.rule_states.setdefault(rule.name, RuleState())
            decision = evaluate_rule(
                rule,
                snapshot,
                state,
                available_size=self._available_size_for_rule(
                    task, rule.name, position.size),
            )
            if not decision.triggered:
                events.append(self._non_trigger_event(
                    task, rule, decision, quote))
                continue
            request = self.executor.build_request(
                token_id=task.token_id,
                size=decision.remaining_size,
                best_bid=quote.best_bid,
                rule_name=rule.name,
                dry_run=task.dry_run,
            )
            result = self.executor.execute(request)
            if result.filled_size > ZERO:
                state.register_fill(result.filled_size)
            events.append(self._trigger_event(
                task, rule, decision, result, quote))
        return events

    def _available_size_for_rule(self, task: WatchTask, rule_name: str, position_size: Decimal) -> Decimal:
        """扣掉其它规则已锁定但未成交的数量，避免同轮超卖。"""
        reserved = ZERO
        for rule in task.rules:
            if rule.name == rule_name:
                continue
            state = self.rule_states.get(rule.name)
            if state is None:
                continue
            reserved += state.remaining_size
        available = position_size - reserved
        return available if available > ZERO else ZERO

    def _non_trigger_event(self, task: WatchTask, rule: ExitRule, decision: TriggerDecision, quote: QuoteSnapshot) -> WatchEvent:
        """为 waiting 或 completed 状态生成输出事件。"""
        status = "completed" if self.rule_states[rule.name].is_complete else "waiting"
        return WatchEvent(
            token_id=task.token_id,
            rule_name=rule.name,
            status=status,
            market_id=quote.market_id,
            best_bid=quote.best_bid,
            best_ask=quote.best_ask,
            top_bids=quote.top_bids,
            top_asks=quote.top_asks,
            message=decision.reason,
            trigger_price=decision.trigger_price,
        )

    def _trigger_event(
        self,
        task: WatchTask,
        rule: ExitRule,
        decision: TriggerDecision,
        result: ExecutionResult,
        quote: QuoteSnapshot,
    ) -> WatchEvent:
        """为已触发规则生成输出事件。"""
        return WatchEvent(
            token_id=task.token_id,
            rule_name=rule.name,
            status=result.status,
            market_id=quote.market_id,
            order_id=result.order_id,
            best_bid=quote.best_bid,
            best_ask=quote.best_ask,
            top_bids=quote.top_bids,
            top_asks=quote.top_asks,
            requested_size=result.requested_size,
            filled_size=result.filled_size,
            message=result.details or decision.reason,
            trigger_price=decision.trigger_price,
        )
