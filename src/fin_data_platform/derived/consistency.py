"""字典 ↔ 算法注册表一致性（doc-11 §4 三方一致性 CI）。

校验方向：

1. **字典 → 代码**：字典每条 ``derived`` 的 ``algorithm_id`` 必须在注册表中，
   ``implementation`` / ``owner`` 与登记一致；
2. **代码 → 字典**：注册表中未被任何字典条目引用的 id 标记 ``deprecated``
   （历史永久保留，不删除）；状态由 :func:`classify_algorithms` 计算；
3. **实现可导入**：``implementation`` 为可导入的模块级函数（导入即触发 ``@register``）。
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping

from fin_data_platform.derived.graph import FactorGraph
from fin_data_platform.derived.inputs import input_view_name
from fin_data_platform.derived.registry import DEFAULT_REGISTRY, AlgorithmRegistry
from fin_data_platform.dictionary.models import DatasetSpec, DerivedEntry

#: 字典引用索引：algorithm_id → (dataset, 条目)
Referenced = dict[str, tuple[str, DerivedEntry]]


def referenced_algorithms(specs: Mapping[str, DatasetSpec]) -> Referenced:
    referenced: Referenced = {}
    for dataset, spec in sorted(specs.items()):
        for entry in spec.derived or []:
            referenced[entry.algorithm_id] = (dataset, entry)
    return referenced


def import_implementations(specs: Mapping[str, DatasetSpec]) -> list[str]:
    """导入字典声明的实现模块（触发 ``@register``）；返回导入错误列表。"""
    errors: list[str] = []
    attempted: set[str] = set()
    for dataset, spec in sorted(specs.items()):
        for entry in spec.derived or []:
            if entry.implementation in attempted:
                continue
            attempted.add(entry.implementation)
            module_path, _, attribute = entry.implementation.rpartition(".")
            try:
                module = importlib.import_module(module_path)
            except Exception as exc:  # 导入错误原因多样，统一报告
                errors.append(
                    f"{dataset}.{entry.output}: 实现不可导入 {entry.implementation}"
                    f"（{type(exc).__name__}: {exc}）"
                )
                continue
            if getattr(module, attribute, None) is None:
                errors.append(f"{dataset}.{entry.output}: 模块 {module_path} 缺少实现 {attribute}")
    return errors


def check_consistency(
    specs: Mapping[str, DatasetSpec],
    registry: AlgorithmRegistry | None = None,
    *,
    import_implementations_first: bool = True,
) -> list[str]:
    """全量一致性校验（CI 与同步入口共用；空列表 = 通过）。"""
    target = registry if registry is not None else DEFAULT_REGISTRY
    errors: list[str] = []
    if import_implementations_first:
        errors.extend(import_implementations(specs))
    errors.extend(target.validate())
    graph, graph_errors = FactorGraph.from_dictionary(specs)
    errors.extend(graph_errors)
    for dataset, entry in referenced_algorithms(specs).values():
        found = target.get(entry.algorithm_id)
        if found is None:
            errors.append(
                f"{dataset}.{entry.output}: 算法未注册（缺少 @register）：{entry.algorithm_id}"
            )
            continue
        if found.implementation != entry.implementation:
            errors.append(
                f"{dataset}.{entry.output}: implementation 与注册不符"
                f"（字典 {entry.implementation}，注册 {found.implementation}）"
            )
        if found.owner != entry.owner:
            errors.append(
                f"{dataset}.{entry.output}: owner 与注册不符"
                f"（字典 {entry.owner}，注册 {found.owner}）"
            )
        if found.inline_sql:
            missing_views = [
                input_view_name(ref)
                for ref in entry.inputs
                if input_view_name(ref) not in found.inline_sql
            ]
            if missing_views:
                errors.append(f"{dataset}.{entry.output}: inline_sql 缺少输入视图 {missing_views}")
            node = graph.get((dataset, entry.output))
            if node is not None and node.factor_inputs:
                errors.append(
                    f"{dataset}.{entry.output}: inline_sql 不支持因子输入"
                    "（读模型内联仅支持物理字段）"
                )
    return errors


def classify_algorithms(
    specs: Mapping[str, DatasetSpec],
    registry: AlgorithmRegistry | None = None,
) -> dict[str, str]:
    """algorithm_id → 状态（``active``：被字典引用；``deprecated``：历史保留）。"""
    target = registry if registry is not None else DEFAULT_REGISTRY
    referenced = referenced_algorithms(specs)
    return {
        spec.algorithm_id: "active" if spec.algorithm_id in referenced else "deprecated"
        for spec in target
    }
