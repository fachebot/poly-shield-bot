from __future__ import annotations

"""FastAPI 后端接口，供 CLI、网页和 Telegram 复用。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from poly_shield.backend.models import ExecutionRecord, ManagedTask, TaskStatus
from poly_shield.backend.runtime import ManagedTaskRuntime
from poly_shield.backend.service import TaskConflictError, TaskNotFoundError, TaskService
from poly_shield.config import PolymarketCredentials
from poly_shield.polymarket import PolymarketConfigurationError, PolymarketGateway, PolymarketRequestError
from poly_shield.positions import PositionReader, PositionRecord
from poly_shield.rules import ExitRule, RuleKind, RuleState


PERCENT_PNL_QUANTUM = Decimal("0.00000001")


class RulePayload(BaseModel):
    """任务规则的 API 输入。"""

    kind: RuleKind
    sell_size: Decimal
    trigger_price: Decimal | None = None
    drawdown_ratio: Decimal | None = None
    label: str | None = None

    def to_domain(self) -> ExitRule:
        """转换成领域层 ExitRule。"""
        return ExitRule(
            kind=self.kind,
            sell_size=self.sell_size,
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


class TaskUpdateRequest(BaseModel):
    """更新任务请求。"""

    dry_run: bool = True
    slippage_bps: Decimal = Decimal("50")
    position_size: Decimal | None = None
    average_cost: Decimal | None = None
    rules: list[RulePayload] = Field(min_length=1)


class RuleRuntimeStateResponse(BaseModel):
    """规则运行态输出。"""

    locked_size: str | None = None
    sold_size: str
    remaining_size: str
    trigger_bid: str | None = None
    peak_bid: str | None = None
    is_triggered: bool
    is_complete: bool


class RuleResponse(BaseModel):
    """规则输出。"""

    name: str
    kind: RuleKind
    sell_size: str
    trigger_price: str | None = None
    drawdown_ratio: str | None = None
    label: str | None = None
    runtime_state: RuleRuntimeStateResponse


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


class PositionResponse(BaseModel):
    """持仓输出。"""

    token_id: str
    size: str
    average_cost: str
    current_price: str
    current_value: str
    cash_pnl: str
    percent_pnl: str
    outcome: str | None = None
    market: str | None = None
    title: str | None = None
    event_slug: str | None = None
    slug: str | None = None
    proxy_wallet: str | None = None


def create_app(
    service: TaskService,
    runtime: ManagedTaskRuntime | None = None,
    *,
    position_reader: PositionReader | None = None,
    web_root: Path | None = None,
) -> FastAPI:
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
    resolved_web_root = web_root or Path(__file__).with_name("web")
    if resolved_web_root.exists():
        app.mount("/assets", StaticFiles(directory=resolved_web_root), name="assets")

    async def refresh_runtime() -> None:
        if runtime is not None:
            await runtime.refresh_active_tasks()

    def serialize_task(task: ManagedTask) -> TaskResponse:
        return _serialize_task(task, service.load_rule_states(task.task_id))

    def get_position_reader() -> PositionReader:
        if position_reader is not None:
            return position_reader
        return PolymarketGateway(PolymarketCredentials.from_env())

    @app.get("/", response_class=HTMLResponse, response_model=None)
    def index():
        index_file = resolved_web_root / "index.html"
        if not index_file.exists():
            return HTMLResponse("Poly Shield web UI is not available.", status_code=404)
        return FileResponse(index_file)

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

    @app.get("/positions", response_model=list[PositionResponse])
    def list_positions(
        token_id: str | None = None,
        size_threshold: Decimal = Query(default=Decimal("0")),
    ) -> list[PositionResponse]:
        reader = get_position_reader()
        try:
            positions = reader.list_positions(size_threshold=size_threshold)
        except PolymarketConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PolymarketRequestError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if token_id is not None:
            positions = [position for position in positions if position.token_id == token_id]
        positions = _prefer_best_bid_prices(reader, positions)
        return [_serialize_position(position) for position in positions]

    @app.get("/tasks", response_model=list[TaskResponse])
    def list_tasks(
        status: TaskStatus | None = None,
        include_deleted: bool = False,
        token_id: str | None = None,
    ) -> list[TaskResponse]:
        tasks = service.list_tasks(
            status=status,
            include_deleted=include_deleted,
            token_id=token_id,
        )
        return [serialize_task(task) for task in tasks]

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
        return serialize_task(task)

    @app.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str) -> TaskResponse:
        try:
            task = service.get_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"task not found: {task_id}") from exc
        return serialize_task(task)

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
        return serialize_task(task)

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
        return serialize_task(task)

    @app.delete("/tasks/{task_id}", response_model=TaskResponse)
    async def delete_task(task_id: str) -> TaskResponse:
        try:
            task = service.delete_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"task not found: {task_id}") from exc
        await refresh_runtime()
        return serialize_task(task)

    @app.get("/records", response_model=list[ExecutionRecordResponse])
    def list_records(
        task_id: str | None = None,
        token_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[ExecutionRecordResponse]:
        records = service.list_execution_records(
            task_id=task_id,
            token_id=token_id,
            limit=limit,
        )
        return [_serialize_record(record) for record in records]

    @app.put("/tasks/{task_id}", response_model=TaskResponse)
    async def update_task(task_id: str, request: TaskUpdateRequest) -> TaskResponse:
        try:
            task = service.update_task(
                task_id,
                rules=tuple(rule.to_domain() for rule in request.rules),
                dry_run=request.dry_run,
                slippage_bps=request.slippage_bps,
                position_size=request.position_size,
                average_cost=request.average_cost,
            )
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"task not found: {task_id}") from exc
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await refresh_runtime()
        return serialize_task(task)
    return app


def _serialize_task(task: ManagedTask, rule_states: dict[str, RuleState]) -> TaskResponse:
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
                sell_size=str(rule.sell_size),
                trigger_price=None if rule.trigger_price is None else str(
                    rule.trigger_price),
                drawdown_ratio=None if rule.drawdown_ratio is None else str(
                    rule.drawdown_ratio),
                label=rule.label,
                runtime_state=_serialize_rule_runtime_state(
                    rule_states.get(rule.name, RuleState()),
                ),
            )
            for rule in task.rules
        ],
    )


def _serialize_rule_runtime_state(state: RuleState) -> RuleRuntimeStateResponse:
    return RuleRuntimeStateResponse(
        locked_size=None if state.locked_size is None else str(state.locked_size),
        sold_size=str(state.sold_size),
        remaining_size=str(state.remaining_size),
        trigger_bid=None if state.trigger_bid is None else str(state.trigger_bid),
        peak_bid=None if state.peak_bid is None else str(state.peak_bid),
        is_triggered=state.is_triggered,
        is_complete=state.is_complete,
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


def _serialize_position(position: PositionRecord) -> PositionResponse:
    return PositionResponse(
        token_id=position.token_id,
        size=str(position.size),
        average_cost=str(position.average_cost),
        current_price=str(position.current_price),
        current_value=str(position.current_value),
        cash_pnl=str(position.cash_pnl),
        percent_pnl=str(position.percent_pnl),
        outcome=position.outcome,
        market=position.market,
        title=position.title,
        event_slug=position.event_slug,
        slug=position.slug,
        proxy_wallet=position.proxy_wallet,
    )


def _prefer_best_bid_prices(
    position_reader: PositionReader,
    positions: list[PositionRecord],
) -> list[PositionRecord]:
    get_best_bid = getattr(position_reader, "get_best_bid", None)
    if not callable(get_best_bid):
        return positions

    enriched_positions: list[PositionRecord] = []
    for position in positions:
        try:
            best_bid = get_best_bid(position.token_id)
        except (PolymarketConfigurationError, PolymarketRequestError):
            enriched_positions.append(position)
            continue
        if best_bid is None or best_bid <= 0:
            enriched_positions.append(position)
            continue
        enriched_positions.append(_position_with_best_bid(position, best_bid))
    return enriched_positions


def _position_with_best_bid(
    position: PositionRecord,
    best_bid: Decimal,
) -> PositionRecord:
    current_value = position.size * best_bid
    if position.size > 0 and position.average_cost > 0:
        cost_basis = position.size * position.average_cost
        cash_pnl = current_value - cost_basis
        percent_pnl = (cash_pnl / cost_basis).quantize(PERCENT_PNL_QUANTUM)
    else:
        cash_pnl = position.cash_pnl
        percent_pnl = position.percent_pnl
    return replace(
        position,
        current_price=best_bid,
        current_value=current_value,
        cash_pnl=cash_pnl,
        percent_pnl=percent_pnl,
    )
