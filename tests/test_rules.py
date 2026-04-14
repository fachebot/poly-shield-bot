from decimal import Decimal

import pytest

from poly_shield.rules import ExitRule, PositionSnapshot, RuleKind, RuleState, evaluate_rule


def make_position(*, size: str = "100", average_cost: str = "0.42", best_bid: str = "0.50") -> PositionSnapshot:
    return PositionSnapshot(
        token_id="token-1",
        size=Decimal(size),
        average_cost=Decimal(average_cost),
        best_bid=Decimal(best_bid),
    )


def test_breakeven_stop_triggers_at_average_cost() -> None:
    rule = ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("25"))
    state = RuleState()

    decision = evaluate_rule(rule, make_position(best_bid="0.42"), state)

    assert decision.triggered is True
    assert decision.target_size == Decimal("25.00")
    assert decision.remaining_size == Decimal("25.00")
    assert state.trigger_bid == Decimal("0.42")


def test_price_stop_waits_until_bid_crosses_down() -> None:
    rule = ExitRule(
        kind=RuleKind.PRICE_STOP,
        sell_size=Decimal("50"),
        trigger_price=Decimal("0.44"),
    )
    state = RuleState()

    decision = evaluate_rule(rule, make_position(best_bid="0.45"), state)

    assert decision.triggered is False
    assert decision.target_size == Decimal("0")
    assert state.locked_size is None


def test_take_profit_triggers_at_or_above_target() -> None:
    rule = ExitRule(
        kind=RuleKind.TAKE_PROFIT,
        sell_size=Decimal("60"),
        trigger_price=Decimal("0.65"),
    )
    state = RuleState()

    decision = evaluate_rule(rule, make_position(best_bid="0.66"), state)

    assert decision.triggered is True
    assert decision.target_size == Decimal("60.00")
    assert decision.remaining_size == Decimal("60.00")


def test_trailing_take_profit_triggers_after_peak_drawdown() -> None:
    rule = ExitRule(
        kind=RuleKind.TRAILING_TAKE_PROFIT,
        sell_size=Decimal("40"),
        drawdown_ratio=Decimal("0.10"),
    )
    state = RuleState()

    first = evaluate_rule(rule, make_position(best_bid="0.80"), state)
    second = evaluate_rule(rule, make_position(best_bid="0.72"), state)

    assert first.triggered is False
    assert state.peak_bid == Decimal("0.80")
    assert second.triggered is True
    assert second.trigger_price == Decimal("0.7200")
    assert second.target_size == Decimal("40.00")


def test_trailing_take_profit_waits_for_activation_price() -> None:
    rule = ExitRule(
        kind=RuleKind.TRAILING_TAKE_PROFIT,
        sell_size=Decimal("50"),
        trigger_price=Decimal("0.60"),
        drawdown_ratio=Decimal("0.10"),
    )
    state = RuleState()

    before_activation = evaluate_rule(
        rule, make_position(best_bid="0.55"), state)
    armed = evaluate_rule(rule, make_position(best_bid="0.70"), state)
    after_drawdown = evaluate_rule(rule, make_position(best_bid="0.62"), state)

    assert before_activation.triggered is False
    assert state.peak_bid == Decimal("0.70")
    assert armed.triggered is False
    assert after_drawdown.triggered is True


def test_exit_rule_validates_required_drawdown_ratio() -> None:
    with pytest.raises(ValueError, match="requires a drawdown_ratio"):
        ExitRule(kind=RuleKind.TRAILING_TAKE_PROFIT,
                 sell_size=Decimal("50"))


def test_rule_locks_target_on_first_trigger_and_keeps_it_after_partial_fill() -> None:
    rule = ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("25"))
    state = RuleState()
    position = make_position(best_bid="0.42")

    first = evaluate_rule(rule, position, state)
    state.register_fill(Decimal("10"))

    second = evaluate_rule(rule, make_position(
        size="80", best_bid="0.40"), state)

    assert first.target_size == Decimal("25.00")
    assert second.target_size == Decimal("25.00")
    assert second.remaining_size == Decimal("15.00")


def test_rule_marks_complete_when_target_is_fully_filled() -> None:
    rule = ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("10"))
    state = RuleState()
    evaluate_rule(rule, make_position(best_bid="0.42"), state)
    state.register_fill(Decimal("10"))

    decision = evaluate_rule(rule, make_position(best_bid="0.30"), state)

    assert decision.triggered is False
    assert decision.remaining_size == Decimal("0")
    assert state.is_complete is True


def test_exit_rule_validates_required_trigger_price() -> None:
    with pytest.raises(ValueError, match="requires a trigger_price"):
        ExitRule(kind=RuleKind.TAKE_PROFIT, sell_size=Decimal("50"))


def test_register_fill_rejects_overfills() -> None:
    rule = ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("10"))
    state = RuleState()
    evaluate_rule(rule, make_position(best_bid="0.42"), state)

    with pytest.raises(ValueError, match="exceeds locked target size"):
        state.register_fill(Decimal("10.01"))


def test_rule_clamps_sell_size_to_available_size_when_triggered() -> None:
    rule = ExitRule(kind=RuleKind.BREAKEVEN_STOP, sell_size=Decimal("120"))
    state = RuleState()

    decision = evaluate_rule(
        rule,
        make_position(size="100", best_bid="0.42"),
        state,
        available_size=Decimal("80"),
    )

    assert decision.triggered is True
    assert decision.target_size == Decimal("80")
    assert decision.remaining_size == Decimal("80")
