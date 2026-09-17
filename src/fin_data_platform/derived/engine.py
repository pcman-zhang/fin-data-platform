"""派生执行引擎（doc-10 §3.5）。

- **解析 / 计划**：``output`` → ``(dataset, DerivedEntry)``；:meth:`DerivedEngine.plan`
  按字典选择服务形态（``none`` → 按需计算 / 读模型内联；``latest`` → 单份投影）；
- **执行（按需）**：as-of 读取输入（:mod:`fin_data_platform.derived.inputs`）→ 调用算法
  （Arrow 输入 / 输出；计算实现可用 DuckDB）→ 结果列校验；
- **内联（读模型字段级）**：算法提供 SQL 模板（``@register(inline_sql=...)``，输入以
  ``input_view_name`` 命名）；引擎把每个输入渲染为 as-of CTE，产出可嵌入读模型视图的
  独立 SQL（与计算路径共用同一模板，避免两套语义）；
- **物化（latest）**：单份投影 ``mart.derived_<表>_<output>``，影子表重建 + 事务内原子
  换名，代次写入 ``meta.data_generation``（doc-12：``YYYYMMDDTHHMMSSZ``）；不落多版本；
- **PIT 双维**：``as_of``（输入知识时点）× ``algorithm_id``（默认字典 active，可 pin
  历史版本复现）；结果携带 ``algorithm_id / inputs_as_of / data_generation``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, text

from fin_data_platform.access import normalize_as_of
from fin_data_platform.derived.consistency import check_consistency
from fin_data_platform.derived.errors import UpstreamStale
from fin_data_platform.derived.graph import FactorGraph
from fin_data_platform.derived.inputs import (
    inline_input_sql,
    input_view_name,
    read_factor_projection_meta,
    read_inputs,
)
from fin_data_platform.derived.registry import DEFAULT_REGISTRY, AlgorithmRegistry
from fin_data_platform.derived.store import AlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec, DerivedEntry
from fin_data_platform.runtime._util import utcnow

if TYPE_CHECKING:
    import pyarrow as pa


def projection_name(spec: DatasetSpec, entry: DerivedEntry) -> str:
    """``latest`` 投影表名：``mart.derived_<表名>_<output>``（单份、可重建）。"""
    table = spec.storage.canonical_table.split(".", 1)[-1]
    return f"mart.derived_{table}_{entry.output}"


def generation_stamp(now: datetime | None = None) -> str:
    """代次格式（doc-12）：``YYYYMMDDTHHMMSSZ``。"""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True, slots=True)
class DerivedPlan:
    """服务形态计划（读模型内联 / 按需计算 / 单份物化）。"""

    dataset: str
    output: str
    algorithm_id: str
    materialize: str
    refresh: str
    service: str
    projection: str | None
    inline_available: bool


@dataclass(frozen=True, slots=True)
class DerivedResult:
    """按需计算结果（附 PIT 双维审计元数据）。"""

    dataset: str
    output: str
    algorithm_id: str
    inputs_as_of: datetime
    data_generation: str | None
    values: pa.Table


@dataclass(frozen=True, slots=True)
class MaterializeReport:
    """``latest`` 物化结果（单份投影 + 新代次 + 上游指纹）。"""

    projection: str
    generation: str
    rows: int
    algorithm_id: str
    inputs_as_of: datetime
    upstream_fingerprint: str = ""


class DerivedEngine:
    """派生引擎（字典 + 算法注册表驱动；不落多版本派生数据）。"""

    def __init__(
        self,
        engine: Engine,
        *,
        specs: Mapping[str, DatasetSpec] | None = None,
        registry: AlgorithmRegistry | None = None,
        store: AlgorithmStore | None = None,
    ) -> None:
        self._engine = engine
        self._specs: Mapping[str, DatasetSpec] = specs if specs is not None else load_all()
        self._registry = registry if registry is not None else DEFAULT_REGISTRY
        self._store = store
        errors = check_consistency(self._specs, self._registry)
        if errors:
            raise ValueError("派生引擎初始化失败：" + "；".join(errors))
        self._graph, _graph_errors = FactorGraph.from_dictionary(self._specs)

    @property
    def specs(self) -> Mapping[str, DatasetSpec]:
        return self._specs

    def resolve(
        self, output: str, dataset: str | None = None
    ) -> tuple[str, DatasetSpec, DerivedEntry]:
        """按输出名解析派生定义（跨数据集重名时必须显式指定 dataset）。"""
        matches: list[tuple[str, DatasetSpec, DerivedEntry]] = []
        for name, spec in sorted(self._specs.items()):
            if dataset is not None and name != dataset:
                continue
            for entry in spec.derived or []:
                if entry.output == output:
                    matches.append((name, spec, entry))
        if not matches:
            scope = f"{dataset}." if dataset else ""
            raise KeyError(f"派生输出不存在：{scope}{output}")
        if len(matches) > 1:
            candidates = ", ".join(name for name, _spec, _entry in matches)
            raise ValueError(
                f"派生输出 {output} 在多个数据集定义，请显式指定 dataset：{candidates}"
            )
        return matches[0]

    def _algorithm(self, algorithm_id: str) -> Any:
        algorithm = self._registry.get(algorithm_id)
        if algorithm is None:
            raise ValueError(f"算法未注册：{algorithm_id}（历史 id 永久保留，不得删除）")
        return algorithm

    def upstream_fingerprint(self, output: str, *, dataset: str | None = None) -> str:
        """目标因子的上游算法指纹（传递上游 algorithm_id 集合哈希）。"""
        name, _spec, entry = self.resolve(output, dataset)
        return self._graph.fingerprint((name, entry.output))

    def _check_upstreams(self, name: str, output: str) -> None:
        """物化前置：上游 latest 因子必须已物化，且指纹与当前算法一致。"""
        for upstream in self._graph.upstream_closure((name, output)):
            node = self._graph.get(upstream)
            if node is None:  # 引用未登记输出：一致性校验已在初始化拒绝
                continue
            stored_algorithm, stored_fingerprint, _generation = read_factor_projection_meta(
                self._engine, upstream[0], upstream[1], specs=self._specs
            )
            if stored_algorithm is None:  # 上游投影为空：无值可校验（合法状态）
                continue
            expected_fingerprint = self._graph.fingerprint(upstream)
            if stored_algorithm != node.algorithm_id or stored_fingerprint != expected_fingerprint:
                raise UpstreamStale(
                    f"上游因子 {upstream[0]}.{upstream[1]} 与当前登记不一致"
                    f"（投影算法 {stored_algorithm}/{stored_fingerprint}，"
                    f"当前 {node.algorithm_id}/{expected_fingerprint}）",
                    hint="上游算法已升级或输入变更：先重算上游因子再物化下游",
                )

    def plan(self, output: str, *, dataset: str | None = None) -> DerivedPlan:
        """按字典选择服务形态（不执行计算）。"""
        name, spec, entry = self.resolve(output, dataset)
        algorithm = self._registry.get(entry.algorithm_id)
        materialize = entry.materialize.value
        return DerivedPlan(
            dataset=name,
            output=entry.output,
            algorithm_id=entry.algorithm_id,
            materialize=materialize,
            refresh=entry.refresh.value,
            service="materialize" if materialize == "latest" else "on_demand",
            projection=projection_name(spec, entry) if materialize == "latest" else None,
            inline_available=bool(algorithm is not None and algorithm.inline_sql),
        )

    def execute(
        self,
        output: str,
        *,
        as_of: datetime,
        dataset: str | None = None,
        algorithm_id: str | None = None,
        entity_ids: Sequence[int] | None = None,
        window: tuple[date, date] | None = None,
    ) -> DerivedResult:
        """按需计算：``as_of`` 输入 × （默认 active 或 pin 的）``algorithm_id``。"""
        name, spec, entry = self.resolve(output, dataset)
        pinned = algorithm_id or entry.algorithm_id
        algorithm = self._algorithm(pinned)
        inputs = read_inputs(
            self._engine,
            entry.inputs,
            as_of=as_of,
            specs=self._specs,
            entity_ids=entity_ids,
            window=window,
        )
        values = algorithm.function(inputs, as_of=as_of)
        _validate_result(values, spec, entry)
        generation = None
        if self._store is not None and entry.materialize.value == "latest":
            generation = self._store.get_generation(projection_name(spec, entry))
        return DerivedResult(
            dataset=name,
            output=entry.output,
            algorithm_id=pinned,
            inputs_as_of=as_of,
            data_generation=generation,
            values=values,
        )

    def inline_sql(
        self,
        output: str,
        *,
        as_of: datetime,
        dataset: str | None = None,
        window: tuple[date, date] | None = None,
    ) -> str:
        """读模型内联：渲染算法 SQL 模板（每个输入以 as-of CTE 注入）。

        产出为独立 SQL（字面量参数），可直接作为读模型视图定义（doc-10 §3.5
        "读模型内联字段级"）；与 :meth:`execute` 共用同一 SQL 模板，语义一致。
        """
        _name, _spec, entry = self.resolve(output, dataset)
        algorithm = self._algorithm(entry.algorithm_id)
        if not algorithm.inline_sql:
            raise ValueError(f"算法 {entry.algorithm_id} 未提供 inline_sql，读模型内联不可用")
        ctes: list[str] = []
        for ref in dict.fromkeys(entry.inputs):
            sql = inline_input_sql(ref, as_of=as_of, specs=self._specs, window=window)
            ctes.append(f"{input_view_name(ref)} AS (\n{sql}\n)")
        body = algorithm.inline_sql.strip().rstrip(";")
        return "WITH\n" + ",\n".join(ctes) + "\n" + body + ";\n"

    def materialize(
        self,
        output: str,
        *,
        as_of: datetime,
        dataset: str | None = None,
        algorithm_id: str | None = None,
        entity_ids: Sequence[int] | None = None,
        window: tuple[date, date] | None = None,
    ) -> MaterializeReport:
        """``latest`` 物化：全量重算 → 影子表 → 原子换名 → 新代次。

        投影视为**可重建缓存**（Cache Never Owns Data）：每次物化整体重算，
        只保留一份；算法升级即 pin 新 ``algorithm_id`` 重跑换代次。
        """
        name, spec, entry = self.resolve(output, dataset)
        if entry.materialize.value != "latest":
            raise ValueError(
                f"{name}.{entry.output} 的 materialize={entry.materialize.value}，"
                "不允许物化（仅 latest）"
            )
        if self._store is None:
            raise ValueError("物化需要 AlgorithmStore（记录 meta.data_generation）")
        self._check_upstreams(name, entry.output)
        result = self.execute(
            output,
            as_of=as_of,
            dataset=dataset,
            algorithm_id=algorithm_id,
            entity_ids=entity_ids,
            window=window,
        )
        projection = projection_name(spec, entry)
        generation = generation_stamp()
        fingerprint = self.upstream_fingerprint(entry.output, dataset=name)
        frame = result.values.to_pandas()
        frame["algorithm_id"] = result.algorithm_id
        frame["as_of"] = normalize_as_of(as_of)
        frame["computed_at"] = utcnow()
        frame["data_generation"] = generation
        frame["upstream_fingerprint"] = fingerprint

        schema_name, _, table_name = projection.rpartition(".")
        shadow = f"{table_name}__next"
        frame.to_sql(
            shadow,
            self._engine,
            schema=schema_name,
            if_exists="replace",
            index=False,
            chunksize=1000,
        )
        with self._engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {projection}"))
            connection.execute(text(f"ALTER TABLE {schema_name}.{shadow} RENAME TO {table_name}"))
        self._store.set_generation(projection, generation)
        return MaterializeReport(
            projection=projection,
            generation=generation,
            rows=result.values.num_rows,
            algorithm_id=result.algorithm_id,
            inputs_as_of=as_of,
            upstream_fingerprint=fingerprint,
        )


def _validate_result(values: Any, spec: DatasetSpec, entry: DerivedEntry) -> None:
    import pyarrow as pa

    if not isinstance(values, pa.Table):
        raise TypeError(
            f"算法 {entry.algorithm_id} 必须返回 pyarrow.Table，收到 {type(values).__name__}"
        )
    required = [*spec.business_key, entry.output]
    missing = [column for column in required if column not in values.column_names]
    if missing:
        raise ValueError(
            f"算法 {entry.algorithm_id} 输出缺少列 {missing}（必须包含业务键与 output）"
        )
