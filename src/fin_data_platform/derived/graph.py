"""因子依赖图（doc-10 §3.6；TASK-3.27）。

节点 = 因子输出（``dataset.derived.output``）；边来自 ``derived.inputs``：

- **数据依赖**：输入是数据字段（``dataset.field``，经访问面）→ 无因子上游；
- **因子依赖**：输入是另一因子输出（同一或其它数据集）→ 算法级依赖。

图用于三处（TASK-3.27）：注册期校验（无环 / 引用可解析 / latest 约束）、
任务依赖门控（``job_dependencies`` 自动生成）、审计指纹（上游算法集合哈希，
检测"上游已升级、下游未重算"的混合 vintage）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fin_data_platform.dictionary.models import DatasetSpec

if TYPE_CHECKING:
    from fin_data_platform.derived.registry import AlgorithmRegistry

#: 因子标识：``(dataset, output)``
FactorId = tuple[str, str]


def _resolve_version(algorithms: AlgorithmRegistry, algorithm_id: str) -> int:
    """解析算法版本（注册表优先；未注册回落到 id 后缀 / 0，供 CI 一致性汇总报错）。"""
    from fin_data_platform.derived.registry import version_from_id

    spec = algorithms.get(algorithm_id)
    if spec is not None:
        return int(spec.version)
    return version_from_id(algorithm_id) or 0


@dataclass(frozen=True, slots=True)
class FactorNode:
    dataset: str
    output: str
    algorithm_id: str
    algorithm_version: int
    materialize: str
    #: 原始输入引用（含 ``@mode`` 后缀）
    inputs: tuple[str, ...]
    #: 数据字段引用（经访问面）
    data_inputs: tuple[str, ...]
    #: 因子上游（依赖边）
    factor_inputs: tuple[FactorId, ...]

    @property
    def identity(self) -> str:
        """审计身份 ``id@vN``（与 :class:`AlgorithmSpec.identity` 同构）。"""
        return f"{self.algorithm_id}@v{self.algorithm_version}"


class FactorGraph:
    """因子依赖图（不可变；由字典构建）。"""

    def __init__(self, nodes: Mapping[FactorId, FactorNode]) -> None:
        self._nodes = dict(nodes)

    # ------------------------------------------------------------ 构建
    @classmethod
    def from_dictionary(
        cls,
        specs: Mapping[str, DatasetSpec],
        registry: AlgorithmRegistry | None = None,
    ) -> tuple[FactorGraph, list[str]]:
        """构图并返回 ``(graph, errors)``（错误不抛出，供 CI 统一汇总）。

        ``registry``（``AlgorithmRegistry``）：解析各节点的算法 ``version``（审计身份
        ``id@vN`` 参与指纹）；缺省用默认注册表，未注册的实现回落到 id 后缀/``0``。
        """
        from fin_data_platform.derived.registry import DEFAULT_REGISTRY

        algorithms = registry if registry is not None else DEFAULT_REGISTRY
        errors: list[str] = []
        outputs: dict[str, set[str]] = {}
        fields: dict[str, set[str]] = {}
        for dataset, spec in specs.items():
            outputs[dataset] = {entry.output for entry in (spec.derived or [])}
            fields[dataset] = {item.name for item in spec.fields}

        nodes: dict[FactorId, FactorNode] = {}
        seen_outputs: set[tuple[str, str]] = set()
        for dataset, spec in sorted(specs.items()):
            field_names = {item.name for item in spec.fields}
            for entry in spec.derived or []:
                label = f"{dataset}.{entry.output}"
                factor_id = (dataset, entry.output)
                if factor_id in seen_outputs:
                    errors.append(f"{label}: derived.output 在同一数据集重复登记")
                    continue
                seen_outputs.add(factor_id)
                if entry.output in field_names:
                    errors.append(f"{label}: derived.output 与物理字段同名")
                data_inputs: list[str] = []
                factor_inputs: list[FactorId] = []
                for ref in entry.inputs:
                    base, _, _mode = ref.partition("@")
                    ref_dataset, _, ref_field = base.rpartition(".")
                    if ref_dataset not in specs:
                        errors.append(f"{label}: 输入数据集不存在 {ref_dataset}")
                        continue
                    if ref_field in outputs.get(ref_dataset, set()):
                        factor_inputs.append((ref_dataset, ref_field))
                    elif ref_field not in fields.get(ref_dataset, set()):
                        errors.append(f"{label}: 输入字段不存在 {base}")
                    else:
                        data_inputs.append(ref)
                nodes[(dataset, entry.output)] = FactorNode(
                    dataset=dataset,
                    output=entry.output,
                    algorithm_id=entry.algorithm_id,
                    algorithm_version=_resolve_version(algorithms, entry.algorithm_id),
                    materialize=entry.materialize.value,
                    inputs=tuple(entry.inputs),
                    data_inputs=tuple(data_inputs),
                    factor_inputs=tuple(factor_inputs),
                )
        graph = cls(nodes)
        errors.extend(graph.validate())
        return graph, errors

    # ------------------------------------------------------------ 校验
    def validate(self) -> list[str]:
        """无环 + 引用可解析 + latest 约束（空列表 = 通过）。"""
        errors: list[str] = []
        for node in self:
            for upstream in node.factor_inputs:
                if upstream not in self._nodes:
                    errors.append(
                        f"{node.dataset}.{node.output}: 因子输入未登记输出 {upstream[1]}"
                        f"（{upstream[0]}）"
                    )
                elif node.materialize == "latest" and self._nodes[upstream].materialize != "latest":
                    errors.append(
                        f"{node.dataset}.{node.output}: latest 物化的下游要求上游因子"
                        f" {upstream[1]} 亦为 latest（当前 "
                        f"{self._nodes[upstream].materialize}）"
                    )
        for cycle in self.cycles():
            path = " → ".join(f"{dataset}.{output}" for dataset, output in cycle)
            errors.append(f"因子依赖成环: {path}")
        return errors

    # ------------------------------------------------------------ 查询
    def __iter__(self) -> Iterator[FactorNode]:
        return iter([self._nodes[key] for key in sorted(self._nodes)])

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, factor: object) -> bool:
        return factor in self._nodes

    def get(self, factor: FactorId) -> FactorNode | None:
        return self._nodes.get(factor)

    def ids(self) -> list[FactorId]:
        return sorted(self._nodes)

    def cycles(self) -> list[list[FactorId]]:
        """返回参与环的节点路径（自环也算；每环只报一条路径）。"""
        found: list[list[FactorId]] = []
        state: dict[FactorId, int] = {}
        path: list[FactorId] = []

        def visit(node: FactorId) -> None:
            state[node] = 1
            path.append(node)
            for upstream in self._nodes[node].factor_inputs:
                if upstream not in self._nodes:
                    continue
                if state.get(upstream) == 1:
                    start = path.index(upstream)
                    found.append([*path[start:], upstream])
                elif state.get(upstream) is None:
                    visit(upstream)
            path.pop()
            state[node] = 2

        for node in self.ids():
            if state.get(node) is None:
                visit(node)
        return found

    def topological_order(self) -> list[FactorId]:
        """拓扑序（上游在前；同层按名称稳定排序）。"""
        indegree = {
            node: len([up for up in self._nodes[node].factor_inputs if up in self._nodes])
            for node in self._nodes
        }
        ready = sorted(node for node, degree in indegree.items() if degree == 0)
        order: list[FactorId] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for candidate in self.ids():
                if node in self._nodes[candidate].factor_inputs:
                    indegree[candidate] -= 1
                    if indegree[candidate] == 0:
                        ready.append(candidate)
            ready.sort()
        if len(order) != len(self._nodes):
            raise ValueError("因子依赖成环，无法拓扑排序")
        return order

    def upstream_closure(self, factor: FactorId) -> list[FactorId]:
        """传递上游集合（不含自身；排序稳定）。"""
        seen: set[FactorId] = set()
        stack = list(self._nodes[factor].factor_inputs) if factor in self._nodes else []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            node = self._nodes.get(current)
            if node is not None:
                stack.extend(node.factor_inputs)
        return sorted(seen)

    def fingerprint(self, factor: FactorId) -> str:
        """上游算法指纹：传递上游集合的**审计身份**（``id@vN``）排序哈希（16 位）。

        上游算法升级（``version`` 递增）→ 指纹变化 → 下游投影可判定"需重算"。
        """
        algorithm_ids = sorted(
            {
                self._nodes[upstream].identity
                for upstream in self.upstream_closure(factor)
                if upstream in self._nodes
            }
        )
        payload = "|".join(algorithm_ids)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
