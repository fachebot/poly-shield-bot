from __future__ import annotations

"""后端任务服务，负责协调仓储与运行中任务注册表。"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from poly_shield.backend.models import ExecutionAttempt, ExecutionAttemptStatus, ExecutionRecord, ManagedTask, RuntimeLease, TaskStatus
from poly_shield.backend.store import SQLiteTaskStore
from poly_shield.rules import ExitRule, RuleState
from poly_shield.watcher import WatchTask


DEFAULT_DB_PATH = Path("data") / "poly-shield.db"


class TaskConflictError(RuntimeError):
    """任务冲突，例如同一 token 重复激活。"""


class TaskNotFoundError(KeyError):
    """任务不存在。"""


class RuntimeLeaseConflictError(RuntimeError):
    """当前数据库已经被另一实例持有运行时租约。"""


@dataclass
class TaskService:
    """任务管理服务。"""

    store: SQLiteTaskStore
    active_tasks: dict[str, ManagedTask] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.restore_active_tasks()

    @classmethod
    def from_db_path(cls, db_path: Path = DEFAULT_DB_PATH) -> "TaskService":
        """从默认数据库路径创建服务。"""
        return cls(store=SQLiteTaskStore(db_path))

    @property
    def restored_task_count(self) -> int:
        """当前从数据库恢复进内存注册表的 active 任务数量。"""
        return len(self.active_tasks)

    def restore_active_tasks(self) -> tuple[ManagedTask, ...]:
        """从数据库恢复 active 任务到内存注册表。"""
        restored = tuple(self.store.list_tasks(status=TaskStatus.ACTIVE))
        self.active_tasks = {task.task_id: task for task in restored}
        return restored

    def create_task(
        self,
        *,
        token_id: str,
        rules: tuple[ExitRule, ...],
        dry_run: bool,
        slippage_bps: Decimal,
        position_size: Decimal | None = None,
        average_cost: Decimal | None = None,
        status: TaskStatus = TaskStatus.ACTIVE,
    ) -> ManagedTask:
        """创建任务并在需要时加入 active 注册表。"""
        WatchTask(token_id=token_id, rules=rules, dry_run=dry_run)
        if status is TaskStatus.ACTIVE:
            self._ensure_token_available(token_id)
        task = self.store.create_task(
            token_id=token_id,
            rules=rules,
            dry_run=dry_run,
            slippage_bps=slippage_bps,
            position_size=position_size,
            average_cost=average_cost,
            status=status,
        )
        if task.status is TaskStatus.ACTIVE:
            self.active_tasks[task.task_id] = task
        return task

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        include_deleted: bool = False,
        token_id: str | None = None,
    ) -> list[ManagedTask]:
        """列出任务。"""
        return self.store.list_tasks(
            status=status,
            include_deleted=include_deleted,
            token_id=token_id,
        )

    def get_task(self, task_id: str) -> ManagedTask:
        """读取单个任务。"""
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def pause_task(self, task_id: str) -> ManagedTask:
        """暂停任务。"""
        task = self.get_task(task_id)
        if task.status is TaskStatus.DELETED:
            raise TaskConflictError("deleted tasks cannot be paused")
        return self.set_task_status(task_id, TaskStatus.PAUSED)

    def resume_task(self, task_id: str) -> ManagedTask:
        """恢复已暂停任务。"""
        task = self.get_task(task_id)
        if task.status is TaskStatus.DELETED:
            raise TaskConflictError("deleted tasks cannot be resumed")
        self._ensure_token_available(task.token_id, excluding_task_id=task_id)
        return self.set_task_status(task_id, TaskStatus.ACTIVE)

    def delete_task(self, task_id: str) -> ManagedTask:
        """软删除任务。"""
        self.get_task(task_id)
        return self.set_task_status(task_id, TaskStatus.DELETED)

    def update_task(
        self,
        task_id: str,
        *,
        rules: tuple[ExitRule, ...],
        dry_run: bool,
        slippage_bps: Decimal,
        position_size: Decimal | None = None,
        average_cost: Decimal | None = None,
    ) -> ManagedTask:
        """更新任务定义。仅允许 paused 任务修改。"""
        task = self.get_task(task_id)
        if task.status is not TaskStatus.PAUSED:
            raise TaskConflictError("task must be paused before updating")
        WatchTask(token_id=task.token_id, rules=rules, dry_run=dry_run)
        updated = self.store.update_task(
            task_id,
            rules=rules,
            dry_run=dry_run,
            slippage_bps=slippage_bps,
            position_size=position_size,
            average_cost=average_cost,
        )
        self.active_tasks.pop(task_id, None)
        return updated

    def set_task_status(self, task_id: str, status: TaskStatus) -> ManagedTask:
        """统一更新任务状态，并同步内存注册表。"""
        updated = self.store.update_task_status(task_id, status)
        if status is TaskStatus.ACTIVE:
            self.active_tasks[task_id] = updated
        else:
            self.active_tasks.pop(task_id, None)
        return updated

    def list_execution_records(
        self,
        *,
        task_id: str | None = None,
        token_id: str | None = None,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        """列出执行记录。"""
        return self.store.list_execution_records(
            task_id=task_id,
            token_id=token_id,
            limit=limit,
        )

    def list_execution_attempts(
        self,
        *,
        task_id: str | None = None,
        statuses: tuple[ExecutionAttemptStatus, ...] | None = None,
        limit: int = 1000,
    ) -> list[ExecutionAttempt]:
        """列出执行意图。"""
        return self.store.list_execution_attempts(task_id=task_id, statuses=statuses, limit=limit)

    def upsert_execution_attempt(self, attempt: ExecutionAttempt) -> ExecutionAttempt:
        """写入执行意图。"""
        self.get_task(attempt.task_id)
        return self.store.upsert_execution_attempt(attempt)

    def get_latest_execution_attempt_by_order_id(self, order_id: str) -> ExecutionAttempt | None:
        """按 order_id 读取最新执行意图。"""
        return self.store.get_latest_execution_attempt_by_order_id(order_id)

    def append_execution_record(self, record: ExecutionRecord) -> ExecutionRecord:
        """写入执行记录。"""
        self.get_task(record.task_id)
        return self.store.append_execution_record(record)

    def load_rule_states(self, task_id: str) -> dict[str, RuleState]:
        """读取任务当前运行态。"""
        self.get_task(task_id)
        return self.store.load_rule_states(task_id)

    def replace_rule_states(self, task_id: str, states: dict[str, RuleState]) -> None:
        """覆盖任务当前运行态。"""
        self.get_task(task_id)
        self.store.replace_rule_states(task_id, states)

    def persist_runtime_changes(
        self,
        task_id: str,
        *,
        states: dict[str, RuleState] | None = None,
        records: tuple[ExecutionRecord, ...] = (),
        attempts: tuple[ExecutionAttempt, ...] = (),
        task_status: TaskStatus | None = None,
    ) -> ManagedTask:
        """以单事务方式落库运行时相关变更，并同步内存注册表。"""
        self.get_task(task_id)
        updated = self.store.persist_task_runtime_changes(
            task_id,
            states=states,
            records=records,
            attempts=attempts,
            task_status=task_status,
        )
        if updated.status is TaskStatus.ACTIVE:
            self.active_tasks[task_id] = updated
        else:
            self.active_tasks.pop(task_id, None)
        return updated

    def acquire_runtime_lease(self, lease_key: str, owner_id: str, ttl_seconds: int) -> RuntimeLease:
        """尝试获取运行时租约。"""
        lease = self.store.acquire_runtime_lease(lease_key, owner_id, ttl_seconds)
        if lease is None:
            raise RuntimeLeaseConflictError(
                f"runtime lease {lease_key} is already held by another instance"
            )
        return lease

    def renew_runtime_lease(self, lease_key: str, owner_id: str, ttl_seconds: int) -> RuntimeLease | None:
        """续租当前运行时租约。"""
        return self.store.renew_runtime_lease(lease_key, owner_id, ttl_seconds)

    def release_runtime_lease(self, lease_key: str, owner_id: str) -> None:
        """释放运行时租约。"""
        self.store.release_runtime_lease(lease_key, owner_id)

    def get_runtime_lease(self, lease_key: str) -> RuntimeLease | None:
        """读取当前运行时租约。"""
        return self.store.get_runtime_lease(lease_key)

    def _ensure_token_available(self, token_id: str, excluding_task_id: str | None = None) -> None:
        for active_task in self.active_tasks.values():
            if active_task.task_id == excluding_task_id:
                continue
            if active_task.token_id == token_id:
                raise TaskConflictError(
                    f"token {token_id} already has an active task: {active_task.task_id}"
                )