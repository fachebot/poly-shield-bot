from __future__ import annotations

"""FastAPI 后端接口，供 CLI 和 Telegram 复用。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from poly_shield.backend.models import ExecutionRecord, ManagedTask, TaskStatus
from poly_shield.backend.runtime import ManagedTaskRuntime
from poly_shield.backend.service import TaskConflictError, TaskNotFoundError, TaskService
from poly_shield.config import PolymarketCredentials
from poly_shield.polymarket import PolymarketConfigurationError, PolymarketGateway, PolymarketRequestError
from poly_shield.positions import PositionReader, PositionRecord
from poly_shield.rules import ExitRule, RuleKind, RuleState


PERCENT_PNL_QUANTUM = Decimal("0.00000001")
TASK_DETAIL_RECORD_PAGE_SIZE = 20


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
    title: str | None = None


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


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _format_cents(value: str | Decimal | None) -> str:
    if value in (None, ""):
        return "—"
    amount = Decimal(str(value)) * Decimal("100")
    cents = format(amount.normalize(), "f")
    if "." in cents:
        cents = cents.rstrip("0").rstrip(".")
    return f"{cents}c"


def _to_cents_input(value: str | Decimal | None) -> str:
    if value in (None, ""):
        return ""
    amount = Decimal(str(value)) * Decimal("100")
    cents = format(amount.normalize(), "f")
    if "." in cents:
        cents = cents.rstrip("0").rstrip(".")
    return cents


_templates.env.globals["format_cents"] = _format_cents
_templates.env.globals["to_cents_input"] = _to_cents_input


def _build_polymarket_url(*, event_slug: str | None, slug: str | None) -> str | None:
    event = (event_slug or "").strip()
    market = (slug or "").strip()
    if event and market:
        # Market-specific page: /event/{event_slug}/{market_slug}
        return f"https://polymarket.com/event/{event}/{market}"
    if event:
        return f"https://polymarket.com/event/{event}"
    if market:
        return f"https://polymarket.com/event/{market}"
    return None


def create_app(
    service: TaskService,
    runtime: ManagedTaskRuntime | None = None,
    *,
    position_reader: PositionReader | None = None,
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

    async def refresh_runtime() -> None:
        if runtime is not None:
            await runtime.refresh_active_tasks()

    def serialize_task(task: ManagedTask) -> TaskResponse:
        return _serialize_task(task, service.load_rule_states(task.task_id))

    def get_position_reader() -> PositionReader:
        if position_reader is not None:
            return position_reader
        return PolymarketGateway(PolymarketCredentials.from_env())

    def _load_live_positions() -> tuple[list[PositionRecord], str | None]:
        try:
            reader = get_position_reader()
            positions = reader.list_positions(size_threshold=Decimal("0"))
            positions = _prefer_best_bid_prices(reader, positions)
            return positions, None
        except PolymarketConfigurationError:
            return [], "config"
        except PolymarketRequestError:
            return [], "network"

    def _is_archived_token(token_id: str) -> bool:
        positions, positions_error = _load_live_positions()
        if positions_error is not None:
            # Fail-open to avoid accidentally blocking edits when position service is unavailable.
            return False
        return token_id not in {position.token_id for position in positions}

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
            positions = [
                position for position in positions if position.token_id == token_id]
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
        rule_name: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[ExecutionRecordResponse]:
        records = service.list_execution_records(
            task_id=task_id,
            token_id=token_id,
            rule_name=rule_name,
            limit=limit,
            offset=offset,
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

    # ── UI routes (Jinja2 + HTMX) ────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def ui_index(request: Request) -> HTMLResponse:
        return _templates.TemplateResponse(request, "index.html")

    @app.get("/ui/panels/overview", response_class=HTMLResponse, include_in_schema=False)
    def ui_overview(request: Request) -> HTMLResponse:
        positions_error: str | None = None
        try:
            reader = get_position_reader()
            positions = reader.list_positions(size_threshold=Decimal("0"))
            positions = _prefer_best_bid_prices(reader, positions)
        except PolymarketConfigurationError:
            positions = []
            positions_error = "config"
        except PolymarketRequestError:
            positions = []
            positions_error = "network"

        total_value_raw = sum(float(p.current_value) for p in positions)
        total_pnl_raw = sum(float(p.cash_pnl) for p in positions)
        total_cost = total_value_raw - total_pnl_raw
        total_pnl_pct_raw = (total_pnl_raw / total_cost *
                             100) if total_cost else 0.0

        tasks_all = service.list_tasks()
        active_count = sum(1 for t in tasks_all if t.status.value == "active")
        paused_count = sum(1 for t in tasks_all if t.status.value == "paused")

        records_today = service.list_execution_records(limit=500)
        today = datetime.now(timezone.utc).date()
        executions_today = sum(
            1 for r in records_today if r.created_at.date() == today)
        confirmed_today = sum(
            1 for r in records_today
            if r.created_at.date() == today and r.event_type == "confirmed"
        )

        sign = "+" if total_pnl_raw >= 0 else ""
        return _templates.TemplateResponse(request, "partials/overview.html", {
            "total_value": f"${total_value_raw:,.2f}",
            "total_pnl": f"{sign}${total_pnl_raw:,.2f}",
            "total_pnl_raw": total_pnl_raw,
            "total_pnl_pct": f"{sign}{total_pnl_pct_raw:.2f}%",
            "total_pnl_pct_raw": total_pnl_pct_raw,
            "active_count": active_count,
            "paused_count": paused_count,
            "executions_today": executions_today,
            "confirmed_today": confirmed_today,
            "positions_error": positions_error,
        })

    @app.get("/ui/panels/health", response_class=HTMLResponse, include_in_schema=False)
    def ui_health(request: Request) -> HTMLResponse:
        snap = runtime.snapshot() if runtime is not None else None
        ctx: dict[str, object] = {"runtime": snap}
        if snap:
            stale = snap.get("stale_seconds", {})
            market_stale = stale.get("market")
            user_stale = stale.get("user")
            ctx["market_stale_ok"] = market_stale is None or market_stale < 30
            ctx["market_idle"] = market_stale is None
            ctx["user_stale_ok"] = user_stale is None or user_stale < 60
            ctx["user_idle"] = user_stale is None
            ctx["restored_count"] = service.restored_task_count
            ctx["runner_count"] = snap.get("runner_count", 0)

            def _age(ts: str | None) -> str:
                if not ts:
                    return "—"
                try:
                    dt = datetime.fromisoformat(ts)
                    secs = int(
                        (datetime.now(timezone.utc) - dt).total_seconds())
                    if secs < 60:
                        return f"{secs}s 前"
                    return f"{secs // 60}m 前"
                except Exception:
                    return ts

            ctx["market_age"] = _age(snap.get("last_market_message_at"))
            ctx["user_age"] = _age(snap.get("last_user_message_at"))
        return _templates.TemplateResponse(request, "partials/health.html", ctx)

    @app.get("/ui/panels/health_chip", response_class=HTMLResponse, include_in_schema=False)
    def ui_health_chip(request: Request) -> HTMLResponse:
        snap = runtime.snapshot() if runtime is not None else None
        is_healthy = False
        if snap:
            stale = snap.get("stale_seconds", {})
            market_stale = stale.get("market")
            user_stale = stale.get("user")
            market_ok = market_stale is None or market_stale < 30
            user_ok = user_stale is None or user_stale < 60
            is_healthy = market_ok and user_ok
        return _templates.TemplateResponse(request, "partials/health_chip.html", {
            "is_healthy": is_healthy,
        })

    @app.get("/ui/panels/runtime_dot", response_class=HTMLResponse, include_in_schema=False)
    def ui_runtime_dot(request: Request) -> HTMLResponse:
        snap = runtime.snapshot() if runtime is not None else None
        is_running = bool(snap and snap.get("running"))
        return _templates.TemplateResponse(request, "partials/runtime_dot.html", {
            "is_running": is_running,
        })

    @app.get("/ui/panels/positions", response_class=HTMLResponse, include_in_schema=False)
    def ui_positions(request: Request, tab: str = Query(default="active")) -> HTMLResponse:
        current_tab = tab if tab in {"active", "archived"} else "active"
        raw_positions, positions_error = _load_live_positions()
        active_token_ids = {position.token_id for position in raw_positions}

        positions_ctx: list[dict[str, object]] = []
        for p in raw_positions:
            cash_pnl_raw = float(p.cash_pnl)
            positions_ctx.append({
                "token_id": p.token_id,
                "title": p.title,
                "size": str(p.size),
                "average_cost": str(p.average_cost),
                "current_price": str(p.current_price),
                "current_value": str(p.current_value),
                "cash_pnl": str(abs(cash_pnl_raw)),
                "cash_pnl_raw": cash_pnl_raw,
                "percent_pnl": str(p.percent_pnl),
                "outcome": p.outcome,
                "is_archived": False,
                "task_count": 0,
            })

        archived_index: dict[str, dict[str, object]] = {}
        for task in sorted(service.list_tasks(include_deleted=False), key=lambda item: item.updated_at, reverse=True):
            if task.token_id in active_token_ids:
                continue
            row = archived_index.get(task.token_id)
            if row is None:
                archived_index[task.token_id] = {
                    "token_id": task.token_id,
                    "title": task.title or f"仓位 {task.token_id[:8]}",
                    "size": "0",
                    "average_cost": "0",
                    "current_price": "0",
                    "current_value": "0",
                    "cash_pnl": "0",
                    "cash_pnl_raw": 0.0,
                    "percent_pnl": "0",
                    "outcome": None,
                    "is_archived": True,
                    "task_count": 1,
                    "updated_at": task.updated_at,
                }
            else:
                row["task_count"] = int(row["task_count"]) + 1
                if not row.get("title") and task.title:
                    row["title"] = task.title

        archived_positions = sorted(
            archived_index.values(),
            key=lambda item: item["updated_at"],
            reverse=True,
        )

        visible_positions = archived_positions if current_tab == "archived" else positions_ctx
        return _templates.TemplateResponse(request, "partials/positions.html", {
            "positions": visible_positions,
            "active_count": len(positions_ctx),
            "archived_count": len(archived_positions),
            "current_tab": current_tab,
            "positions_error": positions_error,
        })

    @app.get("/ui/panels/taskboard", response_class=HTMLResponse, include_in_schema=False)
    def ui_taskboard(request: Request, status: str | None = None, token_id: str | None = None) -> HTMLResponse:
        current_status = status if status and status in TaskStatus._value2member_map_ else ""
        status_filter = TaskStatus(current_status) if current_status else None
        token_filter = token_id.strip() if token_id and token_id.strip() else None
        selected_position_title = ""
        selected_position_url = ""
        selected_position_outcome: str | None = None
        selected_position_price = ""
        is_archived_position = False
        if token_filter:
            positions_for_filter, positions_error = _load_live_positions()
            if positions_error is None:
                is_archived_position = token_filter not in {
                    position.token_id for position in positions_for_filter
                }
            for pos in positions_for_filter:
                if pos.token_id != token_filter:
                    continue
                selected_position_title = (pos.title or "").strip()
                selected_position_url = _build_polymarket_url(
                    event_slug=pos.event_slug,
                    slug=pos.slug,
                ) or ""
                selected_position_outcome = pos.outcome
                selected_position_price = str(pos.current_price)
                break
            if is_archived_position:
                token_tasks = service.list_tasks(
                    include_deleted=False, token_id=token_filter)
                if token_tasks:
                    latest_task = max(
                        token_tasks, key=lambda item: item.updated_at)
                    selected_position_title = (
                        latest_task.title or "").strip() or token_filter
        tasks = service.list_tasks(
            status=status_filter, include_deleted=False, token_id=token_filter)
        tasks_with_states = [_serialize_task(
            t, service.load_rule_states(t.task_id)) for t in tasks]
        # Counts scoped to the same token_id filter so tabs show per-position numbers
        all_tasks = service.list_tasks(
            include_deleted=False, token_id=token_filter)
        counts = {
            "all": len(all_tasks),
            "active": sum(1 for t in all_tasks if t.status.value == "active"),
            "completed": sum(1 for t in all_tasks if t.status.value == "completed"),
            "paused": sum(1 for t in all_tasks if t.status.value == "paused"),
        }
        return _templates.TemplateResponse(request, "partials/taskboard.html", {
            "tasks": tasks_with_states,
            "counts": counts,
            "current_status": current_status,
            "token_id": token_filter or "",
            "selected_position_title": selected_position_title,
            "selected_position_url": selected_position_url,
            "selected_position_outcome": selected_position_outcome,
            "selected_position_price": selected_position_price,
            "is_archived_position": is_archived_position,
        })

    @app.get("/ui/panels/task_detail/{task_id}", response_class=HTMLResponse, include_in_schema=False)
    def ui_task_detail(request: Request, task_id: str) -> HTMLResponse:
        try:
            task = service.get_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        task_ctx = _serialize_task(task, service.load_rule_states(task_id))
        return _templates.TemplateResponse(request, "partials/task_detail.html", {
            "task": task_ctx,
            "record_page_size": TASK_DETAIL_RECORD_PAGE_SIZE,
            "is_archived_position": _is_archived_token(task.token_id),
        })

    @app.get("/ui/panels/task_detail/{task_id}/records", response_class=HTMLResponse, include_in_schema=False)
    def ui_task_detail_records(
        request: Request,
        task_id: str,
        rule_name: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=TASK_DETAIL_RECORD_PAGE_SIZE, ge=1, le=100),
    ) -> HTMLResponse:
        try:
            service.get_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        records = service.list_execution_records(
            task_id=task_id,
            rule_name=rule_name,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(records) > limit
        page_records = records[:limit]
        return _templates.TemplateResponse(request, "partials/task_detail_records_chunk.html", {
            "task_id": task_id,
            "rule_name": rule_name,
            "records": page_records,
            "next_offset": offset + len(page_records),
            "limit": limit,
            "has_more": has_more,
        })

    @app.get("/ui/modals/create_task", response_class=HTMLResponse, include_in_schema=False)
    def ui_create_task_modal(request: Request, token_id: str | None = None) -> HTMLResponse:
        prefill: dict | None = None
        if token_id and token_id.strip():
            if _is_archived_token(token_id.strip()):
                return _templates.TemplateResponse(request, "partials/task_create_modal.html", {
                    "error": "存档仓位为只读，不允许创建任务。若已重新建仓，请刷新仓位列表后在活跃仓位中操作。",
                    "prefill": {"token_id": token_id.strip()},
                })
            prefill = {"token_id": token_id.strip()}
            # Try to prefill position_size and average_cost from live positions
            try:
                reader = get_position_reader()
                for pos in reader.list_positions(size_threshold=Decimal("0")):
                    if pos.token_id == token_id.strip():
                        prefill["position_size"] = str(pos.size)
                        prefill["average_cost"] = str(pos.average_cost)
                        prefill["title_hint"] = pos.title or ""
                        break
            except Exception:
                pass  # Prefill without size/cost if positions unavailable
        return _templates.TemplateResponse(request, "partials/task_create_modal.html", {
            "error": None,
            "prefill": prefill,
        })

    @app.get("/ui/modals/edit_task/{task_id}", response_class=HTMLResponse, include_in_schema=False)
    def ui_edit_task_modal(request: Request, task_id: str) -> HTMLResponse:
        try:
            task = service.get_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if _is_archived_token(task.token_id):
            raise HTTPException(status_code=409, detail="存档仓位为只读，不允许编辑任务")
        task_ctx = _serialize_task(task, service.load_rule_states(task_id))
        return _templates.TemplateResponse(request, "partials/task_edit_modal.html", {
            "task": task_ctx,
            "error": None,
        })

    @app.post("/ui/tasks/create", response_class=HTMLResponse, include_in_schema=False)
    async def ui_create_task(request: Request) -> HTMLResponse:
        form = await request.form()

        def _field(name: str) -> str | None:
            v = form.get(name)
            return str(v).strip() if v else None

        token_id = _field("token_id") or ""
        if not token_id:
            return _templates.TemplateResponse(request, "partials/task_create_modal.html", {
                "error": "Token ID 不能为空",
                "prefill": dict(form),
            })
        token_tab = _field("_token_tab") or "active"
        if _is_archived_token(token_id):
            return _templates.TemplateResponse(request, "partials/task_create_modal.html", {
                "error": "存档仓位为只读，不允许创建任务。若已重新建仓，请刷新仓位列表。",
                "prefill": dict(form),
            })

        # Collect rules by scanning numbered keys
        rules: list[ExitRule] = []
        idx = 0
        while True:
            kind_val = _field(f"rule_kind_{idx}")
            if kind_val is None:
                break
            sell_size_val = _field(f"rule_sell_size_{idx}")
            if not sell_size_val:
                idx += 1
                continue
            try:
                rule_kind = RuleKind(kind_val)
                sell_size = Decimal(sell_size_val)
                trigger_raw = _field(f"rule_trigger_price_{idx}")
                trigger_price = Decimal(trigger_raw) if trigger_raw else None
                drawdown_raw = _field(f"rule_drawdown_ratio_{idx}")
                drawdown_ratio = Decimal(
                    drawdown_raw) if drawdown_raw else None
                label = _field(f"rule_label_{idx}") or None
                rules.append(ExitRule(
                    kind=rule_kind,
                    sell_size=sell_size,
                    trigger_price=trigger_price,
                    drawdown_ratio=drawdown_ratio,
                    label=label,
                ))
            except (ValueError, Exception):
                pass
            idx += 1

        if not rules:
            return _templates.TemplateResponse(request, "partials/task_create_modal.html", {
                "error": "至少需要一条有效规则",
                "prefill": dict(form),
            })

        slippage_raw = _field("slippage_bps") or "50"
        position_raw = _field("position_size")
        avg_cost_raw = _field("average_cost")
        dry_run = form.get("dry_run") is not None  # checkbox: present = True
        title = _field("title") or None

        try:
            task = service.create_task(
                token_id=token_id,
                rules=tuple(rules),
                dry_run=dry_run,
                slippage_bps=Decimal(slippage_raw),
                position_size=Decimal(position_raw) if position_raw else None,
                average_cost=Decimal(avg_cost_raw) if avg_cost_raw else None,
                title=title,
            )
        except (TaskConflictError, ValueError) as exc:
            return _templates.TemplateResponse(request, "partials/task_create_modal.html", {
                "error": str(exc),
                "prefill": dict(form),
            })

        await refresh_runtime()

        # Return the updated task board, preserving any active position filter
        token_filter_create = _field("_token_filter") or None
        tasks = service.list_tasks(
            include_deleted=False, token_id=token_filter_create)
        tasks_with_states = [_serialize_task(
            t, service.load_rule_states(t.task_id)) for t in tasks]
        all_tasks_for_counts = tasks  # counts scoped to same token filter
        counts = {
            "all": len(all_tasks_for_counts),
            "active": sum(1 for t in all_tasks_for_counts if t.status.value == "active"),
            "completed": sum(1 for t in all_tasks_for_counts if t.status.value == "completed"),
            "paused": sum(1 for t in all_tasks_for_counts if t.status.value == "paused"),
        }
        response = _templates.TemplateResponse(request, "partials/taskboard.html", {
            "tasks": tasks_with_states,
            "counts": counts,
            "current_status": "",
            "token_id": token_filter_create or "",
            "is_archived_position": _is_archived_token(token_filter_create or "") if token_filter_create else False,
        })
        response.headers["HX-Trigger"] = "taskCreated"
        response.headers["X-Position-Tab"] = token_tab
        return response

    @app.put("/ui/tasks/{task_id}", response_class=HTMLResponse, include_in_schema=False)
    async def ui_update_task(request: Request, task_id: str) -> HTMLResponse:
        try:
            task = service.get_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        form = await request.form()

        def _edit_field(name: str) -> str | None:
            v = form.get(name)
            return str(v).strip() if v else None

        rules: list[ExitRule] = []
        idx = 0
        while True:
            kind_val = _edit_field(f"rule_kind_{idx}")
            if kind_val is None:
                break
            sell_size_val = _edit_field(f"rule_sell_size_{idx}")
            if not sell_size_val:
                idx += 1
                continue
            try:
                rule_kind = RuleKind(kind_val)
                sell_size = Decimal(sell_size_val)
                trigger_raw = _edit_field(f"rule_trigger_price_{idx}")
                trigger_price = Decimal(trigger_raw) if trigger_raw else None
                drawdown_raw = _edit_field(f"rule_drawdown_ratio_{idx}")
                drawdown_ratio = Decimal(
                    drawdown_raw) if drawdown_raw else None
                label = _edit_field(f"rule_label_{idx}") or None
                rules.append(ExitRule(
                    kind=rule_kind,
                    sell_size=sell_size,
                    trigger_price=trigger_price,
                    drawdown_ratio=drawdown_ratio,
                    label=label,
                ))
            except (ValueError, Exception):
                pass
            idx += 1

        def _render_edit_error(msg: str) -> HTMLResponse:
            task_ctx = _serialize_task(task, service.load_rule_states(task_id))
            resp = _templates.TemplateResponse(request, "partials/task_edit_modal.html", {
                "task": task_ctx,
                "error": msg,
            })
            resp.headers["HX-Retarget"] = "#modal-slot"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp

        if not rules:
            return _render_edit_error("至少需要一条有效规则")
        if _is_archived_token(task.token_id):
            return _render_edit_error("存档仓位为只读，不允许编辑任务")

        slippage_raw = _edit_field("slippage_bps") or "50"
        position_raw = _edit_field("position_size")
        avg_cost_raw = _edit_field("average_cost")
        dry_run = form.get("dry_run") is not None

        try:
            updated_task = service.update_task(
                task_id,
                rules=tuple(rules),
                dry_run=dry_run,
                slippage_bps=Decimal(slippage_raw),
                position_size=Decimal(position_raw) if position_raw else None,
                average_cost=Decimal(avg_cost_raw) if avg_cost_raw else None,
            )
        except (TaskConflictError, ValueError) as exc:
            return _render_edit_error(str(exc))

        await refresh_runtime()
        task_ctx = _serialize_task(
            updated_task, service.load_rule_states(task_id))
        response = _templates.TemplateResponse(request, "partials/task_detail.html", {
            "task": task_ctx,
            "record_page_size": TASK_DETAIL_RECORD_PAGE_SIZE,
            "is_archived_position": _is_archived_token(updated_task.token_id),
        })
        response.headers["HX-Trigger"] = "taskUpdated, editTaskSuccess"
        return response

    @app.post("/ui/actions/tasks/{task_id}/pause", response_class=HTMLResponse, include_in_schema=False)
    async def ui_pause_task(request: Request, task_id: str) -> HTMLResponse:
        try:
            task = service.pause_task(task_id)
        except (TaskNotFoundError, TaskConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await refresh_runtime()
        task_ctx = _serialize_task(task, service.load_rule_states(task_id))
        response = _templates.TemplateResponse(request, "partials/task_detail.html", {
            "task": task_ctx,
            "record_page_size": TASK_DETAIL_RECORD_PAGE_SIZE,
            "is_archived_position": _is_archived_token(task.token_id),
        })
        response.headers["HX-Trigger"] = "taskUpdated"
        return response

    @app.post("/ui/actions/tasks/{task_id}/resume", response_class=HTMLResponse, include_in_schema=False)
    async def ui_resume_task(request: Request, task_id: str) -> HTMLResponse:
        try:
            task = service.resume_task(task_id)
        except (TaskNotFoundError, TaskConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await refresh_runtime()
        task_ctx = _serialize_task(task, service.load_rule_states(task_id))
        response = _templates.TemplateResponse(request, "partials/task_detail.html", {
            "task": task_ctx,
            "record_page_size": TASK_DETAIL_RECORD_PAGE_SIZE,
            "is_archived_position": _is_archived_token(task.token_id),
        })
        response.headers["HX-Trigger"] = "taskUpdated"
        return response

    @app.delete("/ui/actions/tasks/{task_id}", response_class=HTMLResponse, include_in_schema=False)
    async def ui_delete_task(request: Request, task_id: str) -> HTMLResponse:
        try:
            service.delete_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await refresh_runtime()
        response = HTMLResponse(content="", status_code=200)
        response.headers["HX-Trigger"] = "taskDeleted"
        return response

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
        title=task.title,
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
        locked_size=None if state.locked_size is None else str(
            state.locked_size),
        sold_size=str(state.sold_size),
        remaining_size=str(state.remaining_size),
        trigger_bid=None if state.trigger_bid is None else str(
            state.trigger_bid),
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
