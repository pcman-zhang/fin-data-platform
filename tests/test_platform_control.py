"""控制面意图客户端测试（TASK-3.26）：ensure / materialize / wait / 权限边界。"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.control import (
    ControlClient,
    IntentError,
    IntentTimeout,
    InvalidRequest,
    InvalidWindow,
    JobNotRegistered,
    UnknownFactor,
)
from fin_data_platform.derived.store import AlgorithmRow, InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.runtime.calendar import StoredTradeCalendar
from fin_data_platform.runtime.models import JobDef, JobIntent, JobKind, JobStatus
from fin_data_platform.runtime.repository import InMemoryMetaRepository, SqlMetaRepository
from fin_data_platform.storage.schema import build_metadata

DATASET = "cn_equity.daily_bar"
CODE = "600519.SH"
SYNC_JOB = f"sync.{DATASET}.{CODE}"
DERIVE_JOB = f"derive.{DATASET}.ma20"

D1 = date(2026, 9, 10)
D2 = date(2026, 9, 11)
D3 = date(2026, 9, 14)
D4 = date(2026, 9, 15)  # 测试用非交易日：保证 last_closed 与运行时刻无关
CLOCK = datetime(2026, 9, 15, 12, 0)


@pytest.fixture()
def engine():  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        for schema in ("cn_equity", "cn_fund", "ref", "meta", "mart"):
            connection.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    metadata, _ = build_metadata()
    metadata.create_all(engine)
    calendar = metadata.tables["ref.trade_calendar"]
    with engine.begin() as connection:
        connection.execute(
            calendar.insert(),
            [
                {
                    "exchange_id": "XSHG",
                    "trade_date": day,
                    "is_open": is_open,
                    "pretrade_date": None,
                    "knowledge_time": datetime(2026, 9, 1),
                    "version": 1,
                }
                for day, is_open in ((D1, True), (D2, True), (D3, True), (D4, False))
            ],
        )
    return engine, metadata


def _meta(*, derive: bool = False) -> InMemoryMetaRepository:
    repository = InMemoryMetaRepository()
    defs = [JobDef(job_id=SYNC_JOB, kind=JobKind.SYNC.value, dataset=DATASET)]
    if derive:
        defs.append(JobDef(job_id=DERIVE_JOB, kind=JobKind.DERIVE.value, dataset=DATASET))
    repository.sync_defs(defs)
    return repository


def _algorithms(output: str = "ma20") -> InMemoryAlgorithmStore:
    store = InMemoryAlgorithmStore()
    store.upsert(
        [
            AlgorithmRow(
                algorithm_id=output,
                version=1,
                owner="derived-engine",
                implementation=f"fin_data_platform.derived.factors.{output}",
                dataset=DATASET,
                output=output,
                inputs=(f"{DATASET}.close@hfq",),
                description="test",
                status="active",
                effective_from=None,
            )
        ]
    )
    return store


def _client(engine, meta, algorithms=None, **kwargs):  # type: ignore[no-untyped-def]
    calendar = StoredTradeCalendar(engine, clock=lambda: CLOCK)
    return ControlClient(
        engine,
        specs=load_all(),
        meta=meta,
        algorithms=algorithms or _algorithms(),
        calendar=calendar,
        clock=lambda: CLOCK,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_last_closed_uses_stored_calendar(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    assert _client(db, _meta()).last_closed() == D3


def test_ensure_default_window_uses_watermark_and_last_closed(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    meta = _meta()
    meta.set_watermark(DATASET, scope=CODE, watermark_time=datetime(2026, 9, 10))
    client = _client(db, meta)
    runs = client.ensure(DATASET)
    assert len(runs) == 1
    handle = runs.runs[0]
    assert (handle.window_start, handle.window_end) == (D2, D3)
    assert handle.created and handle.job_id == SYNC_JOB
    assert handle.status == JobStatus.QUEUED.value


def test_ensure_explicit_window_beyond_last_closed_raises(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    client = _client(db, _meta())
    with pytest.raises(InvalidWindow) as excinfo:
        client.ensure(DATASET, window=(D1, D4))
    assert excinfo.value.code == "invalid_window" and str(D3) in str(excinfo.value)
    with pytest.raises(InvalidWindow, match="窗口为空"):
        client.ensure(DATASET, window=(D2, D1))


def test_ensure_idempotent_request_id_and_job_key(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    client = _client(db, _meta())
    first = client.ensure(DATASET, window=(D1, D2), request_id="r1").runs[0]
    assert first.created

    replay = client.ensure(DATASET, window=(D1, D2), request_id="r1").runs[0]
    assert (replay.created, replay.matched_via, replay.run_id) == (
        False,
        "request_id",
        first.run_id,
    )

    duplicate = client.ensure(DATASET, window=(D1, D2)).runs[0]
    assert (duplicate.created, duplicate.matched_via, duplicate.run_id) == (
        False,
        "job_key",
        first.run_id,
    )


def test_ensure_unknown_code_or_dataset_raises(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    client = _client(db, _meta())
    with pytest.raises(JobNotRegistered, match="代码未注册"):
        client.ensure(DATASET, codes=["000000.XX"])
    with pytest.raises(JobNotRegistered, match="未注册同步任务"):
        client.ensure("cn_equity.unknown", window=(D1, D2))


def test_ensure_request_id_rejects_multi_code(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    meta = _meta()
    meta.sync_defs(
        [JobDef(job_id=f"sync.{DATASET}.000001.SZ", kind=JobKind.SYNC.value, dataset=DATASET)]
    )
    client = _client(db, meta)
    with pytest.raises(InvalidRequest) as excinfo:
        client.ensure(
            DATASET,
            codes=[CODE, "000001.SZ"],
            window=(D1, D2),
            request_id="r-1",
        )
    assert excinfo.value.code == "invalid_request"
    assert len(meta.list_runs()) == 0  # 拒绝时不产生半提交


def test_last_closed_fail_open_without_calendar_table() -> None:  # type: ignore[no-untyped-def]
    bare = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    client = _client(bare, _meta())
    assert client.last_closed() is None  # 未迁移：不可用（fail-open，由调用方回退）


def test_ensure_up_to_date_returns_empty(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    meta = _meta()
    meta.set_watermark(DATASET, scope=CODE, watermark_time=datetime(2026, 9, 14))
    assert len(_client(db, meta).ensure(DATASET)) == 0  # 已追平：不提交


def test_materialize_submits_derive_intent(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    meta = _meta(derive=True)
    client = _client(db, meta)
    handle = client.materialize("ma20", request_id="m-1")
    assert handle.created and handle.job_id == DERIVE_JOB
    assert handle.version_dimension == "ma20@v1"
    assert (handle.window_start, handle.window_end) == (D4, D4)  # 触发日窗口

    hit = client.materialize("ma20", request_id="m-1")  # 幂等键命中
    assert (hit.created, hit.matched_via, hit.run_id) == (False, "request_id", handle.run_id)
    dup = client.materialize(DATASET + ".ma20")  # 无幂等键：同窗口去重
    assert (dup.created, dup.matched_via, dup.run_id) == (False, "job_key", handle.run_id)


def test_materialize_unknown_or_unregistered(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    client = _client(db, _meta(derive=True))
    with pytest.raises(UnknownFactor, match="因子不存在"):
        client.materialize("no_such_factor")
    # 字典存在但未装配物化任务（fixture 仅注册 ma20）
    with pytest.raises(JobNotRegistered, match="物化任务未注册"):
        client.materialize("adx")


def test_materialize_without_algorithm_row_raises(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    client = _client(db, _meta(derive=True), algorithms=InMemoryAlgorithmStore())
    with pytest.raises(IntentError, match="算法台账"):
        client.materialize("ma20")


def test_wait_returns_terminal_and_times_out(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    meta = _meta()
    client = _client(db, meta)
    handle = client.ensure(DATASET, window=(D1, D2)).runs[0]

    with pytest.raises(IntentTimeout) as excinfo:
        handle.wait(timeout=0)
    assert excinfo.value.code == "timeout"

    meta.claim_next(worker="test")  # 领取（queued → running）后才能落终态
    meta.succeed(handle.run_id, rows_written=2)
    finished = handle.wait(timeout=0)
    assert finished.status == JobStatus.SUCCEEDED.value and finished.rows_written == 2

    # 失败可见：错误入运行记录（retrying 携带失败原因；wait 继续等待终态）
    failed = client.ensure(DATASET, window=(D2, D3)).runs[0]
    meta.claim_next(worker="test")
    meta.fail(failed.run_id, error="EmptySourceWindow: boom")
    retrying = meta.get_run(failed.run_id)
    assert retrying is not None
    assert retrying.status == JobStatus.RETRYING.value
    assert "EmptySourceWindow" in (retrying.error or "")

    # 重试耗尽 → dead（构造 max_attempts=1 的运行）
    doomed = meta.create_run(
        JobIntent(
            kind=JobKind.SYNC.value,
            job_id=SYNC_JOB,
            dataset=DATASET,
            scope=CODE,
            window_start=D3,
            window_end=D3,
            max_attempts=1,
        )
    )
    assert doomed is not None
    doomed_handle = client.run(doomed.run_id)
    assert meta.claim_next(worker="test") is not None
    meta.fail(doomed_handle.run_id, error="EmptySourceWindow: dead")
    terminal = doomed_handle.wait(timeout=0)
    assert terminal.status == JobStatus.DEAD.value
    assert "EmptySourceWindow" in (terminal.error or "")


def test_ensure_writes_only_meta_queue(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    repository = SqlMetaRepository(db)
    repository.sync_defs(
        [JobDef(job_id=SYNC_JOB, kind=JobKind.SYNC.value, dataset=DATASET)]
    )
    client = _client(db, repository)

    assert len(client.ensure(DATASET, window=(D1, D2))) == 1
    with db.connect() as connection:
        queued = connection.execute(
            select(func.count()).select_from(metadata.tables["meta.job_runs"])
        ).scalar_one()
        bars = connection.execute(
            select(func.count()).select_from(metadata.tables[DATASET])
        ).scalar_one()
    assert queued == 1  # 只写意图队列
    assert bars == 0  # 客户端无数据写路径


def test_run_handle_for_existing_run(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    client = _client(db, _meta())
    handle = client.ensure(DATASET, window=(D1, D2)).runs[0]
    fetched = client.run(handle.run_id)
    assert fetched.run_id == handle.run_id and fetched.created is False
    with pytest.raises(IntentError, match="运行不存在"):
        client.run(999999)
