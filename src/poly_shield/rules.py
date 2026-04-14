from __future__ import annotations

"""规则引擎：定义止盈止损规则、状态以及触发判定。"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


ZERO = Decimal("0")
ONE = Decimal("1")


def _as_decimal(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class RuleKind(StrEnum):
    """当前支持的规则类型。"""
    BREAKEVEN_STOP = "breakeven-stop"
    PRICE_STOP = "price-stop"
    TAKE_PROFIT = "take-profit"
    TRAILING_TAKE_PROFIT = "trailing-take-profit"


@dataclass(frozen=True)
class PositionSnapshot:
    """规则评估所需的持仓快照。"""
    token_id: str
    size: Decimal
    average_cost: Decimal
    best_bid: Decimal

    def __post_init__(self) -> None:
        if self.size <= ZERO:
            raise ValueError("position size must be greater than zero")
        if self.average_cost < ZERO:
            raise ValueError("average cost cannot be negative")
        if self.best_bid < ZERO:
            raise ValueError("best bid cannot be negative")


@dataclass(frozen=True)
class ExitRule:
    """单条退出规则定义。"""
    kind: RuleKind
    sell_size: Decimal
    trigger_price: Decimal | None = None
    drawdown_ratio: Decimal | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if self.sell_size <= ZERO:
            raise ValueError("sell_size must be greater than zero")
        expects_trigger = self.kind in {
            RuleKind.PRICE_STOP, RuleKind.TAKE_PROFIT}
        expects_drawdown = self.kind is RuleKind.TRAILING_TAKE_PROFIT
        if expects_trigger and self.trigger_price is None:
            raise ValueError(f"{self.kind} requires a trigger_price")
        if self.kind is RuleKind.BREAKEVEN_STOP and self.trigger_price is not None:
            raise ValueError(f"{self.kind} does not accept a trigger_price")
        if self.trigger_price is not None and self.trigger_price < ZERO:
            raise ValueError("trigger_price cannot be negative")
        if expects_drawdown and self.drawdown_ratio is None:
            raise ValueError(f"{self.kind} requires a drawdown_ratio")
        if not expects_drawdown and self.drawdown_ratio is not None:
            raise ValueError(f"{self.kind} does not accept a drawdown_ratio")
        if self.drawdown_ratio is not None and (self.drawdown_ratio <= ZERO or self.drawdown_ratio >= ONE):
            raise ValueError("drawdown_ratio must be in the range (0, 1)")

    @property
    def name(self) -> str:
        return self.label or self.kind.value

    @property
    def activation_price(self) -> Decimal | None:
        if self.kind is RuleKind.TRAILING_TAKE_PROFIT:
            return self.trigger_price
        return None


@dataclass
class RuleState:
    """规则运行态，用来记录锁定卖出量和 trailing 峰值。"""
    locked_size: Decimal | None = None
    sold_size: Decimal = ZERO
    trigger_bid: Decimal | None = None
    peak_bid: Decimal | None = None

    @property
    def is_triggered(self) -> bool:
        return self.locked_size is not None

    @property
    def remaining_size(self) -> Decimal:
        if self.locked_size is None:
            return ZERO
        remaining = self.locked_size - self.sold_size
        return remaining if remaining > ZERO else ZERO

    @property
    def is_complete(self) -> bool:
        return self.locked_size is not None and self.remaining_size == ZERO

    def register_fill(self, fill_size: Decimal | str | int | float) -> None:
        """登记一次实际成交，用于后续补卖剩余数量。"""
        fill = _as_decimal(fill_size)
        if fill <= ZERO:
            raise ValueError("fill_size must be greater than zero")
        if self.locked_size is None:
            raise ValueError(
                "cannot register fills before the rule is triggered")
        updated = self.sold_size + fill
        if updated > self.locked_size:
            raise ValueError("fill_size exceeds locked target size")
        self.sold_size = updated


@dataclass(frozen=True)
class TriggerDecision:
    """一次规则评估后的输出结果。"""
    triggered: bool
    target_size: Decimal
    remaining_size: Decimal
    trigger_price: Decimal
    reason: str


def trigger_threshold(rule: ExitRule, position: PositionSnapshot) -> Decimal:
    if rule.kind is RuleKind.BREAKEVEN_STOP:
        return position.average_cost
    if rule.kind is RuleKind.TRAILING_TAKE_PROFIT:
        raise ValueError(
            "trailing take-profit requires rule state to compute its dynamic threshold")
    assert rule.trigger_price is not None
    return rule.trigger_price


def trailing_threshold(rule: ExitRule, state: RuleState) -> Decimal:
    """根据峰值和回撤比例计算 trailing 止盈的动态阈值。"""
    if rule.kind is not RuleKind.TRAILING_TAKE_PROFIT:
        raise ValueError(
            "trailing_threshold is only valid for trailing take-profit rules")
    if state.peak_bid is None or rule.drawdown_ratio is None:
        return ZERO
    return state.peak_bid * (ONE - rule.drawdown_ratio)


def trigger_threshold(rule: ExitRule, position: PositionSnapshot, state: RuleState | None = None) -> Decimal:
    if rule.kind is RuleKind.TRAILING_TAKE_PROFIT:
        if state is None:
            raise ValueError(
                "trailing take-profit requires rule state to compute its dynamic threshold")
        return trailing_threshold(rule, state)
    if rule.kind is RuleKind.BREAKEVEN_STOP:
        return position.average_cost
    assert rule.trigger_price is not None
    return rule.trigger_price


def update_rule_state(rule: ExitRule, position: PositionSnapshot, state: RuleState) -> None:
    """只在 trailing 规则下更新峰值，其他规则保持无状态。"""
    if rule.kind is not RuleKind.TRAILING_TAKE_PROFIT:
        return
    activation_price = rule.activation_price
    if activation_price is not None and state.peak_bid is None and position.best_bid < activation_price:
        return
    if state.peak_bid is None or position.best_bid > state.peak_bid:
        state.peak_bid = position.best_bid


def is_rule_triggered(rule: ExitRule, position: PositionSnapshot, state: RuleState) -> bool:
    threshold = trigger_threshold(rule, position, state)
    if rule.kind is RuleKind.TRAILING_TAKE_PROFIT:
        return state.peak_bid is not None and position.best_bid <= threshold and position.best_bid < state.peak_bid
    if rule.kind is RuleKind.TAKE_PROFIT:
        return position.best_bid >= threshold
    return position.best_bid <= threshold


def locked_target_size(rule: ExitRule, available_size: Decimal | str | int | float) -> Decimal:
    """在首次触发时锁定目标卖出数量，避免后续重复按比例放大。"""
    size = _as_decimal(available_size)
    if size <= ZERO:
        raise ValueError("available_size must be greater than zero")
    return rule.sell_size if rule.sell_size <= size else size


def evaluate_rule(
    rule: ExitRule,
    position: PositionSnapshot,
    state: RuleState,
    available_size: Decimal | str | int | float | None = None,
) -> TriggerDecision:
    """评估单条规则是否触发，并返回本轮应卖出的剩余数量。"""
    update_rule_state(rule, position, state)
    threshold = trigger_threshold(rule, position, state)
    if state.is_complete:
        return TriggerDecision(
            triggered=False,
            target_size=state.locked_size or ZERO,
            remaining_size=ZERO,
            trigger_price=threshold,
            reason=f"{rule.name} already completed",
        )

    if not is_rule_triggered(rule, position, state):
        if rule.kind is RuleKind.TRAILING_TAKE_PROFIT:
            if state.peak_bid is None:
                if rule.activation_price is None:
                    reason = f"best bid {position.best_bid} is establishing the trailing peak"
                else:
                    reason = f"best bid {position.best_bid} has not armed trailing take-profit at {rule.activation_price}"
            else:
                reason = (
                    f"best bid {position.best_bid} has not drawn down to {threshold} from peak {state.peak_bid}"
                )
        else:
            reason = f"best bid {position.best_bid} has not reached {threshold}"
        return TriggerDecision(
            triggered=False,
            target_size=state.locked_size or ZERO,
            remaining_size=state.remaining_size,
            trigger_price=threshold,
            reason=reason,
        )

    if state.locked_size is None:
        base_size = position.size if available_size is None else _as_decimal(
            available_size)
        if base_size <= ZERO:
            return TriggerDecision(
                triggered=False,
                target_size=ZERO,
                remaining_size=ZERO,
                trigger_price=threshold,
                reason=f"no available size remains for {rule.name}",
            )
        state.locked_size = locked_target_size(rule, base_size)
        state.trigger_bid = position.best_bid

    return TriggerDecision(
        triggered=state.remaining_size > ZERO,
        target_size=state.locked_size or ZERO,
        remaining_size=state.remaining_size,
        trigger_price=threshold,
        reason=f"best bid {position.best_bid} crossed {threshold}",
    )
