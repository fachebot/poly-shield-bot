from __future__ import annotations

"""FastAPI 后端接口，供 CLI、网页和 Telegram 复用。"""

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from poly_shield.backend.models import ExecutionRecord, ManagedTask, TaskStatus
from poly_shield.backend.runtime import ManagedTaskRuntime
from poly_shield.backend.service import TaskConflictError, TaskNotFoundError, TaskService
from poly_shield.rules import ExitRule, RuleKind


class RulePayload(BaseModel):
    """任务规则的 API 输入。"""

    kind: RuleKind
    sell_ratio: Decimal
    trigger_price: Decimal | None = None
    drawdown_ratio: Decimal | None = None
    label: str | None = None

    def to_domain(self) -> ExitRule:
        """转换成领域层 ExitRule。"""
        return ExitRule(
            kind=self.kind,
            sell_ratio=self.sell_ratio,
            trigger_price=self.trigger_price,
            drawdown_ratio=self.drawdown_ratio,
            label=self.label,
        )


class TaskCreateRequest(BaseModel):
    """创建任务请求。"""

    token_id: str = Field(min_length=1)
    dry_run: bool = True
    slippage_bps: Decimal = Decimal("50")
    position_size: Decimal | None = None
    average_cost: Decimal | None = None
    status: TaskStatus = TaskStatus.ACTIVE
    rules: list[RulePayload] = Field(min_length=1)


class RuleResponse(BaseModel):
    """规则输出。"""

    name: str
    kind: RuleKind
    sell_ratio: str
    trigger_price: str | None = None
    drawdown_ratio: str | None = None
    label: str | None = None


class TaskResponse(BaseModel):
    """任务输出。"""

    task_id: str
    token_id: str
    status: TaskStatus
    dry_run: bool
    slippage_bps: str
    position_size: str | None = None
    average_cost: str | None = None
    created_at: datetime
    updated_at: datetime
    rules: list[RuleResponse]


class ExecutionRecordResponse(BaseModel):
    """执行记录输出。"""

    record_id: str
    task_id: str
    token_id: str
    rule_name: str
    event_type: str
    status: str
    order_id: str | None = None
    market_id: str | None = None
    event_price: str
    best_bid: str
    best_ask: str
    trigger_price: str
    requested_size: str
    filled_size: str
    message: str
    created_at: datetime


def create_app(service: TaskService, runtime: ManagedTaskRuntime | None = None) -> FastAPI:
    """创建后端 API 应用。"""
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if runtime is not None:
            await runtime.start()
        yield
        if runtime is not None:
            await runtime.stop()

    app = FastAPI(title="Poly Shield Backend",
                  version="0.1.0", lifespan=lifespan)

    async def refresh_runtime() -> None:
        if runtime is not None:
            await runtime.refresh_active_tasks()

    @app.get("/health")
    def health() -> dict[str, object]:
        payload = {
            "status": "ok",
            "restored_task_count": service.restored_task_count,
            "active_task_ids": sorted(service.active_tasks),
        }
        if runtime is not None:
            payload["runtime"] = runtime.snapshot()
        return payload

    @app.get("/tasks", response_model=list[TaskResponse])
    def list_tasks(status: TaskStatus | None = None, include_deleted: bool = False) -> list[TaskResponse]:
        tasks = service.list_tasks(
            status=status, include_deleted=include_deleted)
        return [_serialize_task(task) for task in tasks]

    @app.post("/tasks", response_model=TaskResponse, status_code=201)
    async def create_task(request: TaskCreateRequest) -> TaskResponse:
        try:
            task = service.create_task(
                token_id=request.token_id,
                rules=tuple(rule.to_domain() for rule in request.rules),
                dry_run=request.dry_run,
                slippage_bps=request.slippage_bps,
                position_size=request.position_size,
                average_cost=request.average_cost,
                status=request.status,
            )
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await refresh_runtime()
        return _serialize_task(task)

    @app.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str) -> TaskResponse:
        try:
            task = service.get_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"task not found: {task_id}") from exc
        return _serialize_task(task)

    @app.post("/tasks/{task_id}/pause", response_model=TaskResponse)
    async def pause_task(task_id: str) -> TaskResponse:
        try:
            task = service.pause_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"task not found: {task_id}") from exc
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await refresh_runtime()
        return _serialize_task(task)

    @app.post("/tasks/{task_id}/resume", response_model=TaskResponse)
    async def resume_task(task_id: str) -> TaskResponse:
        try:
            task = service.resume_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"task not found: {task_id}") from exc
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await refresh_runtime()
        return _serialize_task(task)

    @app.delete("/tasks/{task_id}", response_model=TaskResponse)
    async def delete_task(task_id: str) -> TaskResponse:
        try:
            task = service.delete_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"task not found: {task_id}") from exc
        await refresh_runtime()
        return _serialize_task(task)

    @app.get("/records", response_model=list[ExecutionRecordResponse])
    def list_records(task_id: str | None = None, limit: int = Query(default=100, ge=1, le=1000)) -> list[ExecutionRecordResponse]:
        records = service.list_execution_records(task_id=task_id, limit=limit)
        return [_serialize_record(record) for record in records]

    return app


def _serialize_task(task: ManagedTask) -> TaskResponse:
    return TaskResponse(
        task_id=task.task_id,
        token_id=task.token_id,
        status=task.status,
        dry_run=task.dry_run,
        slippage_bps=str(task.slippage_bps),
        position_size=None if task.position_size is None else str(
            task.position_size),
        average_cost=None if task.average_cost is None else str(
            task.average_cost),
        created_at=task.created_at,
        updated_at=task.updated_at,
        rules=[
            RuleResponse(
                name=rule.name,
                kind=rule.kind,
                sell_ratio=str(rule.sell_ratio),
                trigger_price=None if rule.trigger_price is None else str(
                    rule.trigger_price),
                drawdown_ratio=None if rule.drawdown_ratio is None else str(
                    rule.drawdown_ratio),
                label=rule.label,
            )
            for rule in task.rules
        ],
    )


def _serialize_record(record: ExecutionRecord) -> ExecutionRecordResponse:
    return ExecutionRecordResponse(
        record_id=record.record_id,
        task_id=record.task_id,
        token_id=record.token_id,
        rule_name=record.rule_name,
        event_type=record.event_type,
        status=record.status,
        order_id=record.order_id,
        market_id=record.market_id,
        event_price=str(record.event_price),
        best_bid=str(record.best_bid),
        best_ask=str(record.best_ask),
        trigger_price=str(record.trigger_price),
        requested_size=str(record.requested_size),
        filled_size=str(record.filled_size),
        message=record.message,
        created_at=record.created_at,
    )
