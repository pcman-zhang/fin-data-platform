"""Factor API（doc-21；TASK-3.25）：两态读取 + 按需子图求值。

- **物化态**（``materialize=latest``）：读单份投影；严格 ``as_of`` 对齐
  （``computed_at <= as_of``）、窗口覆盖校验、实体/窗口过滤，返回审计元数据；
- **按需态**（``materialize=none``）：子图求值（``FactorGraph`` 拓扑序 + 单请求 memo），
  上游若为 latest 则优先读投影（同样对齐校验），否则递归计算；
- **读路径永不写库**（回填/物化是控制面意图，见 TASK-3.26）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from fin_data_platform.derived.engine import DerivedEngine
from fin_data_platform.derived.errors import FactorError, FactorNotMaterialized
from fin_data_platform.derived.graph import FactorGraph, FactorId
from fin_data_platform.derived.inputs import read_inputs, read_projection_frame
from fin_data_platform.derived.registry import DEFAULT_REGISTRY, AlgorithmRegistry
from fin_data_platform.derived.store import AlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec

if TYPE_CHECKING:
    import pyarrow as pa


@dataclass(frozen=True, slots=True)
class FactorMeta:
    """因子读取元数据（审计三件套 + 物化状态）。"""

    dataset: str
    output: str
    algorithm_id: str
    as_of: datetime
    materialized: bool
    data_generation: str | None = None
    computed_at: datetime | None = None
    upstream_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class FactorResult:
    output: str
    values: pa.Table
    meta: FactorMeta


@dataclass(frozen=True, slots=True)
class FactorSummary:
    """因子清单行（供 SDK/REST/WebUI 展示）。"""

    dataset: str
    output: str
    algorithm_id: str
    materialize: str
    inputs: tuple[str, ...]
    upstream_fingerprint: str


class FactorAPI:
    """因子读取入口（两态；读不写库）。"""

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
        self._derived = DerivedEngine(
            engine, specs=self._specs, registry=self._registry, store=store
        )
        self._graph, _errors = FactorGraph.from_dictionary(self._specs)

    # ------------------------------------------------------------ 清单
    def catalog(self) -> list[FactorSummary]:
        return [
            FactorSummary(
                dataset=node.dataset,
                output=node.output,
                algorithm_id=node.algorithm_id,
                materialize=node.materialize,
                inputs=node.inputs,
                upstream_fingerprint=self._graph.fingerprint((node.dataset, node.output)),
            )
            for node in self._graph
        ]

    # ------------------------------------------------------------ 读取
    def read(
        self,
        output: str,
        *,
        as_of: datetime,
        dataset: str | None = None,
        algorithm_id: str | None = None,
        entities: Sequence[int] | None = None,
        window: tuple[date, date] | None = None,
    ) -> FactorResult:
        """读取因子（物化投影优先；按需因子走子图求值）。"""
        name, _spec, entry = self._derived.resolve(output, dataset)
        factor: FactorId = (name, entry.output)
        node = self._graph.get(factor)
        if node is None:  # resolve 已保证登记，此处仅防御
            raise FactorNotMaterialized(f"因子未登记：{name}.{entry.output}")
        if node.materialize == "latest":
            return self._read_materialized(
                factor,
                as_of=as_of,
                entities=entities,
                window=window,
                algorithm_id=algorithm_id,
            )
        values = self._evaluate(
            factor,
            as_of=as_of,
            entities=entities,
            window=window,
            memo={},
            algorithm_id=algorithm_id,
            pinned=factor,
        )
        return FactorResult(
            output=entry.output,
            values=values,
            meta=FactorMeta(
                dataset=name,
                output=entry.output,
                algorithm_id=algorithm_id or entry.algorithm_id,
                as_of=as_of,
                materialized=False,
                upstream_fingerprint=self._graph.fingerprint(factor),
            ),
        )

    # ------------------------------------------------------------ 物化态
    def _read_materialized(
        self,
        factor: FactorId,
        *,
        as_of: datetime,
        entities: Sequence[int] | None,
        window: tuple[date, date] | None,
        algorithm_id: str | None = None,
    ) -> FactorResult:
        dataset, output = factor
        spec = self._specs[dataset]
        entry = next(item for item in (spec.derived or []) if item.output == output)
        table, audit = read_projection_frame(
            self._engine,
            dataset,
            output,
            as_of=as_of,
            specs=self._specs,
            entity_ids=entities,
            window=window,
            coverage_window=window,
        )
        if (
            audit.algorithm_id is not None
            and algorithm_id is not None
            and algorithm_id != audit.algorithm_id
        ):
            raise FactorError(
                f"{dataset}.{output}: 投影算法 {audit.algorithm_id}"
                f" 与 pin {algorithm_id} 不一致",
                hint="物化投影为单份；pin 复现请使用按需因子或先重算投影",
            )
        return FactorResult(
            output=output,
            values=table,
            meta=FactorMeta(
                dataset=dataset,
                output=output,
                # 空投影无审计行：pin 无法校验（无值可辨版本），回落到字典 active
                algorithm_id=audit.algorithm_id or entry.algorithm_id,
                as_of=as_of,
                materialized=True,
                data_generation=audit.data_generation,
                computed_at=audit.computed_at,
                upstream_fingerprint=audit.upstream_fingerprint,
            ),
        )

    # ------------------------------------------------------------ 按需子图
    def _evaluate(
        self,
        factor: FactorId,
        *,
        as_of: datetime,
        entities: Sequence[int] | None,
        window: tuple[date, date] | None,
        memo: dict[FactorId, pa.Table],
        algorithm_id: str | None = None,
        pinned: FactorId | None = None,
    ) -> pa.Table:
        if factor in memo:
            return memo[factor]
        node = self._graph.get(factor)
        if node is None:
            raise FactorNotMaterialized(f"因子未登记：{factor[0]}.{factor[1]}")

        inputs: dict[str, Any] = {}
        if node.data_inputs:
            inputs.update(
                read_inputs(
                    self._engine,
                    node.data_inputs,
                    as_of=as_of,
                    specs=self._specs,
                    entity_ids=entities,
                    window=window,
                )
            )
        for upstream in node.factor_inputs:
            upstream_node = self._graph.get(upstream)
            ref = f"{upstream[0]}.{upstream[1]}"
            if upstream_node is not None and upstream_node.materialize == "latest":
                try:
                    inputs[ref] = self._read_materialized(
                        upstream, as_of=as_of, entities=entities, window=window
                    ).values
                except FactorNotMaterialized:
                    # 上游 latest 尚未物化：回落递归计算（doc-21：优先读投影，否则递归）
                    inputs[ref] = self._evaluate(
                        upstream,
                        as_of=as_of,
                        entities=entities,
                        window=window,
                        memo=memo,
                    )
            else:
                inputs[ref] = self._evaluate(
                    upstream,
                    as_of=as_of,
                    entities=entities,
                    window=window,
                    memo=memo,
                )
        override = algorithm_id if factor == pinned else None
        result = self._derived.compute(
            node.output,
            inputs,
            as_of=as_of,
            dataset=factor[0],
            algorithm_id=override,
        )
        memo[factor] = result.values
        return result.values
