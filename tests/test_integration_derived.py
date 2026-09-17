"""派生引擎真实数据库集成测试（默认跳过：``pytest -m integration``）。

覆盖 PostgreSQL 专属路径：as-of 读取（date 类型回读）、跨方言内联 SQL、
物化（pandas 批量写入 + 影子表原子换名 + meta.data_generation）。

依赖 ``DATABASE_*``（同 ``tests/test_integration_storage.py``）；
测试使用围栏实体 ``999998`` 并在结束后清理（不触碰其他数据）。
"""

from __future__ import annotations

import os
from datetime import date, datetime

import pytest
from sqlalchemy import delete, text

from fin_data_platform.derived.engine import DerivedEngine
from fin_data_platform.derived.store import InMemoryAlgorithmStore, SqlAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import Materialize
from fin_data_platform.storage import (
    StorageConfig,
    build_metadata,
    create_write_engine,
    ensure_schema,
)

pytestmark = pytest.mark.integration

_TEST_ENTITY = 999998
_AS_OF = datetime(2026, 9, 15, 12, 0)
_PROJECTION = "mart.derived_daily_bar_qfq_close"


@pytest.fixture(scope="module")
def engine():
    if not os.environ.get("DATABASE_USER"):
        pytest.skip("缺少 DATABASE_* 环境变量")
    config = StorageConfig.from_env(host_override=os.environ.get("FDP_DATABASE_HOST"))
    engine = create_write_engine(config)
    metadata, specs = build_metadata()
    ensure_schema(engine, config=config, metadata=metadata, specs=specs)

    def _known(day: int) -> datetime:
        return datetime(2026, 9, day, 12, 0)

    daily = metadata.tables["cn_equity.daily_bar"]
    factor = metadata.tables["cn_equity.adj_factor"]
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS mart"))
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS meta.data_generation ("
                "read_model varchar(128) PRIMARY KEY, generation varchar(32) NOT NULL, "
                "updated_at timestamptz NOT NULL)"
            )
        )
        connection.execute(
            daily.insert(),
            [
                {
                    "entity_id": _TEST_ENTITY,
                    "trade_date": date(2026, 9, 10),
                    "close": 10.0,
                    "knowledge_time": _known(10),
                    "ingest_time": _known(10),
                    "version": 1,
                    "provider": "tushare",
                },
                {
                    "entity_id": _TEST_ENTITY,
                    "trade_date": date(2026, 9, 11),
                    "close": 11.0,
                    "knowledge_time": _known(11),
                    "ingest_time": _known(11),
                    "version": 1,
                    "provider": "tushare",
                },
            ],
        )
        connection.execute(
            factor.insert(),
            [
                {
                    "entity_id": _TEST_ENTITY,
                    "trade_date": date(2026, 9, 10),
                    "adj_factor": 1.0,
                    "knowledge_time": _known(10),
                    "ingest_time": _known(10),
                    "version": 1,
                    "provider": "tushare",
                },
                {
                    "entity_id": _TEST_ENTITY,
                    "trade_date": date(2026, 9, 11),
                    "adj_factor": 2.0,
                    "knowledge_time": _known(11),
                    "ingest_time": _known(11),
                    "version": 1,
                    "provider": "tushare",
                },
            ],
        )
    yield engine, metadata
    with engine.begin() as connection:
        connection.execute(delete(daily).where(daily.c.entity_id == _TEST_ENTITY))
        connection.execute(delete(factor).where(factor.c.entity_id == _TEST_ENTITY))
        connection.execute(text(f"DROP TABLE IF EXISTS {_PROJECTION}"))
        connection.execute(
            text("DELETE FROM meta.data_generation WHERE read_model = :name"),
            {"name": _PROJECTION},
        )
    engine.dispose()


def _engine_for(engine, *, latest: bool = False, store=None):  # type: ignore[no-untyped-def]
    specs = load_all()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0]
    if latest:
        entry = entry.model_copy(update={"materialize": Materialize.LATEST})
        specs = dict(specs)
        specs["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    return DerivedEngine(engine, specs=specs, store=store), specs


def test_execute_and_inline_on_postgresql(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    derived, _specs = _engine_for(db)

    result = derived.execute("qfq_close", as_of=_AS_OF, entity_ids=[_TEST_ENTITY])
    rows = result.values.to_pylist()
    assert [(row["trade_date"], row["qfq_close"]) for row in rows] == [
        (date(2026, 9, 10), pytest.approx(5.0)),
        (date(2026, 9, 11), pytest.approx(11.0)),
    ]

    inline = derived.inline_sql("qfq_close", as_of=_AS_OF)
    with db.connect() as connection:
        inline_rows = (
            connection.execute(text(f"SELECT * FROM ({inline.strip().rstrip(';')}) AS q"))
            .mappings()
            .all()
        )
    subset = [row for row in inline_rows if row["entity_id"] == _TEST_ENTITY]
    assert len(subset) == len(rows)
    assert subset[0]["qfq_close"] == pytest.approx(rows[0]["qfq_close"])
    assert subset[1]["qfq_close"] == pytest.approx(rows[1]["qfq_close"])


def test_materialize_atomic_swap_and_generation(engine) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import inspect

    db, _metadata = engine
    store = SqlAlgorithmStore(db)
    derived, _specs = _engine_for(db, latest=True, store=store)

    first = derived.materialize("qfq_close", as_of=_AS_OF, entity_ids=[_TEST_ENTITY])
    inspector = inspect(db)
    assert inspector.has_table("derived_daily_bar_qfq_close", schema="mart")
    assert not inspector.has_table("derived_daily_bar_qfq_close__next", schema="mart")
    assert store.get_generation(_PROJECTION) == first.generation

    # 二次物化：整体重算 + 原子换名（影子表不残留）
    second = derived.materialize("qfq_close", as_of=_AS_OF, entity_ids=[_TEST_ENTITY])
    assert inspector.has_table("derived_daily_bar_qfq_close", schema="mart")
    assert not inspector.has_table("derived_daily_bar_qfq_close__next", schema="mart")
    assert store.get_generation(_PROJECTION) == second.generation

    with db.connect() as connection:
        row = connection.execute(
            text(
                "SELECT algorithm_id, data_generation, count(*) AS rows"
                f" FROM {_PROJECTION} WHERE entity_id = {_TEST_ENTITY}"
                " GROUP BY algorithm_id, data_generation"
            )
        ).one()
    assert row[0] == "qfq_close_v1"
    assert row[1] == second.generation
    assert row[2] == 2

    # 代次元数据由 store 记录（与 InMemory 实现同构）
    assert isinstance(store, SqlAlgorithmStore)
    assert InMemoryAlgorithmStore().get_generation(_PROJECTION) is None


def test_registry_preserved_on_deprecation_postgresql(engine) -> None:  # type: ignore[no-untyped-def]
    """退役 upsert 的 COALESCE 语义（PG）：归属信息不因字典删除而丢失。"""
    from fin_data_platform.derived.store import SqlAlgorithmStore
    from fin_data_platform.derived.sync import sync_algorithms

    db, _metadata = engine
    store = SqlAlgorithmStore(db)
    specs = load_all()
    sync_algorithms(store, specs)

    dataset = specs["cn_equity.daily_bar"]
    retired = dict(specs)
    retired["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": None})
    sync_algorithms(store, retired)

    row = {item.algorithm_id: item for item in store.list_all()}["qfq_close_v1"]
    assert row.status == "deprecated"
    assert row.dataset == "cn_equity.daily_bar"
    assert row.output == "qfq_close"
    assert row.inputs == (
        "cn_equity.daily_bar.close",
        "cn_equity.adj_factor.adj_factor",
    )

    sync_algorithms(store, specs)  # 恢复真实字典状态
    restored = {item.algorithm_id: item for item in store.list_all()}["qfq_close_v1"]
    assert restored.status == "active"
