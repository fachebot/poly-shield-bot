from decimal import Decimal

from contextlib import AbstractAsyncContextManager

from fastapi.testclient import TestClient

from poly_shield.backend.api import create_app
from poly_shield.backend.models import ExecutionRecord
from poly_shield.backend.service import TaskService
from poly_shield.rules import ExitRule, RuleKind


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
                    "sell_ratio": "0.5",
                },
                {
                    "kind": "take-profit",
                    "sell_ratio": "0.25",
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
                sell_ratio=Decimal("0.25"),
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
                "rules": [{"kind": "breakeven-stop", "sell_ratio": "0.5"}],
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