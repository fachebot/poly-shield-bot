import json
from argparse import Namespace
from decimal import Decimal

from poly_shield.cli import _emit_watch_events, build_rules, handle_tasks_add, handle_tasks_list
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


def test_handle_tasks_add_posts_serialized_rules(monkeypatch, capsys) -> None:
    captured = {}

    def fake_backend_request(*, api_url: str, method: str, path: str, payload=None):
        captured["api_url"] = api_url
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"task_id": "task-1", "status": "active"}

    monkeypatch.setattr("poly_shield.cli._backend_request", fake_backend_request)

    args = Namespace(
        api_url="http://127.0.0.1:8787",
        token_id="token-1",
        dry_run=True,
        slippage_bps=Decimal("50"),
        average_cost=Decimal("0.42"),
        position_size=Decimal("100"),
        breakeven_stop_ratio=Decimal("0.5"),
        price_stop=None,
        price_stop_ratio=None,
        take_profit=Decimal("0.7"),
        take_profit_ratio=Decimal("0.25"),
        trailing_drawdown=None,
        trailing_drawdown_ratio=None,
        trailing_activation_price=None,
    )

    handle_tasks_add(args)
    payload = json.loads(capsys.readouterr().out)

    assert payload == {"task_id": "task-1", "status": "active"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/tasks"
    assert captured["payload"]["token_id"] == "token-1"
    assert captured["payload"]["position_size"] == "100"
    assert captured["payload"]["average_cost"] == "0.42"
    assert [rule["kind"] for rule in captured["payload"]["rules"]] == [
        "breakeven-stop",
        "take-profit",
    ]


def test_handle_tasks_list_builds_query_string(monkeypatch, capsys) -> None:
    captured = {}

    def fake_backend_request(*, api_url: str, method: str, path: str, payload=None):
        captured["api_url"] = api_url
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return [{"task_id": "task-1", "status": "active"}]

    monkeypatch.setattr("poly_shield.cli._backend_request", fake_backend_request)

    handle_tasks_list(Namespace(api_url="http://127.0.0.1:8787", status="active", all=False))
    payload = json.loads(capsys.readouterr().out)

    assert payload == [{"task_id": "task-1", "status": "active"}]
    assert captured["method"] == "GET"
    assert captured["path"] == "/tasks?status=active"
