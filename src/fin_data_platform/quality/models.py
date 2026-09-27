"""质量检查结果模型（TASK-3.5）。

一次质量扫描 = 一组 :class:`CheckResult`（``run × dataset × check``），
写入 ``meta.quality_results`` 后构成「每日质量报告」的明细。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: 状态取值
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

#: 检查家族
FAMILY_RULE = "rule"  # 字典规则（unique / not_null / range / enum / expression / jump）
FAMILY_COMPLETENESS = "completeness"  # 完整性（覆盖率与断点）
FAMILY_FRESHNESS = "freshness"  # 时效性（update_sla + 最新数据日）
FAMILY_RECONCILE = "reconcile"  # 引用对账（reconcile against 参照表）
FAMILY_CROSS_SOURCE = "cross_source"  # 跨源对账（小样本实拉）


@dataclass(frozen=True, slots=True)
class CheckResult:
    """单项检查结果。

    - ``samples``：违规样本（限长，如 ``entity_id=1, trade_date=2026-09-25``）；
    - ``metrics``：结构化指标（覆盖率 / 滞后 / 最大差异等，JSON 入库）；
    - ``status=skipped`` 表示结构性跳过（目标未落地 / 无重叠样本等），不计失败。
    """

    dataset: str
    check_id: str
    family: str
    severity: str
    status: str
    window_start: date | None = None
    window_end: date | None = None
    rows_checked: int = 0
    violations: int = 0
    samples: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    message: str = ""


@dataclass(slots=True)
class QualityScanResult:
    """一次质量扫描的完整结果。"""

    window_start: date
    window_end: date
    results: list[CheckResult]

    @property
    def counts(self) -> dict[str, int]:
        summary = {
            STATUS_PASSED: 0,
            STATUS_FAILED: 0,
            STATUS_SKIPPED: 0,
            STATUS_ERROR: 0,
        }
        for item in self.results:
            summary[item.status] = summary.get(item.status, 0) + 1
        return summary

    @property
    def failed(self) -> list[CheckResult]:
        return [item for item in self.results if item.status == STATUS_FAILED]

    @property
    def rows_written(self) -> int:
        return len(self.results)
