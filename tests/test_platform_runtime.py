"""FinDataRuntime 骨架（TASK-3.18 / doc-20）测试。

覆盖：幂等键版本维度、声明式注册校验、仓储状态机与并发、依赖门控、背压、
角色分层、就绪检查（fail fast）与入口参数。
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.runtime import (
    Dispatcher,
    InMemoryMetaRepository,
    JobDef,
    JobDependency,
    JobIntent,
    JobResult,
    JobStatus,
    RuntimeApp,
    RuntimeConfig,
    Scheduler,
    SqlMetaRepository,
    TaskRegistry,
    TaskSpec,
    WorkerPool,
    job_key,
    readiness,
)
from fin_data_platform.runtime.__main__ import main
from fin_data_platform.runtime.roles import (
    SUBMIT_BACKPRESSURE,
    SUBMIT_CREATED,
    SUBMIT_DEPENDENCY,
    SUBMIT_DUPLICATE,
)
from fin_data_platform.runtime.schema import metadata as meta_metadata
from fin_data_platform.storage.config import StorageConfig

DAY = date(2026, 9, 14)


def _intent(
    job_id: str = "job",
    *,
    kind: str = "sync",
    dataset: str = "cn_equity.demo",
    scope: str = "",
    window: date = DAY,
    **overrides: object,
) -> JobIntent:
    return JobIntent(
        kind=kind,
        job_id=job_id,
        dataset=dataset,
        scope=scope,
        window_start=window,
        window_end=window,
        **overrides,  # type: ignore[arg-type]
    )


def _registry() -> TaskRegistry:
    registry = TaskRegistry()
    registry.register(
        TaskSpec(
            job_id="parent",
            kind="sync",
            dataset="cn_equity.parent",
            executor=lambda ctx: JobResult(rows_written=1),
        )
    )
    registry.register(
        TaskSpec(
            job_id="child",
            kind="derive",
            dataset="cn_equity.child",
            executor=lambda ctx: JobResult(rows_written=2),
            dependencies=("parent",),
            version_provider=lambda: "ma20_v1",
        )
    )
    return registry


def _config(role: str = "all") -> RuntimeConfig:
    return RuntimeConfig(
        storage=StorageConfig(write_dsn="sqlite://"),
        role=role,
        worker_count=1,
        tick_interval=0.01,
        worker_interval=0.01,
        max_queued=10,
    )


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS meta"))
    meta_metadata.create_all(engine)
    return engine


# ------------------------------------------------------------------ 幂等键
def test_job_key_stable_and_version_dimension_rules() -> None:
    first = job_key(kind="sync", job_id="daily", window_start=DAY, window_end=DAY)
    assert first == job_key(kind="sync", job_id="daily", window_start=DAY, window_end=DAY)
    other = job_key(
        kind="sync", job_id="daily", window_start=date(2026, 9, 15), window_end=date(2026, 9, 15)
    )
    assert first != other

    with pytest.raises(ValueError, match="algorithm_id"):
        job_key(kind="derive", job_id="ma20")
    with pytest.raises(ValueError, match="不应有版本维度"):
        job_key(kind="sync", job_id="daily", version_dimension="v1")

    v1 = job_key(kind="derive", job_id="ma20", version_dimension="ma20_v1")
    v2 = job_key(kind="derive", job_id="ma20", version_dimension="ma20_v2")
    assert v1 != v2


# ------------------------------------------------------------------ 注册
def test_registry_validation() -> None:
    registry = _registry()
    assert registry.validate() == []

    missing_provider = TaskRegistry()
    missing_provider.register(
        TaskSpec(job_id="a", kind="derive", dataset="d", executor=lambda ctx: JobResult())
    )
    assert any("version_provider" in error for error in missing_provider.validate())

    missing_dep = TaskRegistry()
    missing_dep.register(
        TaskSpec(
            job_id="a",
            kind="sync",
            dataset="d",
            executor=lambda ctx: JobResult(),
            dependencies=("missing",),
        )
    )
    assert any("依赖任务未注册" in error for error in missing_dep.validate())

    cyclic = TaskRegistry()
    cyclic.register(
        TaskSpec(
            job_id="a",
            kind="sync",
            dataset="d1",
            executor=lambda ctx: JobResult(),
            dependencies=("b",),
        )
    )
    cyclic.register(
        TaskSpec(
            job_id="b",
            kind="sync",
            dataset="d2",
            executor=lambda ctx: JobResult(),
            dependencies=("a",),
        )
    )
    assert any("成环" in error for error in cyclic.validate())

    with pytest.raises(ValueError, match="重复注册"):
        cyclic.register(
            TaskSpec(job_id="a", kind="sync", dataset="d", executor=lambda ctx: JobResult())
        )


# ------------------------------------------------------------------ 内存仓储
def test_inmemory_create_is_idempotent_and_claimable() -> None:
    repo = InMemoryMetaRepository()
    assert repo.create_run(_intent()) is not None
    assert repo.create_run(_intent()) is None  # 同幂等键重复提交安全

    run = repo.claim_next(worker="w")
    assert run is not None and run.status == JobStatus.RUNNING.value
    assert repo.claim_next(worker="w") is None  # 无其他可领取

    done = repo.succeed(run.run_id, rows_written=7)
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.rows_written == 7
    # 成功后同键不再入队
    assert repo.create_run(_intent()) is None


def test_inmemory_retry_backoff_and_dead() -> None:
    repo = InMemoryMetaRepository()
    repo.create_run(_intent(max_attempts=2))
    first = repo.claim_next(worker="w")
    assert first is not None
    retrying = repo.fail(first.run_id, error="boom")
    assert retrying.status == JobStatus.RETRYING.value
    assert retrying.attempt == 2
    assert retrying.scheduled_at > datetime(2026, 1, 1)
    assert repo.claim_next(worker="w") is None  # 退避未到

    later = retrying.scheduled_at + timedelta(seconds=1)
    second = repo.claim_next(worker="w", now=later)
    assert second is not None and second.attempt == 2
    dead = repo.fail(second.run_id, error="boom-2")
    assert dead.status == JobStatus.DEAD.value


def test_inmemory_scope_exclusive() -> None:
    repo = InMemoryMetaRepository()
    first = repo.create_run(_intent("job-a", dataset="d", scope="s"))
    second = repo.create_run(_intent("job-b", dataset="d", scope="s"))
    assert first is not None and second is not None

    claimed = repo.claim_next(worker="w")
    assert claimed is not None and claimed.run_id == first.run_id
    assert repo.claim_next(worker="w") is None  # 同 dataset+scope 互斥

    repo.succeed(claimed.run_id)
    next_run = repo.claim_next(worker="w")
    assert next_run is not None and next_run.run_id == second.run_id


def test_inmemory_dependency_gate() -> None:
    repo = InMemoryMetaRepository()
    repo.sync_dependencies([JobDependency(parent_job="parent", child_job="child")])
    assert (
        repo.parents_ready(
            child_job="child", scope="", window_start=DAY, window_end=DAY
        )
        is False
    )
    repo.create_run(_intent("parent", dataset="p", max_attempts=1))
    parent = repo.claim_next(worker="w")
    assert parent is not None
    repo.fail(parent.run_id, error="nope")
    # 父失败（dead）→ 下游不放开（on_success）
    assert (
        repo.parents_ready(
            child_job="child", scope="", window_start=DAY, window_end=DAY
        )
        is False
    )


# ------------------------------------------------------------------ Dispatcher / Scheduler
def test_dispatcher_dependency_gate_and_duplicate() -> None:
    repo = InMemoryMetaRepository()
    repo.sync_dependencies([JobDependency(parent_job="parent", child_job="child")])
    dispatcher = Dispatcher(repo, max_queued=10)

    child_intent = _intent("child", kind="derive", dataset="c", version_dimension="ma20_v1")
    assert dispatcher.submit(child_intent) == SUBMIT_DEPENDENCY  # 父未成功

    assert dispatcher.submit(_intent("parent", dataset="p")) == SUBMIT_CREATED
    parent = repo.claim_next(worker="w")
    assert parent is not None
    repo.succeed(parent.run_id)

    assert dispatcher.submit(child_intent) == SUBMIT_CREATED
    assert dispatcher.submit(child_intent) == SUBMIT_DUPLICATE  # 幂等
    assert dispatcher.health.created == 2
    assert dispatcher.health.dependency_blocked == 1


def test_dispatcher_backpressure_bounded_queue() -> None:
    repo = InMemoryMetaRepository()
    dispatcher = Dispatcher(repo, max_queued=1)
    assert dispatcher.submit(_intent("a", dataset="d1")) == SUBMIT_CREATED
    assert dispatcher.submit(_intent("b", dataset="d2")) == SUBMIT_BACKPRESSURE
    # 领取后 queued 归零，背压解除
    assert repo.claim_next(worker="w") is not None
    assert dispatcher.submit(_intent("b", dataset="d2")) == SUBMIT_CREATED
    assert dispatcher.health.backpressure_blocked == 1


def test_scheduler_tick_emits_without_executing() -> None:
    registry = _registry()
    scheduler = Scheduler(
        registry,
        due_provider=lambda spec, now: [(DAY, DAY)] if spec.job_id == "parent" else [],
    )
    intents = scheduler.tick(now=datetime(2026, 9, 14, 18, 0))
    assert [intent.job_id for intent in intents] == ["parent"]
    assert scheduler.health.intents_emitted == 1
    assert Scheduler(registry).tick() == []  # 无 provider：仅手动触发


# ------------------------------------------------------------------ WorkerPool
def test_worker_pool_executes_and_reports_failure() -> None:
    def _boom(ctx) -> JobResult:
        raise RuntimeError("nope")

    registry = TaskRegistry()
    registry.register(
        TaskSpec(
            job_id="ok",
            kind="sync",
            dataset="d",
            executor=lambda ctx: JobResult(rows_written=3),
        )
    )
    registry.register(
        TaskSpec(job_id="bad", kind="sync", dataset="e", max_attempts=1, executor=_boom)
    )
    repo = InMemoryMetaRepository()
    repo.create_run(_intent("ok", dataset="d"))
    repo.create_run(_intent("bad", dataset="e", max_attempts=1))
    pool = WorkerPool(repo, registry, workers=1)

    first = pool.execute_once(worker_id="w")
    assert first is not None
    assert first.status == JobStatus.SUCCEEDED.value and first.rows_written == 3

    second = pool.execute_once(worker_id="w")
    assert second is not None
    assert second.status == JobStatus.DEAD.value
    assert second.error is not None and "RuntimeError" in second.error
    assert pool.execute_once(worker_id="w") is None


# ------------------------------------------------------------------ SQL 仓储 / App
def test_sql_repository_flow(engine) -> None:
    repo = SqlMetaRepository(engine)
    repo.sync_defs(
        [JobDef(job_id="daily", kind="sync", dataset="cn_equity.daily_bar")]
    )
    assert [item.job_id for item in repo.list_defs()] == ["daily"]

    created = repo.create_run(_intent("daily", dataset="cn_equity.daily_bar"))
    assert created is not None
    assert repo.create_run(_intent("daily", dataset="cn_equity.daily_bar")) is None

    run = repo.claim_next(worker="w")
    assert run is not None and run.status == JobStatus.RUNNING.value
    done = repo.succeed(run.run_id, rows_written=5)
    assert done.status == JobStatus.SUCCEEDED.value and done.rows_written == 5
    assert repo.count_queued() == 0

    repo.set_watermark("cn_equity.daily_bar", watermark_time=datetime(2026, 9, 14))
    mark = repo.get_watermark("cn_equity.daily_bar")
    assert mark is not None and mark.watermark_time == datetime(2026, 9, 14)


def test_runtime_app_end_to_end(engine) -> None:
    repo = SqlMetaRepository(engine)
    registry = _registry()
    app = RuntimeApp(_config("all"), engine=engine, repository=repo, registry=registry)
    app.sync_metadata()
    assert {item.job_id for item in repo.list_defs()} == {"parent", "child"}
    assert [(dep.parent_job, dep.child_job) for dep in repo.list_dependencies()] == [
        ("parent", "child")
    ]

    assert app.submit(_intent("parent", dataset="cn_equity.parent")) == SUBMIT_CREATED
    child_intent = _intent(
        "child", kind="derive", dataset="cn_equity.child", version_dimension="ma20_v1"
    )
    assert app.submit(child_intent) == SUBMIT_DEPENDENCY
    # 父成功后自动重投递子任务（链式推进）：一轮执行 parent + child
    assert app.run_pending() == 2
    # 已完成：手动再投递幂等去重
    assert app.submit(child_intent) == SUBMIT_DUPLICATE

    runs = repo.list_runs()
    assert {run.job_id for run in runs} == {"parent", "child"}
    assert all(run.status == JobStatus.SUCCEEDED.value for run in runs)
    assert app.health()["dispatcher"]["created"] == 1


def test_runtime_role_split_start_stop(engine) -> None:
    repo = SqlMetaRepository(engine)
    for role in ("scheduler", "worker"):
        app = RuntimeApp(
            _config(role), engine=engine, repository=repo, registry=_registry()
        )
        app.start()
        health = app.health()
        assert health["role"] == role
        if role == "worker":
            assert health["workers"]["alive"] is True
        else:
            assert health["workers"]["alive"] is False
        app.stop(timeout=1.0)


# ------------------------------------------------------------------ 就绪 / 入口
def test_readiness_fail_fast_on_schema_mismatch(engine) -> None:
    report = readiness(engine, dsn="postgresql+psycopg://u:p@localhost:5432/db")
    assert report.checks["database"] is True
    assert report.checks["dictionary"] is True
    assert report.checks["schema_revision"] is False  # 未 stamp → fail fast
    assert report.ok is False

    from fin_data_platform.storage.migrations import expected_head_revision

    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": expected_head_revision("postgresql+psycopg://u:p@localhost:5432/db")},
        )
    assert readiness(
        engine, dsn="postgresql+psycopg://u:p@localhost:5432/db"
    ).ok is True


def test_entrypoint_rejects_unknown_role() -> None:
    with pytest.raises(SystemExit):
        main(["--role", "unknown"])


def test_entrypoint_check_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("FDP_SYNC_CODES", "FDP_SYNC_START", "FDP_SYNC_SOURCE", "FDP_SYNC_SCHEDULE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "p")
    monkeypatch.setenv("DATABASE_HOST", "127.0.0.1")
    monkeypatch.setenv("DATABASE_PORT", "1")
    # 连接超时兜底：网络不可达时快速失败（不无限挂起）
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT", "1")
    assert main(["--check"]) == 1

    # 配置非法优先于就绪检查（退出码 2）
    monkeypatch.setenv("FDP_SYNC_CODES", "600519.SH")
    monkeypatch.setenv("FDP_SYNC_START", "2026-09-01")
    assert main(["--check"]) == 2


# ------------------------------------------------------------------ 复审回归
def test_replay_after_dead_restarts_attempt_budget() -> None:
    repo = InMemoryMetaRepository()
    repo.create_run(_intent(max_attempts=2))
    first = repo.claim_next(worker="w")
    assert first is not None
    retrying = repo.fail(first.run_id, error="1")
    assert retrying.status == JobStatus.RETRYING.value

    second = repo.claim_next(worker="w", now=retrying.scheduled_at + timedelta(seconds=1))
    assert second is not None
    assert repo.fail(second.run_id, error="2").status == JobStatus.DEAD.value

    replay = repo.create_run(_intent(max_attempts=2))
    assert replay is not None and replay.attempt == 1  # 新会话重新计预算
    claimed = repo.claim_next(worker="w")
    assert claimed is not None
    assert repo.fail(claimed.run_id, error="x").status == JobStatus.RETRYING.value


def test_sql_sync_replaces_stale_defs_and_dependencies(engine) -> None:
    repo = SqlMetaRepository(engine)
    repo.sync_defs(
        [
            JobDef(job_id="a", kind="sync", dataset="d1"),
            JobDef(job_id="b", kind="sync", dataset="d2"),
        ]
    )
    repo.sync_dependencies([JobDependency(parent_job="a", child_job="b")])
    assert len(repo.list_dependencies()) == 1

    repo.sync_defs([JobDef(job_id="b", kind="sync", dataset="d2")])
    assert [item.job_id for item in repo.list_defs()] == ["b"]
    repo.sync_dependencies([])
    assert repo.list_dependencies() == []  # 依赖清空必须删除旧行（防永久门控）


def test_interrupt_running_marks_stuck_rows() -> None:
    repo = InMemoryMetaRepository()
    repo.create_run(_intent("job-a", dataset="d1"))
    repo.create_run(_intent("job-b", dataset="d2"))
    first = repo.claim_next(worker="worker-0")
    second = repo.claim_next(worker="other-0")
    assert first is not None and second is not None

    assert repo.interrupt_running(worker_prefix="worker") == 1
    assert repo.get_run(first.run_id).status == JobStatus.INTERRUPTED.value
    assert repo.get_run(second.run_id).status == JobStatus.RUNNING.value


def test_worker_stop_timeout_reclaims_running(engine) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking(ctx) -> JobResult:
        started.set()
        release.wait(timeout=5)
        return JobResult(rows_written=1)

    registry = TaskRegistry()
    registry.register(
        TaskSpec(job_id="slow", kind="sync", dataset="d", executor=blocking)
    )
    repo = SqlMetaRepository(engine)
    repo.create_run(_intent("slow", dataset="d"))
    pool = WorkerPool(repo, registry, name="tp", workers=1)
    stop = threading.Event()

    try:
        pool.start(stop, interval=0.01)
        assert started.wait(timeout=5)
        pool.stop(timeout=0.05)  # 线程卡在执行中 → 超时回收 running 行
        interrupted = repo.list_runs(status=JobStatus.INTERRUPTED.value)
        assert [run.job_id for run in interrupted] == ["slow"]
    finally:
        stop.set()
        release.set()


def test_readiness_reports_unreachable_database() -> None:
    engine = create_engine(
        "postgresql+psycopg://u:p@127.0.0.1:1/db",
        connect_args={"connect_timeout": 1},
    )
    report = readiness(engine, dsn="postgresql+psycopg://u:p@localhost:5432/db")
    assert report.ok is False
    assert report.checks["database"] is False
    assert report.checks["schema_revision"] is False  # 不再抛异常
