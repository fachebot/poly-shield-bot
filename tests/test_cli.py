import json
from argparse import Namespace
from decimal import Decimal

from poly_shield.cli import _emit_watch_events, build_rules, handle_secrets_clear_private_key, handle_secrets_inspect_private_key, handle_secrets_set_private_key, handle_secrets_status, handle_tasks_add, handle_tasks_list
from poly_shield.quotes import OrderBookLevel
from poly_shield.rules import RuleKind
from poly_shield.watcher import WatchEvent


def make_watch_args(**overrides) -> Namespace:
    payload = {
        "average_cost": None,
        "breakeven_stop_size": None,
        "price_stop": None,
        "price_stop_size": None,
        "take_profit": None,
        "take_profit_size": None,
        "trailing_drawdown": None,
        "trailing_sell_size": None,
        "trailing_activation_price": None,
    }
    payload.update(overrides)
    return Namespace(**payload)


def test_build_rules_allows_breakeven_stop_without_manual_average_cost() -> None:
    rules = build_rules(make_watch_args(breakeven_stop_size=Decimal("25")))

    assert len(rules) == 1
    assert rules[0].kind is RuleKind.BREAKEVEN_STOP
    assert rules[0].sell_size == Decimal("25")


def test_emit_watch_events_prints_top_of_book(capsys) -> None:
    _emit_watch_events(
        [
            WatchEvent(
                token_id="token-1",
                rule_name="price-stop",
                status="waiting",
                best_bid=Decimal("0.064"),
                best_ask=Decimal("0.066"),
                top_bids=(OrderBookLevel(price=Decimal(
                    "0.064"), size=Decimal("109.75")),),
                top_asks=(OrderBookLevel(price=Decimal(
                    "0.066"), size=Decimal("21.03")),),
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

    monkeypatch.setattr("poly_shield.cli._backend_request",
                        fake_backend_request)

    args = Namespace(
        api_url="http://127.0.0.1:8787",
        token_id="token-1",
        dry_run=True,
        slippage_bps=Decimal("50"),
        average_cost=Decimal("0.42"),
        position_size=Decimal("100"),
        breakeven_stop_size=Decimal("50"),
        price_stop=None,
        price_stop_size=None,
        take_profit=Decimal("0.7"),
        take_profit_size=Decimal("25"),
        trailing_drawdown=None,
        trailing_sell_size=None,
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

    monkeypatch.setattr("poly_shield.cli._backend_request",
                        fake_backend_request)

    handle_tasks_list(
        Namespace(api_url="http://127.0.0.1:8787", status="active", all=False))
    payload = json.loads(capsys.readouterr().out)

    assert payload == [{"task_id": "task-1", "status": "active"}]
    assert captured["method"] == "GET"
    assert captured["path"] == "/tasks?status=active"


def test_handle_secrets_set_private_key_saves_value(monkeypatch, capsys) -> None:
    captured = {}

    class FakeStore:
        path = "C:/fake/secrets.json"

        def save_private_key(self, value: str):
            captured["value"] = value
            return self.path

    monkeypatch.setattr(
        "poly_shield.cli.LocalSecretStore.default", lambda: FakeStore())

    handle_secrets_set_private_key(Namespace(value="0xabc123"))
    payload = json.loads(capsys.readouterr().out)

    assert captured["value"] == "0xabc123"
    assert payload == {"status": "saved", "path": "C:/fake/secrets.json"}


def test_handle_secrets_status_includes_backend(monkeypatch, capsys) -> None:
    class FakeStore:
        path = "C:/fake/secrets.json"
        backend = "keyring"

        def has_private_key(self) -> bool:
            return True

    monkeypatch.setattr(
        "poly_shield.cli.LocalSecretStore.default", lambda: FakeStore())

    handle_secrets_status(Namespace())
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "path": "C:/fake/secrets.json",
        "backend": "keyring",
        "has_private_key": True,
    }


def test_handle_secrets_clear_private_key_reports_status(monkeypatch, capsys) -> None:
    class FakeStore:
        path = "C:/fake/secrets.json"

        def clear_private_key(self) -> bool:
            return True

    monkeypatch.setattr(
        "poly_shield.cli.LocalSecretStore.default", lambda: FakeStore())

    handle_secrets_clear_private_key(Namespace())
    payload = json.loads(capsys.readouterr().out)

    assert payload == {"status": "cleared", "path": "C:/fake/secrets.json"}


def test_handle_secrets_inspect_private_key_reports_address(monkeypatch, capsys) -> None:
    class FakeStore:
        def load_private_key(self) -> str:
            return "0x" + "11" * 32

    monkeypatch.setattr(
        "poly_shield.wallet_identity.LocalSecretStore.default", lambda: FakeStore())
    monkeypatch.setenv(
        "POLY_FUNDER", "0x2222222222222222222222222222222222222222")
    monkeypatch.setenv("POLY_SIGNATURE_TYPE", "1")

    handle_secrets_inspect_private_key(Namespace())
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "valid"
    assert payload["source"] == "local-secret-store"
    assert payload["signer_address"] == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
    assert payload["signature_type"] == "1"
    assert payload["proxy_wallet_mode"] is True
    assert payload["signer_matches_funder"] is False
    assert payload["effective_user_address"] == "0x2222222222222222222222222222222222222222"
