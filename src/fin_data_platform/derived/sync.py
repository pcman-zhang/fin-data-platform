"""算法登记同步：代码 ``@register`` + 字典 ``derived`` → ``meta.*``（doc-11 §4）。

一致性不通过时**拒绝写入**（CI 与运行时同一门禁）；同步为幂等 upsert，
历史 id 只置 ``deprecated``，永不删除。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fin_data_platform.derived.consistency import (
    check_consistency,
    classify_algorithms,
    referenced_algorithms,
)
from fin_data_platform.derived.registry import AlgorithmRegistry
from fin_data_platform.derived.store import (
    AlgorithmEvent,
    AlgorithmRow,
    AlgorithmStore,
)
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec


@dataclass(frozen=True, slots=True)
class SyncReport:
    """同步结果（供 CLI 与任务日志）。"""

    total: int
    active: int
    deprecated: int
    events: int


def build_rows(
    specs: Mapping[str, DatasetSpec], registry: AlgorithmRegistry | None = None
) -> list[AlgorithmRow]:
    """按注册表（全量历史）+ 字典（引用与元数据）构造登记行。"""
    from fin_data_platform.derived.registry import DEFAULT_REGISTRY

    target = registry if registry is not None else DEFAULT_REGISTRY
    referenced = referenced_algorithms(specs)
    statuses = classify_algorithms(specs, target)
    rows: list[AlgorithmRow] = []
    for spec in target:
        entry_ref = referenced.get(spec.algorithm_id)
        if entry_ref is not None:
            dataset, entry = entry_ref
            output: str | None = entry.output
            inputs = tuple(entry.inputs)
            description = entry.description
        else:
            dataset, output, inputs = None, None, ()
            description = next(
                (line.strip() for line in spec.docstring.splitlines() if line.strip()),
                spec.algorithm_id,
            )
        rows.append(
            AlgorithmRow(
                algorithm_id=spec.algorithm_id,
                version=spec.version,
                owner=spec.owner,
                implementation=spec.implementation,
                dataset=dataset,
                output=output,
                inputs=inputs,
                description=description,
                status=statuses[spec.algorithm_id],
                effective_from=spec.effective_from,
            )
        )
    return rows


def build_events(registry: AlgorithmRegistry | None = None) -> list[AlgorithmEvent]:
    """升级 / 重述事件（声明了 ``effective_from`` 的算法）。"""
    from fin_data_platform.derived.registry import DEFAULT_REGISTRY

    target = registry if registry is not None else DEFAULT_REGISTRY
    return [
        AlgorithmEvent(
            algorithm_id=spec.algorithm_id,
            effective_from=spec.effective_from,
            reason=spec.reason,
        )
        for spec in target
        if spec.effective_from is not None
    ]


def sync_algorithms(
    store: AlgorithmStore,
    specs: Mapping[str, DatasetSpec] | None = None,
    registry: AlgorithmRegistry | None = None,
) -> SyncReport:
    """校验一致性后同步登记表与升级台账（幂等）。"""
    dictionary = specs if specs is not None else load_all()
    errors = check_consistency(dictionary, registry)
    if errors:
        raise ValueError("算法一致性校验失败：" + "；".join(errors))
    rows = build_rows(dictionary, registry)
    events = build_events(registry)
    store.upsert(rows)
    written_events = store.record_events(events)
    return SyncReport(
        total=len(rows),
        active=sum(1 for row in rows if row.status == "active"),
        deprecated=sum(1 for row in rows if row.status == "deprecated"),
        events=written_events,
    )


def describe_rows(rows: list[AlgorithmRow]) -> str:
    """登记行的人类可读摘要（CLI 输出）。"""
    lines = ["algorithm_id\tversion\tstatus\tdataset\toutput\timplementation"]
    for row in rows:
        lines.append(
            "\t".join(
                [
                    row.algorithm_id,
                    str(row.version),
                    row.status,
                    row.dataset or "-",
                    row.output or "-",
                    row.implementation,
                ]
            )
        )
    return "\n".join(lines)
