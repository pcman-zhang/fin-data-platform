"""因子依赖图（TASK-3.27）：构图 / 校验 / 任务门控 / 上游指纹。

链：m1（raw 输入，经访问面）→ m2（因子输入，读上游 latest 投影）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.derived.consistency import check_consistency
from fin_data_platform.derived.engine import DerivedEngine
from fin_data_platform.derived.errors import AsOfNotAligned, UpstreamStale
from fin_data_platform.derived.graph import FactorGraph
from fin_data_platform.derived.registry import AlgorithmRegistry, build_spec
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.derived.tasks import register_derived_tasks
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DerivedEntry, Materialize
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.storage.schema import build_metadata

DATASET = "cn_equity.daily_bar"
AS_OF = datetime(2026, 9, 15, 12, 0)


# ------------------------------------------------------------------ 测试算法
def m1_v1(inputs: Any, *, as_of: Any) -> Any:
    """一阶因子（测试用：取规范化收盘价）。

    Formula:
        m1 = close（默认口径，经访问面复权）

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤。
    """
    import pyarrow as pa

    daily = inputs[f"{DATASET}.close"]
    return pa.table(
        {
            "entity_id": daily.column("entity_id"),
            "trade_date": daily.column("trade_date"),
            "m1": daily.column("close"),
        }
    )


def m1_v2(inputs: Any, *, as_of: Any) -> Any:
    """一阶因子（升级版，测试用）。

    Formula:
        m1 = close（v2 占位，数值语义同 v1）

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤。
    """
    return m1_v1(inputs, as_of=as_of)


def m2_v1(inputs: Any, *, as_of: Any) -> Any:
    """二阶因子（测试用：读上游因子投影）。

    Formula:
        m2 = m1

    PIT:
        上游投影带知识锚（computed_at），读取按 as_of 对齐。
    """
    import pyarrow as pa

    upstream = inputs[f"{DATASET}.m1"]
    return pa.table(
        {
            "entity_id": upstream.column("entity_id"),
            "trade_date": upstream.column("trade_date"),
            "m2": upstream.column("m1"),
        }
    )


def empty_factor_v1(inputs: Any, *, as_of: Any) -> Any:
    """空结果因子（测试用）。

    Formula:
        0 行

    PIT:
        不读输入。
    """
    import pyarrow as pa

    return pa.table({"entity_id": [], "trade_date": [], "m1": []})


def _entries(
    *, m1_materialize: Materialize = Materialize.LATEST, upstream: str = "m1_v1"
) -> list[DerivedEntry]:
    return [
        DerivedEntry(
            output="m1",
            algorithm_id=upstream,
            implementation=f"{m1_v1.__module__}.{upstream}",
            owner="derived-engine",
            inputs=[f"{DATASET}.close"],
            description="一阶因子（测试）",
            materialize=m1_materialize,
        ),
        DerivedEntry(
            output="m2",
            algorithm_id="m2_v1",
            implementation=f"{m2_v1.__module__}.m2_v1",
            owner="derived-engine",
            inputs=[f"{DATASET}.m1"],
            description="二阶因子（测试）",
            materialize=Materialize.LATEST,
        ),
    ]


def _specs(**kwargs: Any):  # type: ignore[no-untyped-def]
    specs = load_all()
    dataset = specs[DATASET]
    altered = dict(specs)
    altered[DATASET] = dataset.model_copy(update={"derived": _entries(**kwargs)})
    return altered


def _registry(specs: dict[str, Any]) -> AlgorithmRegistry:  # type: ignore[no-untyped-def]
    registry = AlgorithmRegistry()
    registry.add(build_spec(m1_v1, algorithm_id="m1_v1"))
    registry.add(build_spec(m1_v2, algorithm_id="m1_v2"))
    registry.add(build_spec(m2_v1, algorithm_id="m2_v1"))
    return registry


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


# ------------------------------------------------------------------ 构图
def test_graph_edges_topology_and_fingerprint() -> None:
    specs = _specs()
    graph, errors = FactorGraph.from_dictionary(specs)
    assert errors == []
    m1 = (DATASET, "m1")
    m2 = (DATASET, "m2")

    assert graph.get(m1) is not None and graph.get(m2) is not None
    assert graph.get(m2).factor_inputs == (m1,)  # type: ignore[union-attr]
    assert graph.get(m1).data_inputs == (f"{DATASET}.close",)  # type: ignore[union-attr]
    assert graph.upstream_closure(m2) == [m1]
    assert graph.topological_order() == [m1, m2]

    # 上游算法升级 → 下游指纹变化
    before = graph.fingerprint(m2)
    upgraded, _errors = FactorGraph.from_dictionary(_specs(upstream="m1_v2"))
    assert upgraded.fingerprint(m2) != before


def test_graph_detects_cycle() -> None:
    specs = _specs()
    dataset = specs[DATASET]
    entries = _entries()
    entries[0] = entries[0].model_copy(update={"inputs": [f"{DATASET}.m2"]})
    entries[1] = entries[1].model_copy(update={"inputs": [f"{DATASET}.m1"]})
    altered = dict(specs)
    altered[DATASET] = dataset.model_copy(update={"derived": entries})

    graph, errors = FactorGraph.from_dictionary(altered)
    assert graph.cycles()
    assert any("成环" in error for error in errors)
    with pytest.raises(ValueError, match="成环"):
        graph.topological_order()


def test_consistency_flags_latest_upstream_constraint() -> None:
    specs = _specs(m1_materialize=Materialize.NONE)
    errors = check_consistency(specs, _registry(specs))
    assert any("latest 物化的下游要求上游因子" in error for error in errors)


def test_missing_factor_reference_and_output_collision() -> None:
    specs = _specs()
    dataset = specs[DATASET]
    entries = _entries()
    entries[1] = entries[1].model_copy(update={"inputs": [f"{DATASET}.ghost"]})
    altered = dict(specs)
    altered[DATASET] = dataset.model_copy(update={"derived": entries})
    _graph, errors = FactorGraph.from_dictionary(altered)
    assert any("输入字段不存在" in error for error in errors)


# ------------------------------------------------------------------ 任务门控
def test_derive_task_dependencies_wired(engine) -> None:  # type: ignore[no-untyped-def]
    from fin_data_platform.runtime.registry import TaskRegistry

    specs = _specs()
    registry = TaskRegistry()
    registered = register_derived_tasks(
        registry,
        engine,
        specs=specs,
        registry_algorithms=_registry(specs),
        store=InMemoryAlgorithmStore(),
    )
    by_id = {spec.job_id: spec for spec in registered}
    upstream = f"derive.{DATASET}.m1"
    downstream = f"derive.{DATASET}.m2"
    assert by_id[downstream].dependencies == (upstream,)
    assert by_id[upstream].dependencies == ()
    assert registry.validate() == []


def test_dependency_gating_and_chain_redispatch(engine) -> None:  # type: ignore[no-untyped-def]
    from fin_data_platform.runtime.app import RuntimeApp
    from fin_data_platform.runtime.config import RuntimeConfig
    from fin_data_platform.runtime.registry import TaskRegistry
    from fin_data_platform.runtime.repository import InMemoryMetaRepository
    from fin_data_platform.storage.config import StorageConfig

    specs = _specs()
    task_registry = TaskRegistry()
    registered = register_derived_tasks(
        task_registry,
        engine,
        specs=specs,
        registry_algorithms=_registry(specs),
        store=InMemoryAlgorithmStore(),
    )
    by_id = {spec.job_id: spec for spec in registered}
    repository = InMemoryMetaRepository()
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://"), worker_count=1),
        repository=repository,
        registry=task_registry,
    )
    app.sync_metadata()

    downstream_intent = task_registry.intent(
        by_id[f"derive.{DATASET}.m2"], window_start=None, window_end=None
    )
    # 上游未成功：下游被依赖门控拦截（Dispatcher 只计数不保留）
    assert app.submit(downstream_intent) == "dependency"

    upstream_intent = task_registry.intent(
        by_id[f"derive.{DATASET}.m1"], window_start=None, window_end=None
    )
    assert app.submit(upstream_intent) != "dependency"
    # 父成功 → 自动重投递下游（链式推进）：一轮 run_pending 完成 m1 + m2
    assert app.run_pending() == 2
    statuses = {run.job_id: run.status for run in repository.list_runs()}
    assert statuses[f"derive.{DATASET}.m1"] == "succeeded"
    assert statuses[f"derive.{DATASET}.m2"] == "succeeded"
    # 手动再投递：已完成，幂等去重
    assert app.submit(downstream_intent) == "duplicate"


# ------------------------------------------------------------------ 指纹与投影
def test_upstream_projection_fingerprint_and_stale_detection(engine) -> None:  # type: ignore[no-untyped-def]
    store = InMemoryAlgorithmStore()
    specs = _specs()
    derived = DerivedEngine(engine, specs=specs, registry=_registry(specs), store=store)

    first = derived.materialize("m1", as_of=utcnow())
    assert first.upstream_fingerprint == derived.upstream_fingerprint("m1")

    # 下游物化在（门控保证的）上游物化之后：as_of 取当前知识时点
    now = utcnow()
    second = derived.materialize("m2", as_of=now)
    graph, _errors = FactorGraph.from_dictionary(specs)
    assert second.upstream_fingerprint == graph.fingerprint((DATASET, "m2"))
    assert second.upstream_fingerprint != graph.fingerprint((DATASET, "m1"))

    # 上游升级（m1_v2）后：下游物化发现指纹不一致
    upgraded_specs = _specs(upstream="m1_v2")
    upgraded = DerivedEngine(
        engine, specs=upgraded_specs, registry=_registry(upgraded_specs), store=store
    )
    with pytest.raises(UpstreamStale, match="与当前登记不一致"):
        upgraded.materialize("m2", as_of=now)


def test_downstream_read_requires_aligned_upstream(engine) -> None:  # type: ignore[no-untyped-def]
    store = InMemoryAlgorithmStore()
    specs = _specs()
    derived = DerivedEngine(engine, specs=specs, registry=_registry(specs), store=store)
    derived.materialize("m1", as_of=utcnow())

    # as_of 早于上游投影知识锚（computed_at=now）→ 不对齐
    with pytest.raises(AsOfNotAligned):
        derived.execute("m2", as_of=datetime(2020, 1, 1, 12, 0))


# ------------------------------------------------------------------ 复审回归
def test_factor_input_respects_entity_and_window_filters(engine) -> None:  # type: ignore[no-untyped-def]
    store = InMemoryAlgorithmStore()
    specs = _specs()
    derived = DerivedEngine(engine, specs=specs, registry=_registry(specs), store=store)
    derived.materialize("m1", as_of=utcnow())

    now = utcnow()
    hit = derived.execute("m2", as_of=now, entity_ids=[1])
    assert hit.values.num_rows == 1
    # 实体不匹配 / 窗口不覆盖：过滤后为空（不得返回全量上游）
    miss_entity = derived.execute("m2", as_of=now, entity_ids=[999])
    assert miss_entity.values.num_rows == 0
    miss_window = derived.execute("m2", as_of=now, window=(date(2026, 9, 11), date(2026, 9, 11)))
    assert miss_window.values.num_rows == 0


def test_empty_result_materialize_does_not_crash(engine) -> None:  # type: ignore[no-untyped-def]
    specs = load_all()
    dataset = specs[DATASET]
    entries = _entries()
    entries[0] = entries[0].model_copy(
        update={
            "algorithm_id": "empty_factor_v1",
            "implementation": f"{empty_factor_v1.__module__}.empty_factor_v1",
        }
    )
    altered = dict(specs)
    altered[DATASET] = dataset.model_copy(update={"derived": entries})
    registry = _registry(altered)
    registry.add(build_spec(empty_factor_v1, algorithm_id="empty_factor_v1"))

    derived = DerivedEngine(
        engine, specs=altered, registry=registry, store=InMemoryAlgorithmStore()
    )
    report = derived.materialize("m1", as_of=utcnow())
    assert report.rows == 0
    assert report.upstream_fingerprint == derived.upstream_fingerprint("m1")
    # 空上游可继续参与下游读取（空表语义一致）
    downstream = derived.execute("m2", as_of=utcnow())
    assert downstream.values.num_rows == 0


def test_consistency_rejects_inline_sql_with_factor_input() -> None:
    specs = _specs()
    registry = _registry(specs)
    registry.add(build_spec(m2_v1, algorithm_id="m2_inline_v1", inline_sql="SELECT 1 AS m2"))
    dataset = specs[DATASET]
    entries = _entries()
    entries[1] = entries[1].model_copy(
        update={
            "algorithm_id": "m2_inline_v1",
            "implementation": f"{m2_v1.__module__}.m2_v1",
        }
    )
    altered = dict(specs)
    altered[DATASET] = dataset.model_copy(update={"derived": entries})
    errors = check_consistency(altered, registry, import_implementations_first=False)
    assert any("inline_sql 不支持因子输入" in error for error in errors)


def test_graph_rejects_duplicate_output_in_same_dataset() -> None:
    specs = _specs()
    dataset = specs[DATASET]
    entries = _entries()
    entries.append(entries[1].model_copy(update={"inputs": [f"{DATASET}.close"]}))
    altered = dict(specs)
    altered[DATASET] = dataset.model_copy(update={"derived": entries})
    _graph, errors = FactorGraph.from_dictionary(altered)
    assert any("derived.output 在同一数据集重复登记" in error for error in errors)
