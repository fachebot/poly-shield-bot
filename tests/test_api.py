from decimal import Decimal

from contextlib import AbstractAsyncContextManager

from fastapi.testclient import TestClient

from poly_shield.backend.api import create_app
from poly_shield.backend.models import ExecutionRecord, TaskStatus
from poly_shield.backend.service import TaskService
from poly_shield.positions import PositionRecord
from poly_shield.rules import ExitRule, RuleKind, RuleState


def test_api_can_create_list_and_transition_tasks(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    client = TestClient(create_app(service))

    created = client.post(
        "/tasks",
        json={
            "token_id": "token-1",
            "dry_run": True,
            "slippage_bps": "50",
            "position_size": "100",
            "average_cost": "0.42",
            "rules": [
                {
                    "kind": "breakeven-stop",
                    "sell_size": "50",
                },
                {
                    "kind": "take-profit",
                    "sell_size": "25",
                    "trigger_price": "0.7",
                    "label": "tp-1",
                },
            ],
        },
    )

    assert created.status_code == 201
    assert created.json()["position_size"] == "100"
    assert created.json()["average_cost"] == "0.42"
    task_id = created.json()["task_id"]

    listed = client.get("/tasks")
    paused = client.post(f"/tasks/{task_id}/pause")
    resumed = client.post(f"/tasks/{task_id}/resume")
    deleted = client.delete(f"/tasks/{task_id}")

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert paused.json()["status"] == "paused"
    assert resumed.json()["status"] == "active"
    assert deleted.json()["status"] == "deleted"


def test_api_lists_execution_records(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-2",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.7"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )
    service.append_execution_record(
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

    client = TestClient(create_app(service))
    response = client.get(f"/records?task_id={task.task_id}")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["status"] == "matched"


class FakeRuntime:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.refreshed = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def refresh_active_tasks(self) -> None:
        self.refreshed += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "runner_count": 0,
            "subscribed_token_ids": [],
            "tracked_order_count": 0,
            "subscribed_market_ids": [],
            "running": True,
            "last_market_message_at": "2026-04-13T00:00:00+00:00",
            "last_user_message_at": None,
            "stale_seconds": {"market": 0.0, "user": None, "max": 0.0},
        }


def test_api_refreshes_runtime_and_reports_snapshot(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    runtime = FakeRuntime()

    with TestClient(create_app(service, runtime=runtime)) as client:
        health = client.get("/health")
        created = client.post(
            "/tasks",
            json={
                "token_id": "token-3",
                "dry_run": True,
                "slippage_bps": "50",
                "rules": [{"kind": "breakeven-stop", "sell_size": "50"}],
            },
        )
        task_id = created.json()["task_id"]
        client.post(f"/tasks/{task_id}/pause")
        client.post(f"/tasks/{task_id}/resume")
        client.delete(f"/tasks/{task_id}")

    assert health.status_code == 200
    assert "runtime" in health.json()
    assert "last_market_message_at" in health.json()["runtime"]
    assert "stale_seconds" in health.json()["runtime"]
    assert runtime.started == 1
    assert runtime.stopped == 1
    assert runtime.refreshed == 4


class FakePositionReader:
    def list_positions(self, *, size_threshold=Decimal("0")) -> list[PositionRecord]:
        return [
            PositionRecord(
                token_id="token-1",
                size=Decimal("100"),
                average_cost=Decimal("0.42"),
                current_price=Decimal("0.55"),
                current_value=Decimal("55"),
                cash_pnl=Decimal("13"),
                percent_pnl=Decimal("0.31"),
                outcome="YES",
                market="0xmarket-1",
                title="Will it happen?",
                event_slug="will-it-happen-event",
                slug="will-it-happen",
            )
        ]

    def get_position(self, token_id: str) -> PositionRecord:
        return self.list_positions()[0]


class QuoteAwarePositionReader(FakePositionReader):
    def get_best_bid(self, token_id: str) -> Decimal:
        assert token_id == "token-1"
        return Decimal("0.53")


def test_api_serves_positions_and_frontend_shell(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    client = TestClient(create_app(service, position_reader=FakePositionReader()))

    positions = client.get("/positions?token_id=token-1")
    index = client.get("/")

    assert positions.status_code == 200
    assert positions.json()[0]["title"] == "Will it happen?"
    assert positions.json()[0]["event_slug"] == "will-it-happen-event"
    assert positions.json()[0]["current_price"] == "0.55"
    assert index.status_code == 200
    assert "Poly Shield Control Room" in index.text


def test_api_prefers_best_bid_for_position_metrics(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    client = TestClient(
        create_app(service, position_reader=QuoteAwarePositionReader())
    )

    response = client.get("/positions?token_id=token-1")

    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["current_price"] == "0.53"
    assert Decimal(payload["current_value"]) == Decimal("53")
    assert Decimal(payload["cash_pnl"]) == Decimal("11")
    assert Decimal(payload["percent_pnl"]) == Decimal("0.26190476")


def test_api_filters_tasks_and_records_by_token(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task_one = service.create_task(
        token_id="token-1",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.7"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )
    task_two = service.create_task(
        token_id="token-2",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("50"),
                trigger_price=Decimal("0.8"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )
    service.append_execution_record(
        ExecutionRecord.create(
            task_id=task_one.task_id,
            token_id=task_one.token_id,
            rule_name="take-profit",
            status="dry-run",
            best_bid=Decimal("0.71"),
            message="token-1",
        )
    )
    service.append_execution_record(
        ExecutionRecord.create(
            task_id=task_two.task_id,
            token_id=task_two.token_id,
            rule_name="take-profit",
            status="dry-run",
            best_bid=Decimal("0.81"),
            message="token-2",
        )
    )

    client = TestClient(create_app(service, position_reader=FakePositionReader()))
    tasks = client.get("/tasks?include_deleted=true&token_id=token-1")
    records = client.get("/records?token_id=token-1")

    assert tasks.status_code == 200
    assert len(tasks.json()) == 1
    assert tasks.json()[0]["token_id"] == "token-1"
    assert records.status_code == 200
    assert len(records.json()) == 1
    assert records.json()[0]["message"] == "token-1"


def test_api_updates_paused_task_in_place(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-9",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.7"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
        position_size=Decimal("100"),
        average_cost=Decimal("0.42"),
        status=TaskStatus.PAUSED,
    )

    client = TestClient(create_app(service, position_reader=FakePositionReader()))
    response = client.put(
        f"/tasks/{task.task_id}",
        json={
            "dry_run": False,
            "slippage_bps": "75",
            "position_size": "80",
            "average_cost": "0.4",
            "rules": [
                {
                    "kind": "price-stop",
                    "sell_size": "50",
                    "trigger_price": "0.33",
                    "label": "updated-stop",
                },
                {
                    "kind": "take-profit",
                    "sell_size": "25",
                    "trigger_price": "0.88",
                    "label": "updated-tp",
                },
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == task.task_id
    assert payload["status"] == "paused"
    assert payload["dry_run"] is False
    assert payload["slippage_bps"] == "75"
    assert payload["position_size"] == "80"
    assert payload["average_cost"] == "0.4"
    assert [rule["name"] for rule in payload["rules"]] == ["updated-stop", "updated-tp"]


def test_api_rejects_updating_active_task(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-10",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.7"),
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
    )

    client = TestClient(create_app(service, position_reader=FakePositionReader()))
    response = client.put(
        f"/tasks/{task.task_id}",
        json={
            "dry_run": True,
            "slippage_bps": "50",
            "position_size": None,
            "average_cost": None,
            "rules": [
                {
                    "kind": "take-profit",
                    "sell_size": "25",
                    "trigger_price": "0.75",
                }
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "task must be paused before updating"


def test_api_exposes_rule_runtime_state_in_task_response(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    task = service.create_task(
        token_id="token-11",
        rules=(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_size=Decimal("25"),
                trigger_price=Decimal("0.7"),
                label="tp-live",
            ),
        ),
        dry_run=True,
        slippage_bps=Decimal("50"),
        status=TaskStatus.PAUSED,
    )
    service.replace_rule_states(
        task.task_id,
        {
            "tp-live": RuleState(
                locked_size=Decimal("25"),
                sold_size=Decimal("10"),
                trigger_bid=Decimal("0.72"),
                peak_bid=Decimal("0.91"),
            )
        },
    )

    client = TestClient(create_app(service, position_reader=FakePositionReader()))
    response = client.get(f"/tasks/{task.task_id}")

    assert response.status_code == 200
    runtime_state = response.json()["rules"][0]["runtime_state"]
    assert runtime_state["locked_size"] == "25"
    assert runtime_state["sold_size"] == "10"
    assert runtime_state["remaining_size"] == "15"
    assert runtime_state["trigger_bid"] == "0.72"
    assert runtime_state["peak_bid"] == "0.91"
    assert runtime_state["is_triggered"] is True
    assert runtime_state["is_complete"] is False


def test_api_rejects_legacy_sell_ratio_payload(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    client = TestClient(create_app(service))

    response = client.post(
        "/tasks",
        json={
            "token_id": "token-legacy",
            "dry_run": True,
            "slippage_bps": "50",
            "rules": [{"kind": "breakeven-stop", "sell_ratio": "0.5"}],
        },
    )

    assert response.status_code == 422