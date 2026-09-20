"""派生引擎（TASK-3.12）：算法注册 / 字典一致性 / 登记与台账同步。"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pyarrow as pa
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.derived.consistency import (
    check_consistency,
    classify_algorithms,
    import_implementations,
    referenced_algorithms,
)
from fin_data_platform.derived.engine import (
    DerivedEngine,
    generation_stamp,
    projection_name,
)
from fin_data_platform.derived.inputs import input_view_name, normalize_as_of
from fin_data_platform.derived.registry import (
    AlgorithmRegistry,
    build_spec,
    register,
)
from fin_data_platform.derived.schema import metadata as derived_metadata
from fin_data_platform.derived.store import (
    AlgorithmEvent,
    AlgorithmRow,
    InMemoryAlgorithmStore,
    SqlAlgorithmStore,
)
from fin_data_platform.derived.sync import build_events, build_rows, sync_algorithms
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DerivedEntry, Materialize, Refresh
from fin_data_platform.storage.schema import build_metadata

ADJUSTED_CLOSE_SQL = f"""
SELECT d.entity_id,
       d.trade_date,
       d.close * f.adj_factor / a.adj_factor AS adjusted_close
FROM {input_view_name("cn_equity.daily_bar.close@raw")} AS d
JOIN {input_view_name("cn_equity.adj_factor.adj_factor@raw")} AS f
  ON f.entity_id = d.entity_id AND f.trade_date = d.trade_date
JOIN (
    SELECT entity_id, adj_factor
    FROM (
        SELECT entity_id,
               adj_factor,
               ROW_NUMBER() OVER (
                   PARTITION BY entity_id ORDER BY trade_date DESC
               ) AS _rank
        FROM {input_view_name("cn_equity.adj_factor.adj_factor@raw")}
    ) ranked
    WHERE _rank = 1
) AS a
  ON a.entity_id = d.entity_id
ORDER BY d.entity_id, d.trade_date
"""


def adjusted_close_v1(inputs: Any, *, as_of: Any) -> Any:
    """测试用前复权收盘价（生产字典不登记：复权组合属采集/读取层，doc-5）。

    Formula:
        adjusted_close = close × f / f_anchor

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤。
    """
    import duckdb

    connection = duckdb.connect()
    try:
        for ref, table in inputs.items():
            connection.register(input_view_name(ref), table)
        return connection.execute(ADJUSTED_CLOSE_SQL).to_arrow_table()
    finally:
        connection.close()


_ADJUSTED_SPEC = build_spec(
    adjusted_close_v1, algorithm_id="adjusted_close_v1", inline_sql=ADJUSTED_CLOSE_SQL
)


def _test_registry() -> AlgorithmRegistry:
    """测试注册表：当前算法 + 历史算法（永久保留，含升级事件元数据）。"""
    registry = AlgorithmRegistry()
    registry.add(_ADJUSTED_SPEC)
    registry.add(
        build_spec(
            legacy_close_v1,
            algorithm_id="legacy_close_v1",
            effective_from=date(2026, 9, 14),
            reason="被 adjusted_close_v1 取代：复权锚点口径统一",
        )
    )
    return registry


def legacy_close_v1(inputs: Any, *, as_of: Any) -> Any:
    """历史算法（测试用；模拟被新版本取代后仍永久保留的实现）。

    Formula:
        legacy_close = close

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤。
    """
    raise NotImplementedError


def conflicting_close_v1(inputs: Any, *, as_of: Any) -> Any:
    """同 id 冲突检测（测试用）。

    Formula:
        none

    PIT:
        none
    """
    raise NotImplementedError


def undocumented_v1(inputs: Any, *, as_of: Any) -> Any:
    """缺少必需标记（测试用）。"""
    raise NotImplementedError


def _dictionary_specs() -> dict[str, Any]:
    """shipped 字典 + 注入测试派生条目（生产字典已登记 ma20，此处仅补测试用条目）。"""
    specs = load_all()
    dataset = specs["cn_equity.daily_bar"]
    entry = DerivedEntry(
        output="adjusted_close",
        algorithm_id="adjusted_close_v1",
        implementation=f"{adjusted_close_v1.__module__}.adjusted_close_v1",
        owner="derived-engine",
        inputs=["cn_equity.daily_bar.close@raw", "cn_equity.adj_factor.adj_factor@raw"],
        description="测试用前复权收盘价（生产不登记：复权属采集/读取层组合）",
    )
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    return altered


# ------------------------------------------------------------------ 字典模型
def test_shipped_dictionary_derived_excludes_adjust_outputs() -> None:
    """复权组合属采集/读取层（doc-5）：shipped 字典的派生仅登记真正的计算（ma20/adx），
    不得把 qfq/hfq 组合登记为派生输出。"""
    specs = load_all()
    outputs = {
        entry.output for spec in specs.values() for entry in (spec.derived or [])
    }
    assert outputs == {"ma20", "adx"}
    assert all("qfq" not in name and "hfq" not in name for name in outputs)


def test_test_dictionary_derived_materialize_and_refresh_defaults() -> None:
    entry = _dictionary_specs()["cn_equity.daily_bar"].derived[0]
    assert entry.algorithm_id == "adjusted_close_v1"
    assert entry.materialize.value == "none"
    assert entry.refresh.value == "on_demand"


# ------------------------------------------------------------------ 一致性
def test_test_dictionary_consistency_passes() -> None:
    specs = _dictionary_specs()
    assert import_implementations(specs) == []
    assert check_consistency(specs, _test_registry()) == []
    reference = _ADJUSTED_SPEC
    assert reference.implementation.endswith(".adjusted_close_v1")
    assert "Formula" in reference.docstring and "PIT" in reference.docstring


def test_shipped_dictionary_consistent_with_registry() -> None:
    """生产字典（含 ma20）与代码注册表一致（CI 同口径）。"""
    assert check_consistency(load_all()) == []


def test_consistency_detects_unregistered_algorithm() -> None:
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    ghost = dataset.derived[0].model_copy(
        update={
            "algorithm_id": "ghost_v1",
            "implementation": "fin_data_platform.derived.price.ghost",
        }
    )
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [ghost]})
    errors = check_consistency(altered, _test_registry())
    assert any("算法未注册" in error and "ghost_v1" in error for error in errors)


def test_consistency_detects_implementation_and_owner_drift() -> None:
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    registry = AlgorithmRegistry()
    registry.add(build_spec(legacy_close_v1, algorithm_id="legacy_close_v1"))
    drifted = dataset.derived[0].model_copy(
        update={
            "algorithm_id": "legacy_close_v1",
            "implementation": "fin_data_platform.derived.ghost",
            "owner": "someone-else",
        }
    )
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [drifted]})
    errors = check_consistency(altered, registry, import_implementations_first=False)
    assert any("implementation 与注册不符" in error for error in errors)
    assert any("owner 与注册不符" in error for error in errors)


# ------------------------------------------------------------------ 注册表
def test_build_spec_rejects_invalid_id_and_version_mismatch() -> None:
    with pytest.raises(ValueError, match="命名非法"):
        build_spec(legacy_close_v1, algorithm_id="LegacyClose")
    with pytest.raises(ValueError, match="不一致"):
        build_spec(legacy_close_v1, algorithm_id="legacy_close_v1", version=2)
    with pytest.raises(ValueError, match="version"):
        build_spec(legacy_close_v1, algorithm_id="legacy_close")  # 稳定 id 需显式 version


def test_registry_stable_id_versions_coexist_and_resolve_latest() -> None:
    registry = AlgorithmRegistry()
    v1 = registry.add(build_spec(legacy_close_v1, algorithm_id="legacy_close", version=1))
    v2 = registry.add(build_spec(conflicting_close_v1, algorithm_id="legacy_close", version=2))

    assert v1.identity == "legacy_close@v1" and v2.identity == "legacy_close@v2"
    assert registry.get("legacy_close") == v2  # 缺省取最高版本
    assert registry.get("legacy_close", version=1) == v1  # 精确版本（历史 pin）
    assert registry.ids() == ["legacy_close"]
    assert len(registry) == 2


def test_registry_is_idempotent_and_conflict_safe() -> None:
    registry = AlgorithmRegistry()
    spec = build_spec(legacy_close_v1, algorithm_id="legacy_close_v1")
    assert registry.add(spec) is registry.add(spec)  # 重复导入幂等
    with pytest.raises(ValueError, match="冲突"):
        registry.add(build_spec(conflicting_close_v1, algorithm_id="legacy_close_v1"))


def test_registry_validate_flags_missing_docstring_markers() -> None:
    registry = AlgorithmRegistry()
    registry.add(build_spec(undocumented_v1, algorithm_id="undocumented_v1"))
    errors = registry.validate()
    assert any("docstring 缺少标记" in error for error in errors)


# ------------------------------------------------------------------ 登记行与同步


def test_build_rows_classifies_active_and_deprecated() -> None:
    specs = _dictionary_specs()
    registry = _test_registry()
    rows = {row.algorithm_id: row for row in build_rows(specs, registry)}
    active = rows["adjusted_close_v1"]
    assert active.status == "active"
    assert active.dataset == "cn_equity.daily_bar"
    assert active.output == "adjusted_close"
    assert active.inputs == (
        "cn_equity.daily_bar.close@raw",
        "cn_equity.adj_factor.adj_factor@raw",
    )
    deprecated = rows["legacy_close_v1"]
    assert deprecated.status == "deprecated"
    assert deprecated.dataset is None and deprecated.output is None
    assert classify_algorithms(specs, registry)["legacy_close_v1"] == "deprecated"


def test_sync_rejects_inconsistent_registry() -> None:
    with pytest.raises(ValueError, match="一致性校验失败"):
        sync_algorithms(InMemoryAlgorithmStore(), _dictionary_specs(), AlgorithmRegistry())


def test_sync_writes_rows_and_events_idempotently() -> None:
    specs = _dictionary_specs()
    registry = _test_registry()
    store = InMemoryAlgorithmStore()
    report = sync_algorithms(store, specs, registry)
    assert (report.total, report.active, report.deprecated) == (2, 1, 1)
    assert report.events == 1

    events = build_events(registry)
    assert len(events) == 1
    assert events[0].algorithm_id == "legacy_close_v1"
    assert "取代" in events[0].reason

    # 二次同步幂等：行数与事件数不变
    report_again = sync_algorithms(store, specs, registry)
    assert report_again.total == 2
    assert len(store.list_events()) == 1
    assert len(store.list_all()) == 2


@pytest.fixture()
def sql_algorithm_store() -> SqlAlgorithmStore:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS meta"))
    derived_metadata.create_all(engine)
    return SqlAlgorithmStore(engine)


def test_sql_store_roundtrip(sql_algorithm_store: SqlAlgorithmStore) -> None:
    store = sql_algorithm_store
    specs = _dictionary_specs()
    registry = _test_registry()
    report = sync_algorithms(store, specs, registry)
    assert report.total == 2

    rows = {row.algorithm_id: row for row in store.list_all()}
    assert rows["adjusted_close_v1"].status == "active"
    assert rows["adjusted_close_v1"].inputs == (
        "cn_equity.daily_bar.close@raw",
        "cn_equity.adj_factor.adj_factor@raw",
    )
    assert rows["legacy_close_v1"].status == "deprecated"

    # upsert 不删除历史 id；事件按 (algorithm_id, effective_from) 幂等
    assert (
        store.record_events([AlgorithmEvent("legacy_close_v1", date(2026, 9, 14), "重复写入")]) == 0
    )
    assert len(store.list_events()) == 1
    assert store.list_events()[0].reason == "被 adjusted_close_v1 取代：复权锚点口径统一"

    store.set_generation("mart.derived_daily_bar_adjusted_close", "20260914T000000Z")
    assert store.get_generation("mart.derived_daily_bar_adjusted_close") == "20260914T000000Z"
    assert store.get_generation("mart.missing") is None


def test_register_decorator_uses_custom_registry() -> None:
    registry = AlgorithmRegistry()
    register(algorithm_id="legacy_close_v1", registry=registry)(legacy_close_v1)
    register(algorithm_id="legacy_close_v1", registry=registry)(legacy_close_v1)
    assert registry.ids() == ["legacy_close_v1"]


def test_referenced_algorithms_index() -> None:
    referenced = referenced_algorithms(_dictionary_specs())
    assert set(referenced) >= {"adjusted_close_v1"}
    assert referenced["adjusted_close_v1"][0] == "cn_equity.daily_bar"


def test_rows_are_frozen_dataclasses() -> None:
    row = AlgorithmRow(
        algorithm_id="x_v1",
        version=1,
        owner="o",
        implementation="a.b",
        dataset=None,
        output=None,
        inputs=(),
        description="d",
        status="deprecated",
        effective_from=None,
    )
    with pytest.raises(FrozenInstanceError):
        row.status = "active"  # type: ignore[misc]


# ------------------------------------------------------------------ 切片 B：as-of 执行
AS_OF = datetime(2026, 9, 15, 12, 0)


@pytest.fixture()
def canonical_engine():  # type: ignore[no-untyped-def]
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
    return engine, metadata


def _seed(engine, metadata, dataset: str, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    table = metadata.tables[dataset]
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)


def _daily(entity: int, day: date, close: float, known: date, version: int = 1) -> dict[str, Any]:
    known_at = datetime(known.year, known.month, known.day, 12, 0)
    return {
        "entity_id": entity,
        "trade_date": day,
        "close": close,
        "knowledge_time": known_at,
        "ingest_time": known_at,
        "provider": "tushare",
        "version": version,
    }


def _factor(entity: int, day: date, value: float, known: date, version: int = 1) -> dict[str, Any]:
    known_at = datetime(known.year, known.month, known.day, 12, 0)
    return {
        "entity_id": entity,
        "trade_date": day,
        "adj_factor": value,
        "knowledge_time": known_at,
        "ingest_time": known_at,
        "provider": "tushare",
        "version": version,
    }


def test_engine_executes_adjusted_close_with_asof_guard(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    engine, metadata = canonical_engine
    _seed(
        engine,
        metadata,
        "cn_equity.daily_bar",
        [
            _daily(1, date(2026, 9, 10), 10.0, date(2026, 9, 10)),
            _daily(1, date(2026, 9, 11), 11.0, date(2026, 9, 11)),
            # 未来知识时间（as_of=09-15 时不可见；前视防护）
            _daily(1, date(2026, 9, 11), 99.0, date(2026, 9, 20)),
        ],
    )
    _seed(
        engine,
        metadata,
        "cn_equity.adj_factor",
        [
            _factor(1, date(2026, 9, 10), 1.0, date(2026, 9, 10)),
            _factor(1, date(2026, 9, 11), 2.0, date(2026, 9, 11)),
            _factor(1, date(2026, 9, 11), 4.0, date(2026, 9, 20)),
        ],
    )

    derived = DerivedEngine(engine, specs=_dictionary_specs(), registry=_test_registry())
    result = derived.execute("adjusted_close", as_of=AS_OF)

    assert result.dataset == "cn_equity.daily_bar"
    assert result.algorithm_id == "adjusted_close_v1"
    assert result.inputs_as_of == AS_OF
    assert result.data_generation is None
    rows = result.values.to_pylist()
    # trade_date 在 SQLite 以文本回读、PG 以 date 回读：统一按 ISO 比较
    assert [(str(row["trade_date"]), row["adjusted_close"]) for row in rows] == [
        ("2026-09-10", pytest.approx(5.0)),
        ("2026-09-11", pytest.approx(11.0)),
    ]


def test_engine_respects_knowledge_time_for_restatements(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    engine, metadata = canonical_engine
    _seed(
        engine,
        metadata,
        "cn_equity.daily_bar",
        [
            _daily(1, date(2026, 9, 10), 10.0, date(2026, 9, 10), version=1),
            _daily(1, date(2026, 9, 10), 12.0, date(2026, 9, 12), version=2),
        ],
    )
    _seed(
        engine,
        metadata,
        "cn_equity.adj_factor",
        [_factor(1, date(2026, 9, 10), 1.0, date(2026, 9, 10))],
    )
    derived = DerivedEngine(engine, specs=_dictionary_specs(), registry=_test_registry())

    # 重述前可见 v1；重述后取 v2（同业务键最高版本）
    early = derived.execute("adjusted_close", as_of=datetime(2026, 9, 11, 12, 0))
    assert early.values.to_pylist()[0]["adjusted_close"] == pytest.approx(10.0)
    late = derived.execute("adjusted_close", as_of=AS_OF)
    assert late.values.to_pylist()[0]["adjusted_close"] == pytest.approx(12.0)


def test_engine_applies_window_and_entity_filters(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    engine, metadata = canonical_engine
    rows = []
    factors = []
    for entity in (1, 2):
        for day in (date(2026, 9, 10), date(2026, 9, 11)):
            rows.append(_daily(entity, day, 10.0 + entity, day))
            factors.append(_factor(entity, day, 1.0, day))
    _seed(engine, metadata, "cn_equity.daily_bar", rows)
    _seed(engine, metadata, "cn_equity.adj_factor", factors)

    derived = DerivedEngine(engine, specs=_dictionary_specs(), registry=_test_registry())
    result = derived.execute(
        "adjusted_close",
        as_of=AS_OF,
        entity_ids=[1],
        window=(date(2026, 9, 11), date(2026, 9, 11)),
    )
    values = result.values.to_pylist()
    assert len(values) == 1
    assert values[0]["entity_id"] == 1
    assert str(values[0]["trade_date"]) == "2026-09-11"


def test_engine_pin_and_error_paths(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    engine, metadata = canonical_engine
    _seed(
        engine,
        metadata,
        "cn_equity.daily_bar",
        [_daily(1, date(2026, 9, 10), 10.0, date(2026, 9, 10))],
    )
    _seed(
        engine,
        metadata,
        "cn_equity.adj_factor",
        [_factor(1, date(2026, 9, 10), 1.0, date(2026, 9, 10))],
    )
    derived = DerivedEngine(engine, specs=_dictionary_specs(), registry=_test_registry())

    pinned = derived.execute("adjusted_close", as_of=AS_OF, algorithm_id="adjusted_close_v1")
    assert pinned.algorithm_id == "adjusted_close_v1"
    with pytest.raises(ValueError, match="算法未注册"):
        derived.execute("adjusted_close", as_of=AS_OF, algorithm_id="adjusted_close_v9")
    with pytest.raises(KeyError, match="派生输出不存在"):
        derived.execute("missing_output", as_of=AS_OF)


def bad_result_v1(inputs: Any, *, as_of: Any) -> Any:
    """输出列不合规（测试用）。

    Formula:
        无

    PIT:
        无
    """
    return pa.table({"wrong": [1]})


def test_engine_validates_algorithm_output(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    engine, metadata = canonical_engine
    _seed(
        engine,
        metadata,
        "cn_equity.daily_bar",
        [_daily(1, date(2026, 9, 10), 10.0, date(2026, 9, 10))],
    )
    _seed(
        engine,
        metadata,
        "cn_equity.adj_factor",
        [_factor(1, date(2026, 9, 10), 1.0, date(2026, 9, 10))],
    )
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(
        update={
            "output": "bad_result",
            "algorithm_id": "bad_result_v1",
            "implementation": f"{bad_result_v1.__module__}.bad_result_v1",
        }
    )
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    registry = AlgorithmRegistry()
    registry.add(build_spec(bad_result_v1, algorithm_id="bad_result_v1"))
    derived = DerivedEngine(engine, specs=altered, registry=registry)
    with pytest.raises(ValueError, match="输出缺少列"):
        derived.execute("bad_result", as_of=AS_OF)


def test_engine_reports_generation_for_latest_materialize(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    engine, metadata = canonical_engine
    _seed(
        engine,
        metadata,
        "cn_equity.daily_bar",
        [_daily(1, date(2026, 9, 10), 10.0, date(2026, 9, 10))],
    )
    _seed(
        engine,
        metadata,
        "cn_equity.adj_factor",
        [_factor(1, date(2026, 9, 10), 1.0, date(2026, 9, 10))],
    )
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(update={"materialize": Materialize.LATEST})
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    assert projection_name(altered["cn_equity.daily_bar"], entry) == (
        "mart.derived_daily_bar_adjusted_close"
    )

    store = InMemoryAlgorithmStore()
    store.set_generation("mart.derived_daily_bar_adjusted_close", "20260915T000000Z")
    derived = DerivedEngine(engine, specs=altered, store=store, registry=_test_registry())
    result = derived.execute("adjusted_close", as_of=AS_OF)
    assert result.data_generation == "20260915T000000Z"


def test_normalize_as_of_converts_aware_to_naive_utc() -> None:
    aware = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)
    assert normalize_as_of(aware) == datetime(2026, 9, 15, 20, 0)
    assert normalize_as_of(aware).tzinfo is None


# ------------------------------------------------------------------ 切片 C：计划 / 内联 / 物化
def adjusted_close_v2(inputs: Any, *, as_of: Any) -> Any:
    """前复权收盘价（升级示例：锚点改为固定 1.0，测试用）。

    Formula:
        adjusted_close = close（测试占位）

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤。
    """
    import pyarrow as pa

    daily = inputs["cn_equity.daily_bar.close@raw"]
    return pa.table(
        {
            "entity_id": daily.column("entity_id"),
            "trade_date": daily.column("trade_date"),
            "adjusted_close": daily.column("close"),
        }
    )


def _seed_simple(engine, metadata) -> None:  # type: ignore[no-untyped-def]
    _seed(
        engine,
        metadata,
        "cn_equity.daily_bar",
        [
            _daily(1, date(2026, 9, 10), 10.0, date(2026, 9, 10)),
            _daily(1, date(2026, 9, 11), 11.0, date(2026, 9, 11)),
        ],
    )
    _seed(
        engine,
        metadata,
        "cn_equity.adj_factor",
        [
            _factor(1, date(2026, 9, 10), 1.0, date(2026, 9, 10)),
            _factor(1, date(2026, 9, 11), 2.0, date(2026, 9, 11)),
        ],
    )


def test_plan_selects_service_form(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata = canonical_engine
    derived = DerivedEngine(engine, specs=_dictionary_specs(), registry=_test_registry())
    plan = derived.plan("adjusted_close")
    assert plan.service == "on_demand"
    assert plan.projection is None
    assert plan.inline_available is True
    assert plan.refresh == "on_demand"

    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(update={"materialize": Materialize.LATEST})
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    latest_engine = DerivedEngine(engine, specs=altered, registry=_test_registry())
    latest_plan = latest_engine.plan("adjusted_close")
    assert latest_plan.service == "materialize"
    assert latest_plan.projection == "mart.derived_daily_bar_adjusted_close"


def test_consistency_flags_missing_inline_views() -> None:
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(
        update={
            "algorithm_id": "legacy_close_v1",
            "implementation": f"{legacy_close_v1.__module__}.legacy_close_v1",
        }
    )
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    registry = AlgorithmRegistry()
    registry.add(
        build_spec(
            legacy_close_v1,
            algorithm_id="legacy_close_v1",
            inline_sql="SELECT 1 AS adjusted_close",
        )
    )
    errors = check_consistency(altered, registry, import_implementations_first=False)
    assert any("inline_sql 缺少输入视图" in error for error in errors)


def test_inline_sql_matches_execute(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    import pandas as pd
    from sqlalchemy import text as sql_text

    engine, metadata = canonical_engine
    _seed_simple(engine, metadata)
    derived = DerivedEngine(engine, specs=_dictionary_specs(), registry=_test_registry())

    inline = derived.inline_sql("adjusted_close", as_of=AS_OF)
    with engine.connect() as connection:
        inline_rows = connection.execute(sql_text(inline)).mappings().all()
    executed = derived.execute("adjusted_close", as_of=AS_OF).values.to_pylist()

    inline_frame = pd.DataFrame(inline_rows)
    assert len(inline_frame) == len(executed) == 2
    for index, row in enumerate(executed):
        assert str(inline_frame["trade_date"][index]) == str(row["trade_date"])
        assert inline_frame["adjusted_close"][index] == pytest.approx(row["adjusted_close"])


def test_materialize_single_projection_and_generation(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import inspect

    engine, metadata = canonical_engine
    _seed_simple(engine, metadata)
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(update={"materialize": Materialize.LATEST})
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    store = InMemoryAlgorithmStore()
    derived = DerivedEngine(engine, specs=altered, store=store, registry=_test_registry())

    report = derived.materialize("adjusted_close", as_of=AS_OF)
    assert report.projection == "mart.derived_daily_bar_adjusted_close"
    assert report.rows == 2
    assert report.algorithm_id == "adjusted_close_v1"
    assert store.get_generation(report.projection) == report.generation
    assert re.fullmatch(r"\d{8}T\d{6}Z", report.generation)

    inspector = inspect(engine)
    assert inspector.has_table("derived_daily_bar_adjusted_close", schema="mart")
    assert not inspector.has_table("derived_daily_bar_adjusted_close__next", schema="mart")
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(
                text("PRAGMA mart.table_info('derived_daily_bar_adjusted_close')")
            ).fetchall()
        }
    assert {"entity_id", "trade_date", "adjusted_close"} <= columns
    assert {"algorithm_id", "as_of", "computed_at", "data_generation"} <= columns

    # 重建：整体重算、只保留一份（影子表不残留）
    second = derived.materialize("adjusted_close", as_of=AS_OF)
    assert second.generation >= report.generation
    assert inspector.has_table("derived_daily_bar_adjusted_close", schema="mart")
    assert not inspector.has_table("derived_daily_bar_adjusted_close__next", schema="mart")


def test_materialize_requires_latest_and_store(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    engine, metadata = canonical_engine
    _seed_simple(engine, metadata)
    derived = DerivedEngine(engine, specs=_dictionary_specs(), registry=_test_registry())
    with pytest.raises(ValueError, match="不允许物化"):
        derived.materialize("adjusted_close", as_of=AS_OF)

    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(update={"materialize": Materialize.LATEST})
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    without_store = DerivedEngine(engine, specs=altered, registry=_test_registry())
    with pytest.raises(ValueError, match="AlgorithmStore"):
        without_store.materialize("adjusted_close", as_of=AS_OF)


def test_materialize_pin_records_algorithm_id(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    import pandas as pd
    from sqlalchemy import text as sql_text

    engine, metadata = canonical_engine
    _seed_simple(engine, metadata)
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(
        update={
            "algorithm_id": "adjusted_close_v2",
            "implementation": f"{adjusted_close_v2.__module__}.adjusted_close_v2",
            "materialize": Materialize.LATEST,
        }
    )
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    registry = AlgorithmRegistry()
    registry.add(_ADJUSTED_SPEC)  # 升级后旧实现永久保留（可 pin 复现）
    registry.add(
        build_spec(
            adjusted_close_v2,
            algorithm_id="adjusted_close_v2",
            effective_from=date(2026, 9, 15),
            reason="升级示例：口径调整",
        )
    )
    store = InMemoryAlgorithmStore()
    derived = DerivedEngine(engine, specs=altered, registry=registry, store=store)

    default = derived.materialize("adjusted_close", as_of=AS_OF)
    assert default.algorithm_id == "adjusted_close_v2"

    pinned = derived.materialize("adjusted_close", as_of=AS_OF, algorithm_id="adjusted_close_v1")
    assert pinned.algorithm_id == "adjusted_close_v1"
    with engine.connect() as connection:
        frame = pd.read_sql(
            sql_text("SELECT algorithm_id FROM mart.derived_daily_bar_adjusted_close"),
            connection,
        )
    assert set(frame["algorithm_id"]) == {"adjusted_close_v1"}


def test_generation_stamp_format() -> None:
    assert re.fullmatch(r"\d{8}T\d{6}Z", generation_stamp())


# ------------------------------------------------------------------ 切片 D：Runtime 挂载
def test_register_derived_tasks_only_latest(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    from fin_data_platform.derived.tasks import register_derived_tasks
    from fin_data_platform.runtime.registry import TaskRegistry

    engine, _metadata = canonical_engine
    store = InMemoryAlgorithmStore()
    # 生产字典的 latest 因子（ma20/adx）注册为任务；qfq 组合不登记（归访问面）
    production = register_derived_tasks(TaskRegistry(), engine, store=store)
    assert [spec.job_id for spec in production] == [
        "derive.cn_equity.daily_bar.ma20",
        "derive.cn_equity.daily_bar.adx",
    ]

    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(
        update={"materialize": Materialize.LATEST, "refresh": Refresh.SCHEDULED}
    )
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})

    registry = TaskRegistry()
    registered = register_derived_tasks(
        registry,
        engine,
        specs=altered,
        registry_algorithms=_test_registry(),
        store=store,
        schedule="30 9 * * *",
    )
    assert [spec.job_id for spec in registered] == ["derive.cn_equity.daily_bar.adjusted_close"]
    spec = registered[0]
    assert spec.kind == "derive"
    assert spec.scope == ""  # 因子身份由 job_id 承载（依赖门控要求上下游 scope 一致）
    assert spec.job_id.endswith(".adjusted_close")
    assert spec.schedule == "30 9 * * *"
    assert spec.version_provider is not None
    assert spec.version_provider() == "adjusted_close_v1@v1"  # 审计身份（id@vN）
    assert registry.validate() == []

    intent = registry.intent(spec, window_start=None, window_end=None)
    assert intent.version_dimension == "adjusted_close_v1@v1"
    assert intent.kind == "derive"

    # refresh=on_demand：注册但不挂调度（仅手动/API 触发）
    entries = [
        spec
        for spec in register_derived_tasks(
            TaskRegistry(), engine, specs=altered, registry_algorithms=_test_registry(), store=store
        )
    ]
    assert entries[0].schedule is None


def test_derived_task_executes_materialization(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import inspect

    from fin_data_platform.derived.tasks import register_derived_tasks
    from fin_data_platform.runtime.app import RuntimeApp
    from fin_data_platform.runtime.config import RuntimeConfig
    from fin_data_platform.runtime.registry import TaskRegistry
    from fin_data_platform.runtime.repository import InMemoryMetaRepository
    from fin_data_platform.storage.config import StorageConfig

    engine, metadata = canonical_engine
    _seed_simple(engine, metadata)
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(update={"materialize": Materialize.LATEST})
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})

    store = InMemoryAlgorithmStore()
    registry = TaskRegistry()
    registered = register_derived_tasks(
        registry, engine, specs=altered, registry_algorithms=_test_registry(), store=store
    )
    repository = InMemoryMetaRepository()
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://"), worker_count=1),
        repository=repository,
        registry=registry,
    )
    app.sync_metadata()
    app.submit(registry.intent(registered[0], window_start=None, window_end=None))
    assert app.run_pending() == 1

    runs = repository.list_runs()
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    assert runs[0].version_dimension == "adjusted_close_v1@v1"
    assert store.get_generation("mart.derived_daily_bar_adjusted_close") is not None
    assert inspect(engine).has_table("derived_daily_bar_adjusted_close", schema="mart")


def _latest_specs():  # type: ignore[no-untyped-def]
    specs = _dictionary_specs()
    dataset = specs["cn_equity.daily_bar"]
    entry = dataset.derived[0].model_copy(update={"materialize": Materialize.LATEST})
    altered = dict(specs)
    altered["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": [entry]})
    return altered


def _runtime_repository():  # type: ignore[no-untyped-def]
    from fin_data_platform.runtime.repository import InMemoryMetaRepository

    return InMemoryMetaRepository()


# ------------------------------------------------------------------ 复审修复
def test_daily_window_provider_uses_trigger_day() -> None:
    from fin_data_platform.derived.tasks import daily_window_provider

    assert daily_window_provider(datetime(2026, 9, 17, 3, 30)) == [
        (date(2026, 9, 17), date(2026, 9, 17))
    ]


def test_scheduled_derived_task_uses_window_provider(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    """scheduled 派生任务必须经 window_provider 提交（否则被 dedup/静默丢弃）。"""
    from fin_data_platform.derived.tasks import register_derived_tasks
    from fin_data_platform.runtime._util import utcnow
    from fin_data_platform.runtime.app import RuntimeApp
    from fin_data_platform.runtime.config import RuntimeConfig
    from fin_data_platform.runtime.registry import TaskRegistry
    from fin_data_platform.storage.config import StorageConfig

    engine, metadata = canonical_engine
    _seed_simple(engine, metadata)
    registry = TaskRegistry()
    registered = register_derived_tasks(
        registry,
        engine,
        specs=_latest_specs(),
        registry_algorithms=_test_registry(),
        store=InMemoryAlgorithmStore(),
        schedule="30 9 * * *",
    )
    repository = _runtime_repository()
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://"), worker_count=1),
        repository=repository,
        registry=registry,
    )
    spec = registered[0]
    today = utcnow().date()

    first = app._run_spec(spec)  # noqa: SLF001
    assert len(first) == 1 and first[0] != "duplicate"
    runs = repository.list_runs()
    assert len(runs) == 1
    assert (runs[0].window_start, runs[0].window_end) == (today, today)
    assert runs[0].version_dimension == "adjusted_close_v1@v1"

    # 同日再次触发：job_key 相同（幂等维度=触发日），Dispatcher 判定重复
    assert app._run_spec(spec) == ["duplicate"]  # noqa: SLF001
    assert len(repository.list_runs()) == 1

    # 次日触发：窗口推进 → 新运行并成功执行
    import fin_data_platform.runtime.app as runtime_app_module

    tomorrow = utcnow() + timedelta(days=1)
    original = runtime_app_module.utcnow
    runtime_app_module.utcnow = lambda: tomorrow
    try:
        second = app._run_spec(spec)  # noqa: SLF001
    finally:
        runtime_app_module.utcnow = original
    assert len(second) == 1 and second[0] != "duplicate"
    assert len(repository.list_runs()) == 2
    # 两个窗口（今日 + 次日）均待执行；同日重复触发未产生第三个运行
    assert app.run_pending() == 2
    assert {run.status for run in repository.list_runs()} == {"succeeded"}


def test_deprecated_algorithm_keeps_ownership_metadata() -> None:
    specs = _dictionary_specs()
    registry = _test_registry()
    store = InMemoryAlgorithmStore()
    sync_algorithms(store, specs, registry)
    before = {row.algorithm_id: row for row in store.list_all()}["adjusted_close_v1"]
    assert before.status == "active"

    dataset = specs["cn_equity.daily_bar"]
    retired = dict(specs)
    retired["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": None})
    sync_algorithms(store, retired, registry)

    after = {row.algorithm_id: row for row in store.list_all()}["adjusted_close_v1"]
    assert after.status == "deprecated"
    assert after.dataset == before.dataset == "cn_equity.daily_bar"
    assert after.output == before.output == "adjusted_close"
    assert after.inputs == before.inputs


def test_sql_store_preserves_ownership_on_deprecation(
    sql_algorithm_store: SqlAlgorithmStore,
) -> None:
    specs = _dictionary_specs()
    registry = _test_registry()
    sync_algorithms(sql_algorithm_store, specs, registry)

    dataset = specs["cn_equity.daily_bar"]
    retired = dict(specs)
    retired["cn_equity.daily_bar"] = dataset.model_copy(update={"derived": None})
    sync_algorithms(sql_algorithm_store, retired, registry)

    row = {item.algorithm_id: item for item in sql_algorithm_store.list_all()}["adjusted_close_v1"]
    assert row.status == "deprecated"
    assert row.dataset == "cn_equity.daily_bar"
    assert row.output == "adjusted_close"
    assert row.inputs == (
        "cn_equity.daily_bar.close@raw",
        "cn_equity.adj_factor.adj_factor@raw",
    )


def test_derived_task_invalidates_domain_cache(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    from fin_data_platform.derived.tasks import register_derived_tasks
    from fin_data_platform.runtime.registry import TaskRegistry

    class RecordingCache:
        def __init__(self) -> None:
            self.domains: list[str] = []

        def invalidate_domain(self, domain: str) -> None:
            self.domains.append(domain)

    engine, metadata = canonical_engine
    _seed_simple(engine, metadata)
    cache = RecordingCache()
    registry = TaskRegistry()
    registered = register_derived_tasks(
        registry,
        engine,
        specs=_latest_specs(),
        registry_algorithms=_test_registry(),
        store=InMemoryAlgorithmStore(),
        cache=cache,  # type: ignore[arg-type]
    )
    registered[0].executor(None)  # type: ignore[arg-type]
    assert cache.domains == ["cn_equity"]


def test_materialize_normalizes_aware_as_of(canonical_engine) -> None:  # type: ignore[no-untyped-def]
    import pandas as pd
    from sqlalchemy import text as sql_text

    engine, metadata = canonical_engine
    _seed_simple(engine, metadata)
    store = InMemoryAlgorithmStore()
    derived = DerivedEngine(engine, specs=_latest_specs(), registry=_test_registry(), store=store)
    aware = datetime(2026, 9, 16, 4, 0, tzinfo=timezone(timedelta(hours=8)))
    derived.materialize("adjusted_close", as_of=aware)

    with engine.connect() as connection:
        frame = pd.read_sql(
            sql_text("SELECT DISTINCT as_of FROM mart.derived_daily_bar_adjusted_close"),
            connection,
        )
    values = {str(item) for item in frame["as_of"]}
    assert len(values) == 1
    stored = next(iter(values))
    assert stored.startswith("2026-09-15 20:00:00")
