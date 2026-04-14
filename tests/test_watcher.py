from decimal import Decimal

import pytest

from poly_shield.executor import ExecutionResult, ExitExecutor
from poly_shield.positions import ManualPositionProvider
from poly_shield.quotes import OrderBookLevel, QuoteSnapshot
from poly_shield.rules import ExitRule, RuleKind
from poly_shield.watcher import WatchTask, Watcher


class FakeGateway:
    def __init__(self, *, best_bid: str, best_ask: str | None = None, fills: list[str] | None = None, tick_size: str = "0.01") -> None:
        self.best_bid = Decimal(best_bid)
        self.best_ask = Decimal(best_ask) if best_ask is not None else self.best_bid + Decimal("0.01")
        self.tick_size = Decimal(tick_size)
        self.fills = [Decimal(fill) for fill in fills or []]
        self.requests = []

    def get_quote_snapshot(self, token_id: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            top_bids=(OrderBookLevel(price=self.best_bid, size=Decimal("100")),),
            top_asks=(OrderBookLevel(price=self.best_ask, size=Decimal("120")),),
        )

    def get_tick_size(self, token_id: str) -> Decimal:
        return self.tick_size

    def submit_market_sell(self, request):
        self.requests.append(request)
        fill_size = self.fills.pop(0) if self.fills else Decimal("0")
        return ExecutionResult(
            status="partial" if fill_size and fill_size < request.size else "matched",
            requested_size=request.size,
            filled_size=fill_size,
            price_floor=request.price_floor,
        )


def test_watch_task_rejects_multiple_protective_stops() -> None:
    with pytest.raises(ValueError, match="only one protective stop"):
        WatchTask(
            token_id="token-1",
            rules=(
                ExitRule(kind=RuleKind.BREAKEVEN_STOP,
                         sell_size=Decimal("25")),
                ExitRule(kind=RuleKind.PRICE_STOP, sell_size=Decimal(
                    "25"), trigger_price=Decimal("0.40")),
            ),
        )


def test_watcher_retries_only_remaining_size_after_partial_fill() -> None:
    gateway = FakeGateway(best_bid="0.42", fills=["10", "15"])
    executor = ExitExecutor(gateway=gateway, slippage_bps=Decimal("50"))
    provider = ManualPositionProvider(
        size=Decimal("100"), average_cost=Decimal("0.42"))
    watcher = Watcher(quote_reader=gateway,
                      position_provider=provider, executor=executor)
    task = WatchTask(
        token_id="token-1",
        rules=(ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("25")),),
        dry_run=False,
    )

    first_events = watcher.run_cycle(task)
    second_events = watcher.run_cycle(task)

    assert first_events[0].requested_size == Decimal("25.00")
    assert first_events[0].filled_size == Decimal("10")
    assert second_events[0].requested_size == Decimal("15.00")
    assert second_events[0].filled_size == Decimal("15")
    assert len(gateway.requests) == 2


def test_dry_run_keeps_locked_target_without_registering_fill() -> None:
    gateway = FakeGateway(best_bid="0.66")
    executor = ExitExecutor(gateway=gateway, slippage_bps=Decimal("100"))
    provider = ManualPositionProvider(
        size=Decimal("50"), average_cost=Decimal("0.40"))
    watcher = Watcher(quote_reader=gateway,
                      position_provider=provider, executor=executor)
    task = WatchTask(
        token_id="token-2",
        rules=(
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.65")),
        ),
        dry_run=True,
    )

    first = watcher.run_cycle(task)
    second = watcher.run_cycle(task)

    assert first[0].status == "dry-run"
    assert first[0].requested_size == Decimal("25.00")
    assert second[0].requested_size == Decimal("25.00")
    assert gateway.requests == []


def test_watch_event_includes_best_ask_and_top_of_book() -> None:
    gateway = FakeGateway(best_bid="0.66", best_ask="0.67")
    executor = ExitExecutor(gateway=gateway, slippage_bps=Decimal("100"))
    provider = ManualPositionProvider(
        size=Decimal("50"), average_cost=Decimal("0.40"))
    watcher = Watcher(quote_reader=gateway,
                      position_provider=provider, executor=executor)
    task = WatchTask(
        token_id="token-top-book",
        rules=(
            ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal(
                "25"), trigger_price=Decimal("0.65")),
        ),
        dry_run=True,
    )

    event = watcher.run_cycle(task)[0]

    assert event.best_bid == Decimal("0.66")
    assert event.best_ask == Decimal("0.67")
    assert event.top_bids[0].price == Decimal("0.66")
    assert event.top_asks[0].size == Decimal("120")


def test_watcher_reserves_remaining_size_between_multiple_take_profit_rules() -> None:
    gateway = FakeGateway(best_bid="0.70")
    executor = ExitExecutor(gateway=gateway, slippage_bps=Decimal("100"))
    provider = ManualPositionProvider(
        size=Decimal("100"), average_cost=Decimal("0.40"))
    watcher = Watcher(quote_reader=gateway,
                      position_provider=provider, executor=executor)
    task = WatchTask(
        token_id="token-3",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("60"),
                trigger_price=Decimal("0.65"),
                label="tp-1",
            ),
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("50"),
                trigger_price=Decimal("0.60"),
                label="tp-2",
            ),
        ),
        dry_run=True,
    )

    events = watcher.run_cycle(task)

    assert events[0].requested_size == Decimal("60.00")
    assert events[1].requested_size == Decimal("40")


def test_trailing_take_profit_dry_run_triggers_after_peak_drawdown() -> None:
    gateway = FakeGateway(best_bid="0.80")
    executor = ExitExecutor(gateway=gateway, slippage_bps=Decimal("100"))
    provider = ManualPositionProvider(
        size=Decimal("30"), average_cost=Decimal("0.40"))
    watcher = Watcher(quote_reader=gateway,
                      position_provider=provider, executor=executor)
    task = WatchTask(
        token_id="token-4",
        rules=(
            ExitRule(
                kind=RuleKind.TRAILING_TAKE_PROFIT,
                sell_size=Decimal("15"),
                drawdown_ratio=Decimal("0.10"),
            ),
        ),
        dry_run=True,
    )

    first = watcher.run_cycle(task)
    gateway.best_bid = Decimal("0.72")
    second = watcher.run_cycle(task)

    assert first[0].status == "waiting"
    assert second[0].status == "dry-run"
    assert second[0].requested_size == Decimal("15.00")


def test_executor_aligns_price_floor_to_tick_size() -> None:
    gateway = FakeGateway(best_bid="0.421", tick_size="0.01")
    executor = ExitExecutor(gateway=gateway, slippage_bps=Decimal("50"))

    request = executor.build_request(
        token_id="token-3",
        size=Decimal("10"),
        best_bid=Decimal("0.421"),
        rule_name="tp",
        dry_run=False,
    )

    assert request.price_floor == Decimal("0.41")
