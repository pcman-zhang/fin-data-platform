"""Runtime 元数据仓储（doc-20 §4 / §5）。

- :class:`MetaRepository`：协议（内存与 SQL 实现可互换）；
- :class:`InMemoryMetaRepository`：单测使用；
- :class:`SqlMetaRepository`：``meta.*`` 持久实现（SQLite / PostgreSQL 兼容）；
- 并发：领取是原子状态迁移（PG 用 ``SKIP LOCKED`` + advisory lock），
  同一 ``(dataset, scope)`` 同时最多一个 running。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import (
    Engine,
    RowMapping,
    and_,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection

from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.keys import job_key
from fin_data_platform.runtime.models import (
    CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    DependencyCondition,
    JobDef,
    JobDependency,
    JobIntent,
    JobRun,
    JobStatus,
    Watermark,
)
from fin_data_platform.runtime.schema import (
    job_defs,
    job_dependencies,
    job_runs,
    watermarks,
)

#: 重试退避基数（秒）：attempt 次失败后延迟 base * 2**(attempt-1)
RETRY_BACKOFF_SECONDS = 5


def _naive(value: datetime | None) -> datetime | None:
    """统一为 naive（PG timestamptz 返回 aware，本地写入用 naive）。"""
    if value is not None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _row_to_run(row: RowMapping) -> JobRun:
    return JobRun(
        run_id=int(row["run_id"]),
        job_key=str(row["job_key"]),
        job_id=str(row["job_id"]),
        kind=str(row["kind"]),
        dataset=str(row["dataset"]),
        scope=str(row["scope"]),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        priority=int(row["priority"]),
        scheduled_at=row["scheduled_at"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        version_dimension=row["version_dimension"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        rows_written=row["rows_written"],
        error=row["error"],
        request_id=row["request_id"],
        worker=row["worker"],
    )


class MetaRepository(Protocol):
    """元数据仓储协议。"""

    def sync_defs(self, defs: Sequence[JobDef]) -> None: ...

    def sync_dependencies(self, deps: Sequence[JobDependency]) -> None: ...

    def list_defs(self) -> list[JobDef]: ...

    def list_dependencies(self) -> list[JobDependency]: ...

    def create_run(
        self, intent: JobIntent, *, request_id: str | None = None
    ) -> JobRun | None: ...

    def claim_next(self, *, worker: str, now: datetime | None = None) -> JobRun | None: ...

    def succeed(self, run_id: int, *, rows_written: int = 0) -> JobRun: ...

    def fail(self, run_id: int, *, error: str) -> JobRun: ...

    def interrupt(self, run_id: int, *, reason: str) -> JobRun: ...

    def interrupt_running(self, *, worker_prefix: str) -> int: ...

    def get_run(self, run_id: int) -> JobRun | None: ...

    def list_runs(
        self,
        *,
        status: str | None = None,
        job_id: str | None = None,
        limit: int = 100,
    ) -> list[JobRun]: ...

    def find_run_by_request_id(self, request_id: str) -> JobRun | None: ...

    def find_run_by_job_key(self, job_key: str) -> JobRun | None:
        """按幂等键查既有运行（同键多条时返回最新一条；无则 ``None``）。"""
        ...

    def count_queued(self) -> int: ...

    def parents_ready(
        self,
        *,
        child_job: str,
        scope: str,
        window_start,
        window_end,
    ) -> bool: ...

    def get_watermark(self, dataset: str, scope: str = "") -> Watermark | None: ...

    def list_watermarks(self) -> list[Watermark]: ...

    def set_watermark(
        self, dataset: str, *, scope: str = "", watermark_time: datetime
    ) -> Watermark: ...

    def advance_watermark(
        self, dataset: str, *, scope: str = "", watermark_time: datetime
    ) -> Watermark: ...


class InMemoryMetaRepository:
    """内存实现（单测；线程安全）。"""

    def __init__(self) -> None:
        import threading

        self._lock = threading.RLock()
        self._defs: dict[str, JobDef] = {}
        self._deps: list[JobDependency] = []
        self._runs: dict[int, JobRun] = {}
        self._watermarks: dict[tuple[str, str], Watermark] = {}
        self._next_run_id = 1

    def sync_defs(self, defs: Sequence[JobDef]) -> None:
        with self._lock:
            for item in defs:
                self._defs[item.job_id] = item

    def sync_dependencies(self, deps: Sequence[JobDependency]) -> None:
        with self._lock:
            self._deps = list(deps)

    def list_defs(self) -> list[JobDef]:
        with self._lock:
            return list(self._defs.values())

    def list_dependencies(self) -> list[JobDependency]:
        with self._lock:
            return list(self._deps)

    def create_run(
        self, intent: JobIntent, *, request_id: str | None = None
    ) -> JobRun | None:
        key = job_key(
            kind=intent.kind,
            job_id=intent.job_id,
            scope=intent.scope,
            window_start=intent.window_start,
            window_end=intent.window_end,
            version_dimension=intent.version_dimension,
        )
        with self._lock:
            for run in self._runs.values():
                if run.job_key == key and run.status in (
                    CLAIMABLE_STATUSES
                    | {JobStatus.RUNNING.value, JobStatus.SUCCEEDED.value}
                ):
                    return None
            attempt = 1  # 新会话：每次 create 重新计尝试预算
            run = JobRun(
                run_id=self._next_run_id,
                job_key=key,
                job_id=intent.job_id,
                kind=intent.kind,
                dataset=intent.dataset,
                scope=intent.scope,
                status=JobStatus.QUEUED.value,
                attempt=attempt,
                max_attempts=intent.max_attempts,
                priority=intent.priority,
                scheduled_at=utcnow(),
                window_start=intent.window_start,
                window_end=intent.window_end,
                version_dimension=intent.version_dimension,
                request_id=request_id,
            )
            self._runs[run.run_id] = run
            self._next_run_id += 1
            return run

    def claim_next(
        self, *, worker: str, now: datetime | None = None
    ) -> JobRun | None:
        moment = now or utcnow()
        with self._lock:
            running_scopes = {
                (run.dataset, run.scope)
                for run in self._runs.values()
                if run.status == JobStatus.RUNNING.value
            }
            candidates = sorted(
                (
                    run
                    for run in self._runs.values()
                    if run.status in CLAIMABLE_STATUSES
                    and run.scheduled_at <= moment
                    and (run.dataset, run.scope) not in running_scopes
                ),
                key=lambda run: (run.priority, run.run_id),
            )
            if not candidates:
                return None
            run = candidates[0]
            claimed = JobRun(
                **{
                    **{f: getattr(run, f) for f in JobRun.__dataclass_fields__},
                    "status": JobStatus.RUNNING.value,
                    "started_at": moment,
                    "worker": worker,
                }
            )
            self._runs[run.run_id] = claimed
            return claimed

    def _finish(
        self,
        run_id: int,
        status: str,
        *,
        rows_written: int | None = None,
        error: str | None = None,
        attempt: int | None = None,
        scheduled_at: datetime | None = None,
    ) -> JobRun:
        with self._lock:
            run = self._runs[run_id]
            if run.status != JobStatus.RUNNING.value:
                return run  # 非运行态不可覆盖（停机超时回收后重放不得改写）
            finished = JobRun(
                **{
                    **{f: getattr(run, f) for f in JobRun.__dataclass_fields__},
                    "status": status,
                    "rows_written": rows_written,
                    "error": error,
                    "attempt": attempt if attempt is not None else run.attempt,
                    "scheduled_at": scheduled_at or run.scheduled_at,
                    "finished_at": None if status == JobStatus.RETRYING.value else utcnow(),
                }
            )
            self._runs[run_id] = finished
            return finished

    def succeed(self, run_id: int, *, rows_written: int = 0) -> JobRun:
        return self._finish(
            run_id, JobStatus.SUCCEEDED.value, rows_written=rows_written
        )

    def fail(self, run_id: int, *, error: str) -> JobRun:
        with self._lock:
            run = self._runs[run_id]
            if run.attempt < run.max_attempts:
                return self._finish(
                    run_id,
                    JobStatus.RETRYING.value,
                    error=error,
                    attempt=run.attempt + 1,
                    scheduled_at=utcnow()
                    + timedelta(seconds=RETRY_BACKOFF_SECONDS * (2 ** (run.attempt - 1))),
                )
            return self._finish(run_id, JobStatus.DEAD.value, error=error)

    def interrupt(self, run_id: int, *, reason: str) -> JobRun:
        return self._finish(run_id, JobStatus.INTERRUPTED.value, error=reason)

    def interrupt_running(self, *, worker_prefix: str) -> int:
        """把该 worker 前缀名下仍处于 running 的行标记为 interrupted（停机超时回收）。"""
        with self._lock:
            count = 0
            for run in list(self._runs.values()):
                if (
                    run.status == JobStatus.RUNNING.value
                    and (run.worker or "").startswith(worker_prefix)
                ):
                    self._finish(run.run_id, JobStatus.INTERRUPTED.value, error="停机超时")
                    count += 1
            return count

    def get_run(self, run_id: int) -> JobRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_runs(
        self,
        *,
        status: str | None = None,
        job_id: str | None = None,
        limit: int = 100,
    ) -> list[JobRun]:
        with self._lock:
            rows = [
                run
                for run in self._runs.values()
                if (status is None or run.status == status)
                and (job_id is None or run.job_id == job_id)
            ]
            return sorted(rows, key=lambda run: run.run_id, reverse=True)[:limit]

    def find_run_by_request_id(self, request_id: str) -> JobRun | None:
        with self._lock:
            matches = [
                run for run in self._runs.values() if run.request_id == request_id
            ]
        return min(matches, key=lambda run: run.run_id) if matches else None

    def find_run_by_job_key(self, job_key: str) -> JobRun | None:
        with self._lock:
            matches = [run for run in self._runs.values() if run.job_key == job_key]
        return max(matches, key=lambda run: run.run_id) if matches else None

    def count_queued(self) -> int:
        with self._lock:
            return sum(
                1
                for run in self._runs.values()
                if run.status in CLAIMABLE_STATUSES
            )

    def parents_ready(
        self,
        *,
        child_job: str,
        scope: str,
        window_start,
        window_end,
    ) -> bool:
        with self._lock:
            parents = [
                dep
                for dep in self._deps
                if dep.child_job == child_job
            ]
            for dep in parents:
                matched = [
                    run
                    for run in self._runs.values()
                    if run.job_id == dep.parent_job
                    and run.scope == scope
                    and run.window_start == window_start
                    and run.window_end == window_end
                ]
                if not matched:
                    return False
                if dep.condition == DependencyCondition.ALWAYS.value:
                    continue
                if dep.condition == DependencyCondition.ON_COMPLETE.value:
                    if not any(run.status in TERMINAL_STATUSES for run in matched):
                        return False
                elif not any(
                    run.status == JobStatus.SUCCEEDED.value for run in matched
                ):
                    return False
            return True

    def get_watermark(self, dataset: str, scope: str = "") -> Watermark | None:
        with self._lock:
            return self._watermarks.get((dataset, scope))

    def list_watermarks(self) -> list[Watermark]:
        with self._lock:
            return sorted(
                self._watermarks.values(), key=lambda mark: (mark.dataset, mark.scope)
            )

    def set_watermark(
        self, dataset: str, *, scope: str = "", watermark_time: datetime
    ) -> Watermark:
        with self._lock:
            mark = Watermark(dataset=dataset, scope=scope, watermark_time=watermark_time)
            self._watermarks[(dataset, scope)] = mark
            return mark

    def advance_watermark(
        self, dataset: str, *, scope: str = "", watermark_time: datetime
    ) -> Watermark:
        """单调推进水位（不回退；乱序/补数窗口不得使覆盖范围倒退）。"""
        with self._lock:
            current = self._watermarks.get((dataset, scope))
            if (
                current is not None
                and current.watermark_time is not None
                and current.watermark_time >= watermark_time
            ):
                return current
            return self.set_watermark(
                dataset, scope=scope, watermark_time=watermark_time
            )


class SqlMetaRepository:
    """``meta.*`` SQL 实现（State 权威）。

    并发说明：多线程/多进程 Runtime 请使用 PostgreSQL（advisory lock + ``SKIP
    LOCKED``）；SQLite 仅用于单线程测试（共享内存连接不支持并发访问）。
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------ 声明镜像
    def sync_defs(self, defs: Sequence[JobDef]) -> None:
        now = utcnow()
        with self._engine.begin() as connection:
            # 镜像语义：注册表为全集，删除已移除的任务定义
            connection.execute(job_defs.delete())
            for item in defs:
                connection.execute(
                    insert(job_defs).values(
                        job_id=item.job_id,
                        kind=item.kind,
                        dataset=item.dataset,
                        schedule=item.schedule,
                        priority=item.priority,
                        max_attempts=item.max_attempts,
                        enabled=item.enabled,
                        updated_at=now,
                    )
                )

    def sync_dependencies(self, deps: Sequence[JobDependency]) -> None:
        now = utcnow()
        with self._engine.begin() as connection:
            # 镜像语义：整体替换（与内存实现一致；依赖被清空时旧行必须删除）
            connection.execute(job_dependencies.delete())
            for dep in deps:
                connection.execute(
                    insert(job_dependencies).values(
                        parent_job=dep.parent_job,
                        child_job=dep.child_job,
                        condition=dep.condition,
                        updated_at=now,
                    )
                )

    def list_defs(self) -> list[JobDef]:
        with self._engine.begin() as connection:
            rows = connection.execute(select(job_defs)).mappings().all()
        return [
            JobDef(
                job_id=str(row["job_id"]),
                kind=str(row["kind"]),
                dataset=str(row["dataset"]),
                schedule=row["schedule"],
                priority=int(row["priority"]),
                max_attempts=int(row["max_attempts"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def list_dependencies(self) -> list[JobDependency]:
        with self._engine.begin() as connection:
            rows = connection.execute(select(job_dependencies)).mappings().all()
        return [
            JobDependency(
                parent_job=str(row["parent_job"]),
                child_job=str(row["child_job"]),
                condition=str(row["condition"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------ 运行
    def create_run(
        self, intent: JobIntent, *, request_id: str | None = None
    ) -> JobRun | None:
        key = job_key(
            kind=intent.kind,
            job_id=intent.job_id,
            scope=intent.scope,
            window_start=intent.window_start,
            window_end=intent.window_end,
            version_dimension=intent.version_dimension,
        )
        now = utcnow()
        with self._engine.begin() as connection:
            self._acquire_scope_lock(connection, f"{intent.dataset}:{intent.scope}")
            existing = connection.execute(
                select(job_runs.c.status)
                .where(job_runs.c.job_key == key)
            ).all()
            statuses = {str(row[0]) for row in existing}
            if statuses & (
                CLAIMABLE_STATUSES
                | {JobStatus.RUNNING.value, JobStatus.SUCCEEDED.value}
            ):
                return None
            result = connection.execute(
                insert(job_runs)
                .values(
                    job_key=key,
                    job_id=intent.job_id,
                    kind=intent.kind,
                    dataset=intent.dataset,
                    scope=intent.scope,
                    window_start=intent.window_start,
                    window_end=intent.window_end,
                    version_dimension=intent.version_dimension,
                    status=JobStatus.QUEUED.value,
                    attempt=1,  # 新会话：每次 create 重新计尝试预算
                    max_attempts=intent.max_attempts,
                    priority=intent.priority,
                    scheduled_at=now,
                    request_id=request_id,
                    updated_at=now,
                )
                .returning(job_runs.c.run_id)
            )
            run_id = int(result.scalar_one())
            row = connection.execute(
                select(job_runs).where(job_runs.c.run_id == run_id)
            ).mappings().one()
        return _row_to_run(row)

    def claim_next(
        self, *, worker: str, now: datetime | None = None
    ) -> JobRun | None:
        moment = now or utcnow()
        with self._engine.begin() as connection:
            running = job_runs.alias("running")
            scope_busy = (
                select(running.c.run_id)
                .where(
                    running.c.dataset == job_runs.c.dataset,
                    running.c.scope == job_runs.c.scope,
                    running.c.status == JobStatus.RUNNING.value,
                )
                .exists()
            )
            statement = (
                select(
                    job_runs.c.run_id, job_runs.c.dataset, job_runs.c.scope
                )
                .where(
                    job_runs.c.status.in_(CLAIMABLE_STATUSES),
                    job_runs.c.scheduled_at <= moment,
                    ~scope_busy,
                )
                .order_by(job_runs.c.priority, job_runs.c.run_id)
                .limit(1)
            )
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            candidate = connection.execute(statement).one_or_none()
            if candidate is None:
                return None
            run_id, dataset, scope = int(candidate[0]), str(candidate[1]), str(candidate[2])
            # advisory 锁粒度 = (dataset, scope)：与写入端互斥（doc-20 §4.5）；
            # 行级 SKIP LOCKED 保证多 worker 不重复领取
            self._acquire_scope_lock(connection, f"{dataset}:{scope}")
            connection.execute(
                update(job_runs)
                .where(
                    job_runs.c.run_id == run_id,
                    job_runs.c.status.in_(CLAIMABLE_STATUSES),
                )
                .values(
                    status=JobStatus.RUNNING.value,
                    worker=worker,
                    started_at=moment,
                    updated_at=moment,
                )
            )
            row = connection.execute(
                select(job_runs).where(job_runs.c.run_id == run_id)
            ).mappings().one()
        return _row_to_run(row)

    def _finish(
        self,
        run_id: int,
        status: str,
        *,
        rows_written: int | None = None,
        error: str | None = None,
        attempt: int | None = None,
        scheduled_at: datetime | None = None,
        finished: bool = True,
    ) -> JobRun:
        now = utcnow()
        values: dict[str, Any] = {
            "status": status,
            "updated_at": now,
            "rows_written": rows_written,
            "error": error,
            "finished_at": now if finished else None,
        }
        if attempt is not None:
            values["attempt"] = attempt
        if scheduled_at is not None:
            values["scheduled_at"] = scheduled_at
        with self._engine.begin() as connection:
            connection.execute(
                update(job_runs)
                .where(
                    job_runs.c.run_id == run_id,
                    job_runs.c.status == JobStatus.RUNNING.value,
                )
                .values(values)
            )
            row = connection.execute(
                select(job_runs).where(job_runs.c.run_id == run_id)
            ).mappings().one()
        return _row_to_run(row)

    def succeed(self, run_id: int, *, rows_written: int = 0) -> JobRun:
        return self._finish(
            run_id,
            JobStatus.SUCCEEDED.value,
            rows_written=rows_written,
            error=None,
        )

    def fail(self, run_id: int, *, error: str) -> JobRun:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"run 不存在: {run_id}")
        if run.attempt < run.max_attempts:
            delay = RETRY_BACKOFF_SECONDS * (2 ** (run.attempt - 1))
            return self._finish(
                run_id,
                JobStatus.RETRYING.value,
                error=error,
                attempt=run.attempt + 1,
                scheduled_at=utcnow() + timedelta(seconds=delay),
                finished=False,
            )
        return self._finish(run_id, JobStatus.DEAD.value, error=error)

    def interrupt(self, run_id: int, *, reason: str) -> JobRun:
        return self._finish(run_id, JobStatus.INTERRUPTED.value, error=reason)

    def interrupt_running(self, *, worker_prefix: str) -> int:
        """把该 worker 前缀名下仍处于 running 的行标记为 interrupted（停机超时回收）。"""
        now = utcnow()
        with self._engine.begin() as connection:
            result = connection.execute(
                update(job_runs)
                .where(
                    job_runs.c.status == JobStatus.RUNNING.value,
                    job_runs.c.worker.like(f"{worker_prefix}%"),
                )
                .values(
                    status=JobStatus.INTERRUPTED.value,
                    error="停机超时",
                    finished_at=now,
                    updated_at=now,
                )
            )
            return int(result.rowcount or 0)

    def get_run(self, run_id: int) -> JobRun | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                select(job_runs).where(job_runs.c.run_id == run_id)
            ).mappings().one_or_none()
        return _row_to_run(row) if row is not None else None

    def list_runs(
        self,
        *,
        status: str | None = None,
        job_id: str | None = None,
        limit: int = 100,
    ) -> list[JobRun]:
        statement = select(job_runs).order_by(job_runs.c.run_id.desc()).limit(limit)
        if status is not None:
            statement = statement.where(job_runs.c.status == status)
        if job_id is not None:
            statement = statement.where(job_runs.c.job_id == job_id)
        with self._engine.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_row_to_run(row) for row in rows]

    def find_run_by_request_id(self, request_id: str) -> JobRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(job_runs)
                    .where(job_runs.c.request_id == request_id)
                    .order_by(job_runs.c.run_id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return _row_to_run(row) if row is not None else None

    def find_run_by_job_key(self, job_key: str) -> JobRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(job_runs)
                    .where(job_runs.c.job_key == job_key)
                    .order_by(job_runs.c.run_id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return _row_to_run(row) if row is not None else None

    def count_queued(self) -> int:
        with self._engine.begin() as connection:
            return int(
                connection.execute(
                    select(func.count())
                    .select_from(job_runs)
                    .where(job_runs.c.status.in_(CLAIMABLE_STATUSES))
                ).scalar_one()
            )

    def parents_ready(
        self,
        *,
        child_job: str,
        scope: str,
        window_start,
        window_end,
    ) -> bool:
        with self._engine.begin() as connection:
            deps = connection.execute(
                select(job_dependencies).where(
                    job_dependencies.c.child_job == child_job
                )
            ).mappings().all()
            for dep in deps:
                condition = str(dep["condition"])
                statement = select(job_runs.c.status).where(
                    job_runs.c.job_id == str(dep["parent_job"]),
                    job_runs.c.scope == scope,
                    job_runs.c.window_start == window_start,
                    job_runs.c.window_end == window_end,
                )
                statuses = {
                    str(row[0])
                    for row in connection.execute(statement).all()
                }
                if condition == DependencyCondition.ALWAYS.value:
                    if not statuses:
                        return False
                    continue
                if condition == DependencyCondition.ON_COMPLETE.value:
                    if not statuses & TERMINAL_STATUSES:
                        return False
                    continue
                if JobStatus.SUCCEEDED.value not in statuses:
                    return False
            return True

    # ------------------------------------------------------------ 水位
    def get_watermark(self, dataset: str, scope: str = "") -> Watermark | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                select(watermarks).where(
                    and_(
                        watermarks.c.dataset == dataset,
                        watermarks.c.scope == scope,
                    )
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return Watermark(
            dataset=str(row["dataset"]),
            scope=str(row["scope"]),
            watermark_time=row["watermark_time"],
        )

    def list_watermarks(self) -> list[Watermark]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(watermarks).order_by(
                        watermarks.c.dataset, watermarks.c.scope
                    )
                )
                .mappings()
                .all()
            )
        return [
            Watermark(
                dataset=str(row["dataset"]),
                scope=str(row["scope"]),
                watermark_time=row["watermark_time"],
            )
            for row in rows
        ]

    def set_watermark(
        self, dataset: str, *, scope: str = "", watermark_time: datetime
    ) -> Watermark:
        now = utcnow()
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(watermarks.c.dataset).where(
                    and_(
                        watermarks.c.dataset == dataset,
                        watermarks.c.scope == scope,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    insert(watermarks).values(
                        dataset=dataset,
                        scope=scope,
                        watermark_time=watermark_time,
                        updated_at=now,
                    )
                )
            else:
                connection.execute(
                    update(watermarks)
                    .where(
                        and_(
                            watermarks.c.dataset == dataset,
                            watermarks.c.scope == scope,
                        )
                    )
                    .values(watermark_time=watermark_time, updated_at=now)
                )
        return Watermark(dataset=dataset, scope=scope, watermark_time=watermark_time)

    def advance_watermark(
        self, dataset: str, *, scope: str = "", watermark_time: datetime
    ) -> Watermark:
        """单调推进水位（仅当新值更大时写入）。"""
        now = utcnow()
        with self._engine.begin() as connection:
            current = connection.execute(
                select(watermarks.c.watermark_time).where(
                    and_(
                        watermarks.c.dataset == dataset,
                        watermarks.c.scope == scope,
                    )
                )
            ).scalar_one_or_none()
            current_naive = _naive(current)
            if current_naive is not None and current_naive >= watermark_time:
                return Watermark(
                    dataset=dataset,
                    scope=scope,
                    watermark_time=current,
                )
            if current is None:
                connection.execute(
                    insert(watermarks).values(
                        dataset=dataset,
                        scope=scope,
                        watermark_time=watermark_time,
                        updated_at=now,
                    )
                )
            else:
                connection.execute(
                    update(watermarks)
                    .where(
                        and_(
                            watermarks.c.dataset == dataset,
                            watermarks.c.scope == scope,
                        )
                    )
                    .values(watermark_time=watermark_time, updated_at=now)
                )
        return Watermark(dataset=dataset, scope=scope, watermark_time=watermark_time)

    # ------------------------------------------------------------ 锁
    def _acquire_scope_lock(self, connection: Connection, key: str) -> None:
        """PG advisory 事务锁（非 PG 方言为 no-op；状态迁移本身是原子的）。"""
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"fdp_runtime:{key}"},
            )
