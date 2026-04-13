from __future__ import annotations

"""SQLite 持久化实现，负责任务、规则运行态和执行审计。"""

import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from poly_shield.backend.models import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionRecord,
    ManagedTask,
    PersistedRuleState,
    RuntimeLease,
    TaskStatus,
    new_identifier,
    utc_now,
)
from poly_shield.rules import ExitRule, RuleKind, RuleState


def _to_iso(timestamp: datetime) -> str:
    return timestamp.isoformat()


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _to_decimal(value: str | None) -> Decimal | None:
    if value in {None, ""}:
        return None
    return Decimal(value)


class SQLiteTaskStore:
    """基于 sqlite3 的任务仓储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
        task_id: str | None = None,
    ) -> ManagedTask:
        """写入一条新任务及其规则定义。"""
        created_at = utc_now()
        assigned_task_id = task_id or new_identifier()
        task = ManagedTask(
            task_id=assigned_task_id,
            token_id=token_id,
            rules=rules,
            status=status,
            dry_run=dry_run,
            slippage_bps=slippage_bps,
            position_size=position_size,
            average_cost=average_cost,
            created_at=created_at,
            updated_at=created_at,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id,
                    token_id,
                    status,
                    dry_run,
                    slippage_bps,
                    position_size,
                    average_cost,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.token_id,
                    task.status.value,
                    int(task.dry_run),
                    str(task.slippage_bps),
                    None if task.position_size is None else str(
                        task.position_size),
                    None if task.average_cost is None else str(
                        task.average_cost),
                    _to_iso(task.created_at),
                    _to_iso(task.updated_at),
                ),
            )
            connection.executemany(
                """
                INSERT INTO task_rules (
                    task_id,
                    rule_name,
                    rule_kind,
                    sell_ratio,
                    trigger_price,
                    drawdown_ratio,
                    label
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        task.task_id,
                        rule.name,
                        rule.kind.value,
                        str(rule.sell_ratio),
                        None if rule.trigger_price is None else str(
                            rule.trigger_price),
                        None if rule.drawdown_ratio is None else str(
                            rule.drawdown_ratio),
                        rule.label,
                    )
                    for rule in task.rules
                ],
            )
        return task

    def upsert_execution_attempt(self, attempt: ExecutionAttempt) -> ExecutionAttempt:
        """插入或更新执行意图。"""
        with self._connect() as connection:
            self._upsert_execution_attempt(connection, attempt)
        return attempt

    def list_execution_attempts(
        self,
        *,
        task_id: str | None = None,
        statuses: tuple[ExecutionAttemptStatus, ...] | None = None,
        limit: int = 1000,
    ) -> list[ExecutionAttempt]:
        """查询执行意图。"""
        query = """
            SELECT attempt_id, task_id, token_id, rule_name, status, requested_size,
                   trigger_price, best_bid, best_ask, filled_size, order_id,
                   market_id, message, created_at, updated_at
            FROM execution_attempts
        """
        parameters: list[object] = []
        clauses: list[str] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if statuses:
            clauses.append("status IN ({})".format(", ".join("?" for _ in statuses)))
            parameters.extend(status.value for status in statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._build_execution_attempt(row) for row in rows]

    def get_latest_execution_attempt_by_order_id(self, order_id: str) -> ExecutionAttempt | None:
        """按 order_id 读取最新一条执行意图。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_id, task_id, token_id, rule_name, status, requested_size,
                       trigger_price, best_bid, best_ask, filled_size, order_id,
                       market_id, message, created_at, updated_at
                FROM execution_attempts
                WHERE order_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return self._build_execution_attempt(row)

    def persist_task_runtime_changes(
        self,
        task_id: str,
        *,
        states: dict[str, RuleState] | None = None,
        records: tuple[ExecutionRecord, ...] = (),
        attempts: tuple[ExecutionAttempt, ...] = (),
        task_status: TaskStatus | None = None,
    ) -> ManagedTask:
        """在单个事务内落库任务运行态、执行记录、意图和状态。"""
        updated_at = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT task_id, token_id, status, dry_run, slippage_bps,
                       position_size, average_cost, created_at, updated_at
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task_id: {task_id}")
            target_status = task_status.value if task_status is not None else row["status"]
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (target_status, _to_iso(updated_at), task_id),
            )
            if states is not None:
                connection.execute("DELETE FROM task_states WHERE task_id = ?", (task_id,))
                connection.executemany(
                    """
                    INSERT INTO task_states (
                        task_id,
                        rule_name,
                        locked_size,
                        sold_size,
                        trigger_bid,
                        peak_bid,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            task_id,
                            persisted_state.rule_name,
                            None if persisted_state.locked_size is None else str(persisted_state.locked_size),
                            str(persisted_state.sold_size),
                            None if persisted_state.trigger_bid is None else str(persisted_state.trigger_bid),
                            None if persisted_state.peak_bid is None else str(persisted_state.peak_bid),
                            _to_iso(persisted_state.updated_at),
                        )
                        for persisted_state in [
                            PersistedRuleState.from_rule_state(rule_name, state)
                            for rule_name, state in states.items()
                        ]
                    ],
                )
            for attempt in attempts:
                self._upsert_execution_attempt(connection, attempt)
            if records:
                connection.executemany(
                    """
                    INSERT INTO execution_records (
                        record_id,
                        task_id,
                        token_id,
                        rule_name,
                        event_type,
                        status,
                        order_id,
                        market_id,
                        event_price,
                        best_bid,
                        best_ask,
                        trigger_price,
                        requested_size,
                        filled_size,
                        message,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            record.record_id,
                            record.task_id,
                            record.token_id,
                            record.rule_name,
                            record.event_type,
                            record.status,
                            record.order_id,
                            record.market_id,
                            str(record.event_price),
                            str(record.best_bid),
                            str(record.best_ask),
                            str(record.trigger_price),
                            str(record.requested_size),
                            str(record.filled_size),
                            record.message,
                            _to_iso(record.created_at),
                        )
                        for record in records
                    ],
                )
            refreshed = connection.execute(
                """
                SELECT task_id, token_id, status, dry_run, slippage_bps,
                       position_size, average_cost, created_at, updated_at
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            assert refreshed is not None
            return self._build_task(connection, refreshed)

    def acquire_runtime_lease(self, lease_key: str, owner_id: str, ttl_seconds: int) -> RuntimeLease | None:
        """尝试获取运行时租约；如果已被其它存活实例占用，则返回 None。"""
        now = utc_now()
        expires_at = now.timestamp() + ttl_seconds
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lease_key, owner_id, expires_at, updated_at FROM runtime_leases WHERE lease_key = ?",
                (lease_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO runtime_leases (lease_key, owner_id, expires_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (lease_key, owner_id, _to_iso(datetime.fromtimestamp(expires_at, now.tzinfo)), _to_iso(now)),
                )
            else:
                current_expires_at = _from_iso(row["expires_at"])
                if row["owner_id"] != owner_id and current_expires_at > now:
                    return None
                connection.execute(
                    """
                    UPDATE runtime_leases
                    SET owner_id = ?, expires_at = ?, updated_at = ?
                    WHERE lease_key = ?
                    """,
                    (owner_id, _to_iso(datetime.fromtimestamp(expires_at, now.tzinfo)), _to_iso(now), lease_key),
                )
            return RuntimeLease(
                lease_key=lease_key,
                owner_id=owner_id,
                expires_at=datetime.fromtimestamp(expires_at, now.tzinfo),
                updated_at=now,
            )

    def renew_runtime_lease(self, lease_key: str, owner_id: str, ttl_seconds: int) -> RuntimeLease | None:
        """续租；如果租约已不属于当前实例，则返回 None。"""
        now = utc_now()
        expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, now.tzinfo)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_leases
                SET expires_at = ?, updated_at = ?
                WHERE lease_key = ? AND owner_id = ?
                """,
                (_to_iso(expires_at), _to_iso(now), lease_key, owner_id),
            )
            if cursor.rowcount != 1:
                return None
        return RuntimeLease(
            lease_key=lease_key,
            owner_id=owner_id,
            expires_at=expires_at,
            updated_at=now,
        )

    def release_runtime_lease(self, lease_key: str, owner_id: str) -> None:
        """释放运行时租约。"""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM runtime_leases WHERE lease_key = ? AND owner_id = ?",
                (lease_key, owner_id),
            )

    def get_runtime_lease(self, lease_key: str) -> RuntimeLease | None:
        """读取当前运行时租约。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lease_key, owner_id, expires_at, updated_at FROM runtime_leases WHERE lease_key = ?",
                (lease_key,),
            ).fetchone()
        if row is None:
            return None
        return RuntimeLease(
            lease_key=row["lease_key"],
            owner_id=row["owner_id"],
            expires_at=_from_iso(row["expires_at"]),
            updated_at=_from_iso(row["updated_at"]),
        )

    def get_task(self, task_id: str) -> ManagedTask | None:
        """按主键读取单个任务。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT task_id, token_id, status, dry_run, slippage_bps,
                       position_size, average_cost, created_at, updated_at
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return self._build_task(connection, row)

    def list_tasks(self, *, status: TaskStatus | None = None, include_deleted: bool = False) -> list[ManagedTask]:
        """查询任务列表。"""
        query = """
            SELECT task_id, token_id, status, dry_run, slippage_bps,
                   position_size, average_cost, created_at, updated_at
            FROM tasks
        """
        parameters: list[str] = []
        clauses: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status.value)
        elif not include_deleted:
            clauses.append("status != ?")
            parameters.append(TaskStatus.DELETED.value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._build_task(connection, row) for row in rows]

    def update_task_status(self, task_id: str, status: TaskStatus) -> ManagedTask:
        """更新任务状态。"""
        updated_at = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (status.value, _to_iso(updated_at), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown task_id: {task_id}")
        task = self.get_task(task_id)
        assert task is not None
        return task

    def replace_rule_states(self, task_id: str, states: dict[str, RuleState]) -> None:
        """整组覆盖任务的规则运行态。"""
        if self.get_task(task_id) is None:
            raise KeyError(f"unknown task_id: {task_id}")
        persisted_states = [
            PersistedRuleState.from_rule_state(rule_name, state)
            for rule_name, state in states.items()
        ]
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM task_states WHERE task_id = ?", (task_id,))
            connection.executemany(
                """
                INSERT INTO task_states (
                    task_id,
                    rule_name,
                    locked_size,
                    sold_size,
                    trigger_bid,
                    peak_bid,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        task_id,
                        persisted_state.rule_name,
                        None if persisted_state.locked_size is None else str(
                            persisted_state.locked_size),
                        str(persisted_state.sold_size),
                        None if persisted_state.trigger_bid is None else str(
                            persisted_state.trigger_bid),
                        None if persisted_state.peak_bid is None else str(
                            persisted_state.peak_bid),
                        _to_iso(persisted_state.updated_at),
                    )
                    for persisted_state in persisted_states
                ],
            )

    def load_rule_states(self, task_id: str) -> dict[str, RuleState]:
        """读取并恢复指定任务的规则运行态。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rule_name, locked_size, sold_size, trigger_bid, peak_bid, updated_at
                FROM task_states
                WHERE task_id = ?
                ORDER BY rule_name ASC
                """,
                (task_id,),
            ).fetchall()
        return {
            row["rule_name"]: PersistedRuleState(
                rule_name=row["rule_name"],
                locked_size=_to_decimal(row["locked_size"]),
                sold_size=Decimal(row["sold_size"]),
                trigger_bid=_to_decimal(row["trigger_bid"]),
                peak_bid=_to_decimal(row["peak_bid"]),
                updated_at=_from_iso(row["updated_at"]),
            ).to_rule_state()
            for row in rows
        }

    def append_execution_record(self, record: ExecutionRecord) -> ExecutionRecord:
        """保存单次执行审计记录。"""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_records (
                    record_id,
                    task_id,
                    token_id,
                    rule_name,
                    event_type,
                    status,
                    order_id,
                    market_id,
                    event_price,
                    best_bid,
                    best_ask,
                    trigger_price,
                    requested_size,
                    filled_size,
                    message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.task_id,
                    record.token_id,
                    record.rule_name,
                    record.event_type,
                    record.status,
                    record.order_id,
                    record.market_id,
                    str(record.event_price),
                    str(record.best_bid),
                    str(record.best_ask),
                    str(record.trigger_price),
                    str(record.requested_size),
                    str(record.filled_size),
                    record.message,
                    _to_iso(record.created_at),
                ),
            )
        return record

    def list_execution_records(self, *, task_id: str | None = None, limit: int = 100) -> list[ExecutionRecord]:
        """查询执行审计记录。"""
        query = """
             SELECT record_id, task_id, token_id, rule_name, event_type, status,
                 order_id, market_id, event_price, best_bid, best_ask,
                 trigger_price, requested_size, filled_size, message, created_at
            FROM execution_records
        """
        parameters: list[object] = []
        if task_id is not None:
            query += " WHERE task_id = ?"
            parameters.append(task_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            ExecutionRecord(
                record_id=row["record_id"],
                task_id=row["task_id"],
                token_id=row["token_id"],
                rule_name=row["rule_name"],
                event_type=row["event_type"],
                status=row["status"],
                order_id=row["order_id"],
                market_id=row["market_id"],
                event_price=Decimal(row["event_price"]),
                best_bid=Decimal(row["best_bid"]),
                best_ask=Decimal(row["best_ask"]),
                trigger_price=Decimal(row["trigger_price"]),
                requested_size=Decimal(row["requested_size"]),
                filled_size=Decimal(row["filled_size"]),
                message=row["message"],
                created_at=_from_iso(row["created_at"]),
            )
            for row in rows
        ]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    slippage_bps TEXT NOT NULL,
                    position_size TEXT,
                    average_cost TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_rules (
                    task_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    rule_kind TEXT NOT NULL,
                    sell_ratio TEXT NOT NULL,
                    trigger_price TEXT,
                    drawdown_ratio TEXT,
                    label TEXT,
                    PRIMARY KEY (task_id, rule_name),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS task_states (
                    task_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    locked_size TEXT,
                    sold_size TEXT NOT NULL,
                    trigger_bid TEXT,
                    peak_bid TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, rule_name),
                    FOREIGN KEY (task_id, rule_name) REFERENCES task_rules(task_id, rule_name) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS execution_records (
                    record_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    market_id TEXT,
                    event_price TEXT NOT NULL,
                    best_bid TEXT NOT NULL,
                    best_ask TEXT NOT NULL,
                    trigger_price TEXT NOT NULL,
                    requested_size TEXT NOT NULL,
                    filled_size TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS execution_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_size TEXT NOT NULL,
                    trigger_price TEXT NOT NULL,
                    best_bid TEXT NOT NULL,
                    best_ask TEXT NOT NULL,
                    filled_size TEXT NOT NULL,
                    order_id TEXT,
                    market_id TEXT,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runtime_leases (
                    lease_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_execution_records_task_created_at ON execution_records(task_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_execution_attempts_task_status ON execution_attempts(task_id, status, created_at DESC);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "position_size" not in columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN position_size TEXT")
            if "average_cost" not in columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN average_cost TEXT")
            record_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(execution_records)").fetchall()
            }
            if "event_type" not in record_columns:
                connection.execute(
                    "ALTER TABLE execution_records ADD COLUMN event_type TEXT NOT NULL DEFAULT 'rule'")
            if "order_id" not in record_columns:
                connection.execute(
                    "ALTER TABLE execution_records ADD COLUMN order_id TEXT")
            if "market_id" not in record_columns:
                connection.execute(
                    "ALTER TABLE execution_records ADD COLUMN market_id TEXT")
            if "event_price" not in record_columns:
                connection.execute(
                    "ALTER TABLE execution_records ADD COLUMN event_price TEXT NOT NULL DEFAULT '0'")

    def _upsert_execution_attempt(self, connection: sqlite3.Connection, attempt: ExecutionAttempt) -> None:
        connection.execute(
            """
            INSERT INTO execution_attempts (
                attempt_id,
                task_id,
                token_id,
                rule_name,
                status,
                requested_size,
                trigger_price,
                best_bid,
                best_ask,
                filled_size,
                order_id,
                market_id,
                message,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO UPDATE SET
                status = excluded.status,
                requested_size = excluded.requested_size,
                trigger_price = excluded.trigger_price,
                best_bid = excluded.best_bid,
                best_ask = excluded.best_ask,
                filled_size = excluded.filled_size,
                order_id = excluded.order_id,
                market_id = excluded.market_id,
                message = excluded.message,
                updated_at = excluded.updated_at
            """,
            (
                attempt.attempt_id,
                attempt.task_id,
                attempt.token_id,
                attempt.rule_name,
                attempt.status.value,
                str(attempt.requested_size),
                str(attempt.trigger_price),
                str(attempt.best_bid),
                str(attempt.best_ask),
                str(attempt.filled_size),
                attempt.order_id,
                attempt.market_id,
                attempt.message,
                _to_iso(attempt.created_at),
                _to_iso(attempt.updated_at),
            ),
        )

    def _build_execution_attempt(self, row: sqlite3.Row) -> ExecutionAttempt:
        return ExecutionAttempt(
            attempt_id=row["attempt_id"],
            task_id=row["task_id"],
            token_id=row["token_id"],
            rule_name=row["rule_name"],
            status=ExecutionAttemptStatus(row["status"]),
            requested_size=Decimal(row["requested_size"]),
            trigger_price=Decimal(row["trigger_price"]),
            best_bid=Decimal(row["best_bid"]),
            best_ask=Decimal(row["best_ask"]),
            filled_size=Decimal(row["filled_size"]),
            order_id=row["order_id"],
            market_id=row["market_id"],
            message=row["message"],
            created_at=_from_iso(row["created_at"]),
            updated_at=_from_iso(row["updated_at"]),
        )

    def _build_task(self, connection: sqlite3.Connection, row: sqlite3.Row) -> ManagedTask:
        rules = tuple(self._load_rules(connection, row["task_id"]))
        return ManagedTask(
            task_id=row["task_id"],
            token_id=row["token_id"],
            rules=rules,
            status=TaskStatus(row["status"]),
            dry_run=bool(row["dry_run"]),
            slippage_bps=Decimal(row["slippage_bps"]),
            position_size=_to_decimal(row["position_size"]),
            average_cost=_to_decimal(row["average_cost"]),
            created_at=_from_iso(row["created_at"]),
            updated_at=_from_iso(row["updated_at"]),
        )

    def _load_rules(self, connection: sqlite3.Connection, task_id: str) -> list[ExitRule]:
        rows = connection.execute(
            """
            SELECT rule_name, rule_kind, sell_ratio, trigger_price, drawdown_ratio, label
            FROM task_rules
            WHERE task_id = ?
            ORDER BY rowid ASC
            """,
            (task_id,),
        ).fetchall()
        return [
            ExitRule(
                kind=RuleKind(row["rule_kind"]),
                sell_ratio=Decimal(row["sell_ratio"]),
                trigger_price=_to_decimal(row["trigger_price"]),
                drawdown_ratio=_to_decimal(row["drawdown_ratio"]),
                label=row["label"],
            )
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        with closing(connection.cursor()) as cursor:
            cursor.execute("PRAGMA foreign_keys = ON")
        return connection
