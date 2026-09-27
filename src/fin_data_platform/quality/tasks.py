"""质量任务挂载（TASK-3.5 / doc-20）：全局任务 ``quality.scan``。

- 全局任务（``scope=""``）：窗口 = **最近已收盘交易日**（16:30 CST 截止，落库日历；
  日历不可用时回退触发日），扫描窗口内部再回看（``lookback_days``）；
- 扫描终点独立收敛（:func:`_scan_end`）：即使被盘中手工触发，也不会把「未收盘的
  交易日」计入期望（避免当日数据未发布造成全市场假失败）；
- 调度可选（``FDP_QUALITY_SCHEDULE``）：不配置 = 仅手动 / 管理界面触发；
- 执行成功与否只看「扫描是否完成」——质量**发现**（failed / error）是数据信号，
  非任务失败；失败明细在运行日志与 ``meta.quality_results`` 中可追溯；
- 结果写入 ``meta.quality_results``（先清同 ``run_id`` 旧行，重试幂等）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine

from fin_data_platform.quality.models import QualityScanResult
from fin_data_platform.quality.runner import run_quality_scan
from fin_data_platform.quality.store import write_results
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.calendar import StoredTradeCalendar
from fin_data_platform.runtime.models import JobKind
from fin_data_platform.runtime.registry import (
    JobContext,
    JobResult,
    TaskRegistry,
    TaskSpec,
)

logger = logging.getLogger(__name__)

#: 全局任务标识（可由管理 API / WebUI 触发）
QUALITY_JOB = "quality.scan"
#: 任务归属数据集（结果表）
QUALITY_DATASET = "meta.quality_results"

#: 首批扫描的数据域（有数据且声明了质量规则；可按配置覆盖）
DEFAULT_DATASETS: tuple[str, ...] = (
    "cn_equity.daily_bar",
    "cn_equity.adj_factor",
    "cn_equity.daily_status",
    "cn_equity.listing_lifecycle",
    "ref.trade_calendar",
)


def _scan_end(calendar: Any, window_end: date, now: datetime) -> date:
    """扫描终点：不晚于最近已收盘交易日（盘中触发不误判当日数据缺失）。"""
    closed = calendar.last_closed(now)
    if closed is not None and window_end > closed:
        return closed
    return window_end


def register_quality_task(
    registry: TaskRegistry,
    engine: Engine,
    *,
    hub: Any = None,
    datasets: Sequence[str] = DEFAULT_DATASETS,
    lookback_days: int = 10,
    codes: Sequence[str] = (),
    reconcile_codes: Sequence[str] = (),
    coverage_min: float = 0.99,
    schedule: str | None = None,
    priority: int = 130,
    max_attempts: int = 3,
) -> TaskSpec:
    """注册质量扫描任务（返回已注册定义）。"""
    calendar = StoredTradeCalendar(engine)

    def window_provider(now: datetime) -> list[tuple[date, date]]:
        """窗口 = 最近已收盘交易日（与同步任务的调度口径一致）。"""
        day = calendar.last_closed(now) or now.date()
        return [(day, day)]

    def executor(context: JobContext) -> JobResult:
        end = _scan_end(
            calendar, context.window_end or utcnow().date(), utcnow()
        )
        result: QualityScanResult = run_quality_scan(
            engine,
            datasets=datasets,
            window_end=end,
            lookback_days=lookback_days,
            codes=codes,
            reconcile_codes=reconcile_codes,
            hub=hub,
            coverage_min=coverage_min,
        )
        written = write_results(engine, run_id=context.run.run_id, results=result.results)
        counts = result.counts
        failed = counts.get("failed", 0) + counts.get("error", 0)
        log = logger.warning if failed else logger.info
        log(
            "质量扫描完成（run_id=%s）：检查 %d 项，passed=%d failed=%d skipped=%d error=%d",
            context.run.run_id,
            len(result.results),
            counts.get("passed", 0),
            counts.get("failed", 0),
            counts.get("skipped", 0),
            counts.get("error", 0),
        )
        for item in result.results:
            if item.status in ("failed", "error"):
                logger.warning(
                    "质量告警：%s %s — %s", item.dataset, item.check_id, item.message
                )
        return JobResult(rows_written=written)

    return registry.register(
        TaskSpec(
            job_id=QUALITY_JOB,
            kind=JobKind.QUALITY.value,
            dataset=QUALITY_DATASET,
            executor=executor,
            schedule=schedule,
            priority=priority,
            max_attempts=max_attempts,
            scope="",
            window_provider=window_provider,
        )
    )
