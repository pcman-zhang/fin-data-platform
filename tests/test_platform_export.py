"""批量导出（TASK-3.10）单测：分块写出 / 任务端到端 / API 流程 / 资源隔离配置。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.api.app import create_app
from fin_data_platform.api.deps import ApiContext
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.export import (
    EXPORT_DATASET,
    EXPORT_JOB,
    create_request,
    get_request,
    mark_failed,
    register_export_task,
    write_export,
)
from fin_data_platform.registry.reader import RegistryReader
from fin_data_platform.runtime import RuntimeApp, RuntimeConfig, SqlMetaRepository, TaskRegistry
from fin_data_platform.runtime.models import JobIntent
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.engine import _connect_args
from fin_data_platform.storage.schema import build_metadata

NOW = datetime(2026, 10, 8, 10, 0)
#: 知识时间早于查询时点（SQLite 时间字符串比较不支持相等边界；PG 支持）
EARLIER = datetime(2026, 10, 7, 10, 0)
D0 = date(2026, 9, 28)
D1 = date(2026, 9, 29)
D2 = date(2026, 9, 30)
DAY = date(2026, 10, 8)
DATASET = "cn_equity.daily_bar"


def _bar(entity_id: int, day: date, close: float) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
        "amount": None,
        "knowledge_time": EARLIER,
        "publish_time": None,
        "ingest_time": EARLIER,
        "version": 1,
        "provider": "tushare",
    }


def _seed(engine, metadata):  # type: ignore[no-untyped-def]
    with engine.begin() as connection:
        connection.execute(
            insert(metadata.tables[DATASET]),
            [
                _bar(1, D0, 100.0),
                _bar(1, D1, 101.0),
                _bar(1, D2, 102.0),
                _bar(2, D0, 200.0),
                _bar(2, D1, 201.0),
            ],
        )


@pytest.fixture()
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
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
    _seed(engine, metadata)
    return engine, metadata, tmp_path / "exports"


def test_write_export_parquet_and_arrow(env) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, directory = env
    params = {
        "version_mode": "latest",
        "fields": ["entity_id", "trade_date", "close"],
        "entities": [1, 2],
        "start": D0.isoformat(),
        "end": D2.isoformat(),
    }
    request = create_request(engine, dataset=DATASET, params=params, format="parquet")
    path = directory / f"{request.export_id}.parquet"
    rows, size = write_export(engine, request, path)
    assert rows == 5 and size > 0 and path.is_file()
    table = pq.read_table(path)
    assert table.num_rows == 5
    assert set(table.column_names) == {"entity_id", "trade_date", "close"}

    arrow_request = create_request(
        engine, dataset=DATASET, params=params, format="arrow"
    )
    arrow_path = directory / f"{arrow_request.export_id}.arrow"
    rows, _size = write_export(engine, arrow_request, arrow_path)
    assert rows == 5
    table = pa.ipc.open_stream(arrow_path.read_bytes()).read_all()
    assert table.num_rows == 5


def test_write_export_chunking_and_entity_batches(env) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, directory = env
    params = {
        "version_mode": "latest",
        "fields": ["entity_id", "trade_date", "close"],
        "start": D0.isoformat(),
        "end": D2.isoformat(),
    }
    request = create_request(engine, dataset=DATASET, params=params, format="parquet")
    path = directory / f"{request.export_id}.parquet"
    # 时间块 2 天 × 实体批 1 → 多块写出，行数仍为全量
    rows, _size = write_export(engine, request, path, entity_batch=1, chunk_days=2)
    assert rows == 5
    assert pq.read_table(path).num_rows == 5


def test_write_export_filters_and_pit(env) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, directory = env
    params = {
        "version_mode": "latest",
        "fields": ["entity_id", "close"],
        "entities": [1],
        "filters": [{"field": "close", "op": "gte", "value": 101.0}],
    }
    request = create_request(engine, dataset=DATASET, params=params, format="parquet")
    path = directory / f"{request.export_id}.parquet"
    rows, _size = write_export(engine, request, path)
    assert rows == 2
    assert pq.read_table(path)["close"].to_pylist() == [101.0, 102.0]


def test_export_task_end_to_end(env) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, directory = env
    registry = TaskRegistry()
    register_export_task(registry, engine, export_dir=directory)
    repository = SqlMetaRepository(engine)
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://")),
        engine=engine,
        repository=repository,
        registry=registry,
    )
    app.sync_metadata()
    request = create_request(
        engine,
        dataset=DATASET,
        params={
            "version_mode": "latest",
            "fields": ["entity_id", "close"],
            "start": D0.isoformat(),
            "end": D2.isoformat(),
        },
        format="parquet",
    )
    intent = JobIntent(
        kind="export",
        job_id=EXPORT_JOB,
        dataset=EXPORT_DATASET,
        scope=request.export_id,
        window_start=DAY,
        window_end=DAY,
    )
    assert app.submit(intent) == "created"
    assert app.run_pending() == 1
    stored = get_request(engine, request.export_id)
    assert stored is not None and stored.status == "succeeded"
    assert stored.rows == 5 and stored.artifact_path and Path(stored.artifact_path).is_file()
    run = repository.list_runs()[0]
    assert run.status == "succeeded" and run.rows_written == 5

    # 失败路径：非法数据集 → 状态 failed + error 留痕
    bad = create_request(engine, dataset="nope.dataset", params={}, format="parquet")
    app.submit(
        JobIntent(
            kind="export",
            job_id=EXPORT_JOB,
            dataset=EXPORT_DATASET,
            scope=bad.export_id,
            window_start=DAY,
            window_end=DAY,
        )
    )
    app.run_pending()
    stored_bad = get_request(engine, bad.export_id)
    assert stored_bad is not None and stored_bad.status == "failed"
    assert "数据集不存在" in (stored_bad.error or "")


def test_export_api_flow(env) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, directory = env
    registry = TaskRegistry()
    register_export_task(registry, engine, export_dir=directory)
    repository = SqlMetaRepository(engine)
    runtime = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://")),
        engine=engine,
        repository=repository,
        registry=registry,
    )
    runtime.sync_metadata()
    context = ApiContext(
        config=StorageConfig(write_dsn="sqlite://"),
        writer_engine=engine,
        read_engine=engine,
        meta=repository,
        algorithms=InMemoryAlgorithmStore(),
        registry=RegistryReader(engine),
        specs=load_all(),
        factors=None,
    )
    client = TestClient(create_app(context, web_dist=None))

    created = client.post(
        "/v1/exports",
        json={
            "dataset": DATASET,
            "version_mode": "latest",
            "fields": ["entity_id", "close"],
            "start": D0.isoformat(),
            "end": D2.isoformat(),
            "format": "parquet",
        },
    )
    assert created.status_code == 202
    export_id = created.json()["export_id"]
    assert created.json()["run_id"]

    status = client.get(f"/v1/exports/{export_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "pending"
    assert status.json()["download_url"] is None

    assert runtime.run_pending() == 1
    status = client.get(f"/v1/exports/{export_id}").json()
    assert status["status"] == "succeeded" and status["rows"] == 5
    assert status["download_url"] == f"/v1/exports/{export_id}/download"

    download = client.get(f"/v1/exports/{export_id}/download")
    assert download.status_code == 200
    table = pq.read_table(pa.BufferReader(download.content))
    assert table.num_rows == 5

    listing = client.get("/v1/exports")
    assert listing.status_code == 200 and listing.json()["total"] == 1

    # 参数校验（问题体）
    assert client.post("/v1/exports", json={"dataset": "nope"}).status_code == 404
    bad_field = client.post(
        "/v1/exports", json={"dataset": DATASET, "fields": ["nope"]}
    )
    assert bad_field.status_code == 422
    assert bad_field.json()["title"] == "invalid_field"
    bad_window = client.post(
        "/v1/exports", json={"dataset": DATASET, "start": D0.isoformat()}
    )
    assert bad_window.status_code == 422 and bad_window.json()["title"] == "unsupported_filter"
    assert client.get("/v1/exports/unknown-id").status_code == 404
    assert client.get("/v1/exports/unknown-id/download").status_code == 404


def test_statement_timeout_config_and_injection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "p")
    monkeypatch.setenv("DATABASE_HOST", "db")
    monkeypatch.setenv("DATABASE_STATEMENT_TIMEOUT", "45")
    config = StorageConfig.from_env()
    assert config.statement_timeout == 45.0
    args = _connect_args(config, config.reader_dsn, {})
    assert args["connect_args"]["options"] == "-c statement_timeout=45000"
    # 关闭（0/空）与写端（不注入）
    monkeypatch.setenv("DATABASE_STATEMENT_TIMEOUT", "0")
    disabled = StorageConfig.from_env()
    assert disabled.statement_timeout is None
    assert "options" not in _connect_args(
        replace(disabled, statement_timeout=None), disabled.write_dsn, {}
    ).get("connect_args", {})
    # sqlite 不注入
    sqlite_config = StorageConfig(write_dsn="sqlite://", statement_timeout=30)
    assert "options" not in _connect_args(
        sqlite_config, "sqlite://", {}
    ).get("connect_args", {})


# ---------------------------------------------------------------- 评审修复回归
def test_write_export_empty_chunks_and_null_columns(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """空时间块不破坏 schema；全 NULL 列按物理类型写出（不再 schema 错配）。"""
    engine, _metadata, directory = env
    params = {
        "version_mode": "latest",
        "fields": ["entity_id", "trade_date", "close", "amount"],
        "start": "2026-09-01",  # 早于首条数据（09-28）→ 前面的时间块为空
        "end": D2.isoformat(),
    }
    request = create_request(engine, dataset=DATASET, params=params, format="parquet")
    path = directory / f"{request.export_id}.parquet"
    rows, _size = write_export(engine, request, path, chunk_days=7)
    assert rows == 5
    table = pq.read_table(path)
    assert table.num_rows == 5
    # amount 全 NULL：schema 仍为物理类型（double），不是 null 类型
    assert table.schema.field("amount").type == pa.float64()
    assert set(table.column("amount").to_pylist()) == {None}

    arrow_request = create_request(
        engine, dataset=DATASET, params=params, format="arrow"
    )
    arrow_path = directory / f"{arrow_request.export_id}.arrow"
    rows, _size = write_export(engine, arrow_request, arrow_path, chunk_days=7)
    assert rows == 5
    table = pa.ipc.open_stream(arrow_path.read_bytes()).read_all()
    assert table.schema.field("amount").type == pa.float64()


def test_write_export_entity_batch_effective(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """entity_batch 参数真实生效：每批读取的 entities 数量符合配置。"""
    from fin_data_platform.export import writer as writer_mod

    engine, _metadata, directory = env
    calls: list[tuple[int, ...]] = []
    original = writer_mod.read_rows

    def spy(engine_, query, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(tuple(query.entities))
        return original(engine_, query, **kwargs)

    monkeypatch.setattr(writer_mod, "read_rows", spy)
    request = create_request(
        engine,
        dataset=DATASET,
        params={
            "version_mode": "latest",
            "fields": ["entity_id", "close"],
            "start": D0.isoformat(),
            "end": D2.isoformat(),
        },
        format="parquet",
    )
    rows, _size = write_export(
        engine, request, directory / f"{request.export_id}.parquet", entity_batch=1
    )
    assert rows == 5
    assert sorted(calls) == [(1,), (2,)]  # 两个实体各一批


def test_write_export_dedupes_entities(env) -> None:  # type: ignore[no-untyped-def]
    """entities 去重：跨批重复 ID 不产生重复行。"""
    engine, _metadata, directory = env
    request = create_request(
        engine,
        dataset=DATASET,
        params={
            "version_mode": "latest",
            "fields": ["entity_id", "trade_date"],
            "entities": [1, 1, 2],
        },
        format="parquet",
    )
    path = directory / f"{request.export_id}.parquet"
    rows, _size = write_export(engine, request, path, entity_batch=1)
    assert rows == 5  # 实体 1 三行 + 实体 2 两行（重复 ID 不重复读出）
    table = pq.read_table(path)
    records = [(row["entity_id"], row["trade_date"]) for row in table.to_pylist()]
    assert len(records) == len(set(records))


def test_mark_running_clears_finished_at_and_reconcile(env) -> None:  # type: ignore[no-untyped-def]
    """重试清空 finished_at；启动对账把失联 running 请求置 failed。"""
    from fin_data_platform.export import reconcile_stale_exports
    from fin_data_platform.export.store import mark_running

    engine, _metadata, _directory = env
    request = create_request(engine, dataset=DATASET, params={}, format="parquet")
    mark_failed(engine, request.export_id, error="boom")
    assert get_request(engine, request.export_id).finished_at is not None
    mark_running(engine, request.export_id, run_id=None)
    stored = get_request(engine, request.export_id)
    assert stored.status == "running" and stored.finished_at is None

    assert reconcile_stale_exports(engine) == 1
    stored = get_request(engine, request.export_id)
    assert stored.status == "failed" and "对账" in (stored.error or "")


def test_export_api_validations_and_intent_params(env) -> None:  # type: ignore[no-untyped-def]
    """提交期校验（算子/策略/entities 适用性）与意图参数（重试预算/优先级）。"""
    engine, _metadata, directory = env
    registry = TaskRegistry()
    register_export_task(registry, engine, export_dir=directory)
    repository = SqlMetaRepository(engine)
    runtime = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://")),
        engine=engine,
        repository=repository,
        registry=registry,
    )
    runtime.sync_metadata()
    context = ApiContext(
        config=StorageConfig(write_dsn="sqlite://"),
        writer_engine=engine,
        read_engine=engine,
        meta=repository,
        algorithms=InMemoryAlgorithmStore(),
        registry=RegistryReader(engine),
        specs=load_all(),
        factors=None,
    )
    client = TestClient(create_app(context, web_dist=None))

    bad_op = client.post(
        "/v1/exports",
        json={"dataset": DATASET, "filters": [{"field": "close", "op": "like"}]},
    )
    assert bad_op.status_code == 422 and bad_op.json()["title"] == "unsupported_filter"
    bad_policy = client.post(
        "/v1/exports", json={"dataset": DATASET, "as_of_policy": "bogus"}
    )
    assert bad_policy.status_code == 422
    assert bad_policy.json()["title"] == "invalid_as_of_policy"
    no_entity = client.post(
        "/v1/exports",
        json={"dataset": "ref.trade_calendar", "entities": [1]},
    )
    assert no_entity.status_code == 422 and no_entity.json()["title"] == "unsupported_filter"

    created = client.post("/v1/exports", json={"dataset": DATASET})
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT priority, max_attempts FROM meta.job_runs WHERE run_id = :rid"),
            {"rid": run_id},
        ).one()
    assert row[0] == 200 and row[1] == 2  # TaskSpec 声明（priority/max_attempts）
