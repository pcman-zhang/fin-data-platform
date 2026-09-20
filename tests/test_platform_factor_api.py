"""Factor API（TASK-3.25）：两态读取 / 按需子图求值 / 结构化异常 / 零写入。

复用 ``test_platform_factor_graph`` 的测试算法与夹具（m1 → m2 链）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from fin_data_platform.derived.errors import (
    AsOfNotAligned,
    WindowNotCovered,
)
from fin_data_platform.derived.factor_api import FactorAPI
from fin_data_platform.dictionary.models import Materialize
from fin_data_platform.runtime._util import utcnow
from test_platform_factor_graph import (
    DATASET,
    _entries,
    _registry,
    _specs,
    m1_v1,
    m2_v1,
)

CALLS: dict[str, int] = {}


@pytest.fixture()
def engine():  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool

    from fin_data_platform.storage.schema import build_metadata

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
    daily = metadata.tables[DATASET]
    factor = metadata.tables["cn_equity.adj_factor"]
    with engine.begin() as connection:
        connection.execute(
            daily.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": date(2026, 9, 10),
                    "close": 10.0,
                    "knowledge_time": datetime(2026, 9, 10, 12, 0),
                    "ingest_time": datetime(2026, 9, 10, 12, 0),
                    "provider": "tushare",
                    "version": 1,
                }
            ],
        )
        connection.execute(
            factor.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": date(2026, 9, 10),
                    "adj_factor": 1.0,
                    "knowledge_time": datetime(2026, 9, 10, 12, 0),
                    "ingest_time": datetime(2026, 9, 10, 12, 0),
                    "provider": "tushare",
                    "version": 1,
                }
            ],
        )
    return engine


def counting_m1_v1(inputs: Any, *, as_of: Any) -> Any:
    """计数版一阶因子（验证单请求 memo）。

    Formula:
        m1 = close

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤。
    """
    CALLS["m1"] = CALLS.get("m1", 0) + 1
    return m1_v1(inputs, as_of=as_of)


def m3_v1(inputs: Any, *, as_of: Any) -> Any:
    """菱形节点（依赖 m1 与 m2；测试单请求 memo）。

    Formula:
        m3 = m1 + m2

    PIT:
        上游由引擎按 as_of 求值（物化优先）。
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    left = inputs[f"{DATASET}.m1"]
    right = inputs[f"{DATASET}.m2"]
    return pa.table(
        {
            "entity_id": left.column("entity_id"),
            "trade_date": left.column("trade_date"),
            "m3": pc.add(left.column("m1"), right.column("m2")),
        }
    )


def _on_demand_specs(m3: bool = False):  # type: ignore[no-untyped-def]
    """全按需（materialize=none）链；可选加菱形节点 m3。

    m1 的实现替换为计数版（验证 memo：菱形下只算一次）。
    """
    from fin_data_platform.dictionary.models import DerivedEntry

    specs = _specs()
    dataset = specs[DATASET]
    entries = [entry.model_copy(update={"materialize": Materialize.NONE}) for entry in _entries()]
    entries[0] = entries[0].model_copy(
        update={"implementation": f"{counting_m1_v1.__module__}.counting_m1_v1"}
    )
    if m3:
        entries.append(
            DerivedEntry(
                output="m3",
                algorithm_id="m3_v1",
                implementation=f"{m3_v1.__module__}.m3_v1",
                owner="derived-engine",
                inputs=[f"{DATASET}.m1", f"{DATASET}.m2"],
                description="菱形节点（测试 memo）",
                materialize=Materialize.NONE,
            )
        )
    altered = dict(specs)
    altered[DATASET] = dataset.model_copy(update={"derived": entries})
    return altered


def _registry_for(specs: dict[str, Any]):  # type: ignore[no-untyped-def]
    from fin_data_platform.derived.registry import AlgorithmRegistry, build_spec

    registry = AlgorithmRegistry()
    registry.add(build_spec(counting_m1_v1, algorithm_id="m1_v1"))
    registry.add(build_spec(m1_v1, algorithm_id="m1_v2"))
    registry.add(build_spec(m2_v1, algorithm_id="m2_v1"))
    registry.add(build_spec(m3_v1, algorithm_id="m3_v1"))
    return registry


# ------------------------------------------------------------------ 按需态
def test_on_demand_chain_subgraph_and_memo(engine) -> None:  # type: ignore[no-untyped-def]
    CALLS.clear()
    specs = _on_demand_specs(m3=True)
    api = FactorAPI(engine, specs=specs, registry=_registry_for(specs))

    result = api.read("m3", as_of=utcnow())
    assert result.meta.materialized is False
    assert result.values.num_rows == 1
    # 菱形依赖：m1 只计算一次（单请求 memo）
    assert CALLS["m1"] == 1


def test_mixed_chain_prefers_materialized_upstream(engine) -> None:  # type: ignore[no-untyped-def]
    from fin_data_platform.derived.engine import DerivedEngine
    from fin_data_platform.derived.store import InMemoryAlgorithmStore

    store = InMemoryAlgorithmStore()
    specs = _specs()  # m1/m2 均 latest
    registry = _registry(specs)
    engine_impl = DerivedEngine(engine, specs=specs, registry=registry, store=store)
    engine_impl.materialize("m1", as_of=utcnow())

    # m2 改成按需：读取时应走 m1 的物化投影（不再递归计算）
    dataset = specs[DATASET]
    entries = [entry.model_copy(update={}) for entry in _entries()]
    entries[1] = entries[1].model_copy(update={"materialize": Materialize.NONE})
    mixed = dict(specs)
    mixed[DATASET] = dataset.model_copy(update={"derived": entries})
    api = FactorAPI(engine, specs=mixed, registry=registry, store=store)

    result = api.read("m2", as_of=utcnow())
    assert result.meta.materialized is False
    assert result.values.num_rows == 1


def test_on_demand_pin_and_unknown(engine) -> None:  # type: ignore[no-untyped-def]
    specs = _on_demand_specs()
    api = FactorAPI(engine, specs=specs, registry=_registry_for(specs))
    pinned = api.read("m2", as_of=utcnow(), algorithm_id="m2_v1")
    assert pinned.meta.algorithm_id == "m2_v1"
    with pytest.raises(ValueError, match="算法未注册"):
        api.read("m2", as_of=utcnow(), algorithm_id="m2_v9")


# ------------------------------------------------------------------ 物化态
def test_materialized_read_metadata_alignment_and_coverage(engine) -> None:  # type: ignore[no-untyped-def]
    from fin_data_platform.derived.engine import DerivedEngine
    from fin_data_platform.derived.store import InMemoryAlgorithmStore

    store = InMemoryAlgorithmStore()
    specs = _specs()
    registry = _registry(specs)
    DerivedEngine(engine, specs=specs, registry=registry, store=store).materialize(
        "m1", as_of=utcnow()
    )
    api = FactorAPI(engine, specs=specs, registry=registry, store=store)

    result = api.read("m1", as_of=utcnow())
    assert result.meta.materialized is True
    assert result.meta.data_generation == store.get_generation("mart.derived_daily_bar_m1")
    assert result.meta.computed_at is not None

    # as_of 早于知识锚 → 不对齐
    with pytest.raises(AsOfNotAligned):
        api.read("m1", as_of=datetime(2020, 1, 1, 12, 0))
    # 窗口超出投影覆盖 → 明确报错
    with pytest.raises(WindowNotCovered, match="覆盖"):
        api.read("m1", as_of=utcnow(), window=(date(2026, 9, 1), date(2026, 9, 30)))
    # 窗口在覆盖内 → 过滤返回
    inside = api.read("m1", as_of=utcnow(), window=(date(2026, 9, 10), date(2026, 9, 10)))
    assert inside.values.num_rows == 1


# ------------------------------------------------------------------ 零写入
def test_read_path_never_writes(engine) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import inspect

    from fin_data_platform.derived.store import InMemoryAlgorithmStore

    store = InMemoryAlgorithmStore()
    specs = _on_demand_specs()
    api = FactorAPI(engine, specs=specs, registry=_registry_for(specs), store=store)
    before_tables = set(inspect(engine).get_table_names(schema="mart"))
    before_generation = store.get_generation("mart.derived_daily_bar_m1")

    api.read("m2", as_of=utcnow())

    assert set(inspect(engine).get_table_names(schema="mart")) == before_tables
    assert store.get_generation("mart.derived_daily_bar_m1") == before_generation


# ------------------------------------------------------------------ 清单
def test_catalog_lists_factors_and_fingerprints(engine) -> None:  # type: ignore[no-untyped-def]
    specs = _specs()
    api = FactorAPI(engine, specs=specs, registry=_registry(specs))
    rows = {row.output: row for row in api.catalog()}
    assert set(rows) == {"m1", "m2"}
    assert rows["m2"].materialize == "latest"
    assert rows["m1"].inputs == (f"{DATASET}.close",)
    assert rows["m1"].upstream_fingerprint != rows["m2"].upstream_fingerprint


# ------------------------------------------------------------------ 复审回归
def test_materialized_read_rejects_pin_mismatch(engine) -> None:  # type: ignore[no-untyped-def]
    """物化投影 + pin 不一致必须报错（不得静默返回旧版本数据）。"""
    from fin_data_platform.derived.engine import DerivedEngine
    from fin_data_platform.derived.errors import FactorError
    from fin_data_platform.derived.store import InMemoryAlgorithmStore

    store = InMemoryAlgorithmStore()
    specs = _specs()
    registry = _registry(specs)  # 含 m1_v1 与 m1_v2
    DerivedEngine(engine, specs=specs, registry=registry, store=store).materialize(
        "m1", as_of=utcnow()
    )
    api = FactorAPI(engine, specs=specs, registry=registry, store=store)

    # pin 命中投影算法：正常
    assert api.read("m1", as_of=utcnow(), algorithm_id="m1_v1").meta.algorithm_id == "m1_v1"
    # pin 与投影不一致：报错（此前会被静默忽略）
    with pytest.raises(FactorError, match="与 pin"):
        api.read("m1", as_of=utcnow(), algorithm_id="m1_v2")


def test_mixed_chain_falls_back_when_upstream_not_materialized(engine) -> None:  # type: ignore[no-untyped-def]
    """上游 latest 未物化：按需下游回落到递归计算（doc-21「优先读投影，否则递归」）。"""
    specs = _specs()
    dataset = specs[DATASET]
    entries = _entries()
    entries[1] = entries[1].model_copy(update={"materialize": Materialize.NONE})
    mixed = dict(specs)
    mixed[DATASET] = dataset.model_copy(update={"derived": entries})
    api = FactorAPI(engine, specs=mixed, registry=_registry(mixed))

    result = api.read("m2", as_of=utcnow())
    assert result.meta.materialized is False
    assert result.values.num_rows == 1
