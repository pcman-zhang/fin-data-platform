"""数据质量检查（TASK-3.5；规则语义见 doc-11 §3.4，结果表见 doc-13）。

- 规则执行（unique / not_null / range / enum / expression / jump）——规则来自数据字典；
- 完整性（coverage：期望实体 × 交易日 vs 实际，停牌感知、逐日断点）；
- 时效性（``update_sla`` + 最新数据日 → 滞后交易日数）；
- 引用对账（``reconcile against`` 参照表：共享键存在性；未落地目标记 skipped）；
- 跨源对账（小样本实拉：原始价一致 + 复权因子归一化，doc-8 SOP 子集）。

结果写入 ``meta.quality_results``（append-only：run × dataset × check），
经管理 API / WebUI「质量」页构成每日质量报告。
"""

from fin_data_platform.quality.models import CheckResult, QualityScanResult
from fin_data_platform.quality.runner import run_quality_scan
from fin_data_platform.quality.store import (
    fetch_results,
    fetch_summary,
    latest_day,
    write_results,
)
from fin_data_platform.quality.tasks import (
    DEFAULT_DATASETS,
    QUALITY_DATASET,
    QUALITY_JOB,
    register_quality_task,
)

__all__ = [
    "CheckResult",
    "DEFAULT_DATASETS",
    "QUALITY_DATASET",
    "QUALITY_JOB",
    "QualityScanResult",
    "fetch_results",
    "fetch_summary",
    "latest_day",
    "register_quality_task",
    "run_quality_scan",
    "write_results",
]
