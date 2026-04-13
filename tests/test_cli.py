import json
from argparse import Namespace
from decimal import Decimal

from poly_shield.cli import _emit_watch_events, build_rules
from poly_shield.quotes import OrderBookLevel
from poly_shield.rules import RuleKind
from poly_shield.watcher import WatchEvent


def make_watch_args(**overrides) -> Namespace:
    payload = {
        "average_cost": None,
        "breakeven_stop_ratio": None,
        "price_stop": None,
        "price_stop_ratio": None,
        "take_profit": None,
        "take_profit_ratio": None,
        "trailing_drawdown": None,
        "trailing_drawdown_ratio": None,
        "trailing_activation_price": None,
    }
    payload.update(overrides)
    return Namespace(**payload)


def test_build_rules_allows_breakeven_stop_without_manual_average_cost() -> None:
    rules = build_rules(make_watch_args(breakeven_stop_ratio=Decimal("0.25")))

    assert len(rules) == 1
    assert rules[0].kind is RuleKind.BREAKEVEN_STOP
    assert rules[0].sell_ratio == Decimal("0.25")


def test_emit_watch_events_prints_top_of_book(capsys) -> None:
    _emit_watch_events(
        [
            WatchEvent(
                token_id="token-1",
                rule_name="price-stop",
                status="waiting",
                best_bid=Decimal("0.064"),
                best_ask=Decimal("0.066"),
                top_bids=(OrderBookLevel(price=Decimal("0.064"), size=Decimal("109.75")),),
                top_asks=(OrderBookLevel(price=Decimal("0.066"), size=Decimal("21.03")),),
                message="waiting for trigger",
                trigger_price=Decimal("0.07"),
            )
        ]
    )

    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["best_ask"] == "0.066"
    assert payload["top_bids"] == [{"price": "0.064", "size": "109.75"}]
    assert payload["top_asks"] == [{"price": "0.066", "size": "21.03"}]
