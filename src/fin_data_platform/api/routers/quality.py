"""数据质量报告（TASK-3.5）：每日摘要 + 明细下钻（读端）。

结果由质量任务（``quality.scan``）写入 ``meta.quality_results``；本路由只读。
质量报告属**控制面产物**（``meta`` schema 不对只读角色授权），故与任务 / 水位一致
使用写连接；日期缺省 = 最近报告日（同日多次运行取最新一次）。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from fin_data_platform.api.deps import ApiContext, get_context
from fin_data_platform.api.schemas import (
    QualityResultOut,
    QualityResultsPage,
    QualitySummaryOut,
)
from fin_data_platform.quality import fetch_results, fetch_summary
from fin_data_platform.quality.models import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
)

router = APIRouter(tags=["quality"])

Context = Annotated[ApiContext, Depends(get_context)]

_STATUS_PATTERN = "|".join((STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED, STATUS_ERROR))


@router.get(
    "/quality/summary", response_model=QualitySummaryOut, summary="每日质量报告（按数据集）"
)
def quality_summary(
    context: Context,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> QualitySummaryOut:
    return QualitySummaryOut.model_validate(fetch_summary(context.writer_engine, day=day))


@router.get("/quality/results", response_model=QualityResultsPage, summary="质量检查明细")
def quality_results(
    context: Context,
    day: Annotated[date | None, Query(alias="date")] = None,
    dataset: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query(pattern=f"^({_STATUS_PATTERN})$")] = None,
    severity: Annotated[str | None, Query(pattern="^(error|warn)$")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QualityResultsPage:
    total, items = fetch_results(
        context.writer_engine,
        day=day,
        dataset=dataset,
        status=status,
        severity=severity,
        limit=limit,
        offset=offset,
    )
    return QualityResultsPage(
        total=total, items=[QualityResultOut.model_validate(item) for item in items]
    )
