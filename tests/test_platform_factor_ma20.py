"""MA20 因子（TASK-3.28）：数值对账 / 窗口语义 / as-of 防前视 / 端到端物化。

数据：单实体 25 个交易日，close = 1..25，因子恒为 1（hfq/raw 数值相同）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.access import read as access_read
from fin_data_platform.derived.consistency import check_consistency, import_implementations
from fin_data_platform.derived.engine import DerivedEngine
from fin_data_platform.derived.errors import FactorError
from fin_data_platform.derived.factor_api import FactorAPI
from fin_data_platform.derived.registry import DEFAULT_REGISTRY
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all, validate_directory
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.storage.schema import build_metadata

DATASET = "cn_equity.daily_bar"
AS_OF = datetime(2026, 6, 30, 12, 0)


@pytest.fixture()
def engine():  # type: ignore[no-untyped-def]
    import pandas as pd

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        for schema in ("cn_equity", "cn_fund", "ref", "meta", "mart"):
            connection.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    metadata, _specs = build_metadata()
    metadata.create_all(engine)

    days = [item.date() for item in pd.bdate_range("2026-01-05", periods=25)]
    daily = metadata.tables[DATASET]
    factor = metadata.tables["cn_equity.adj_factor"]
    known = datetime(2026, 6, 1, 12, 0)  # 全部行在 as_of 之前可知
    with engine.begin() as connection:
        connection.execute(
            daily.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": day,
                    "close": float(index + 1),
                    "knowledge_time": known,
                    "ingest_time": known,
                    "provider": "tushare",
                    "version": 1,
                }
                for index, day in enumerate(days)
            ],
        )
        connection.execute(
            factor.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": day,
                    "adj_factor": 1.0,
                    "knowledge_time": known,
                    "ingest_time": known,
                    "provider": "tushare",
                    "version": 1,
                }
                for day in days
            ],
        )
    return engine, metadata, days


def _engine_for(engine):  # type: ignore[no-untyped-def]
    specs = load_all()
    assert import_implementations(specs) == []  # 触发 @register（含 ma20）
    return DerivedEngine(engine, specs=specs), specs


# ------------------------------------------------------------------ CI 与注册
def test_shipped_dictionary_and_registry_consistent() -> None:
    specs = load_all()
    assert validate_directory() == []
    assert check_consistency(specs) == []
    spec = DEFAULT_REGISTRY.get("ma20")
    assert spec is not None and spec.version == 1


# ------------------------------------------------------------------ 数值对账
def test_ma20_matches_pandas_rolling_and_skips_partial_windows(engine) -> None:  # type: ignore[no-untyped-def]
    import pandas as pd

    db, _metadata, days = engine
    derived, specs = _engine_for(db)
    result = derived.execute("ma20", as_of=AS_OF)

    rows = result.values.to_pylist()
    # 25 个交易日 → 仅 6 行具备完整 20 日窗口
    assert len(rows) == 6
    # SQLite 以文本回读日期、PG 以 date 回读：统一按 ISO 比较
    assert str(rows[0]["trade_date"]) == str(days[19])
    assert all(str(row["trade_date"]) >= str(days[19]) for row in rows)

    # 独立实现：访问面（hfq）+ pandas rolling(20, min_periods=20)
    adjusted = access_read(db, DATASET, ["close"], as_of=AS_OF).table.to_pandas()
    expected = (
        adjusted.sort_values("trade_date")
        .groupby("entity_id")["close"]
        .rolling(20, min_periods=20)
        .mean()
        .dropna()
    )
    actual = pd.Series([row["ma20"] for row in rows], index=range(len(rows)), dtype="float64")
    assert actual.to_list() == pytest.approx(expected.to_list(), rel=1e-12)


def test_ma20_respects_knowledge_time(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata, days = engine
    derived, _specs = _engine_for(db)
    before = [row["ma20"] for row in derived.execute("ma20", as_of=AS_OF).values.to_pylist()]

    # 最后一天的收盘价被重述（知识时间在 as_of 之后）→ 早于重述的 as_of 不受影响
    daily = metadata.tables[DATASET]
    with db.begin() as connection:
        connection.execute(
            daily.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": days[-1],
                    "close": 999.0,
                    "knowledge_time": datetime(2026, 7, 15, 12, 0),
                    "ingest_time": datetime(2026, 7, 15, 12, 0),
                    "provider": "tushare",
                    "version": 2,
                }
            ],
        )
    guarded = [row["ma20"] for row in derived.execute("ma20", as_of=AS_OF).values.to_pylist()]
    assert guarded == pytest.approx(before)


# ------------------------------------------------------------------ 端到端
def test_ma20_materialize_and_factor_read(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata, _days = engine
    store = InMemoryAlgorithmStore()
    specs = load_all()
    assert import_implementations(specs) == []
    DerivedEngine(db, specs=specs, store=store).materialize("ma20", as_of=utcnow())

    api = FactorAPI(db, specs=specs, store=store)
    result = api.read("ma20", as_of=utcnow())
    assert result.meta.materialized is True
    assert result.meta.algorithm_id == "ma20"
    assert result.meta.algorithm_version == 1
    assert result.meta.data_generation == store.get_generation("mart.derived_daily_bar_ma20")
    assert result.values.num_rows == 6
    assert api.catalog()[0].upstream_fingerprint  # 空上游集的稳定指纹
    assert api.catalog()[0].algorithm_version == 1


def test_materialized_read_rejects_version_drift(engine) -> None:  # type: ignore[no-untyped-def]
    """投影算法版本与当前登记不一致 → 结构化报错（不静默复用旧投影）。"""
    from fin_data_platform.derived.factors import ma20
    from fin_data_platform.derived.registry import AlgorithmRegistry, build_spec

    db, _metadata, _days = engine
    store = InMemoryAlgorithmStore()
    specs = load_all()
    assert import_implementations(specs) == []
    DerivedEngine(db, specs=specs, store=store).materialize("ma20", as_of=utcnow())

    bumped = AlgorithmRegistry()
    for registered in DEFAULT_REGISTRY:
        if registered.algorithm_id != "ma20":
            bumped.add(registered)
    bumped.add(build_spec(ma20, algorithm_id="ma20", version=2, owner="derived-engine"))
    api = FactorAPI(db, specs=specs, registry=bumped, store=store)
    with pytest.raises(FactorError, match="不一致"):
        api.read("ma20", as_of=utcnow())


def test_materialized_read_rejects_unknown_pin(engine) -> None:  # type: ignore[no-untyped-def]
    """未注册的 pin（含 id@vN 写法）→ 结构化报错，不静默复用投影。"""
    db, _metadata, _days = engine
    store = InMemoryAlgorithmStore()
    specs = load_all()
    assert import_implementations(specs) == []
    DerivedEngine(db, specs=specs, store=store).materialize("ma20", as_of=utcnow())

    api = FactorAPI(db, specs=specs, store=store)
    with pytest.raises(FactorError, match="未注册"):
        api.read("ma20", as_of=utcnow(), algorithm_id="ma20@v1")
