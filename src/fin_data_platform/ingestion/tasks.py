"""Sync job 注册：把 Sync Engine 挂到 Runtime（最小：单标的日线）。"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import Engine

from fin_data_platform.cache import LayeredCache
from fin_data_platform.ingestion.adj_factor import (
    DATASET as ADJ_FACTOR_DATASET,
)
from fin_data_platform.ingestion.adj_factor import (
    sync_adjust_factor,
)
from fin_data_platform.ingestion.common import (
    EmptySourceWindow,
    SyncResult,
    calendar_open_days,
    entity_has_history,
)
from fin_data_platform.ingestion.daily_bar import DATASET, sync_daily_bar
from fin_data_platform.ingestion.daily_status import (
    DATASET as STATUS_DATASET,
)
from fin_data_platform.ingestion.daily_status import (
    sync_daily_status,
)
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.models import JobKind
from fin_data_platform.runtime.registry import (
    JobContext,
    JobResult,
    TaskRegistry,
    TaskSpec,
)
from fin_data_platform.runtime.repository import MetaRepository


def _guard_empty_window(engine: Engine, label: str, result: SyncResult) -> None:
    """空窗口软失败判定（TASK-3.33）。

    源端 0 行（``fetched == 0``）且窗口含交易日且该实体在窗口末（含）之前已有数据
    （在市；含「窗口内已有部分数据」的情形）时判为软失败（抛
    :class:`EmptySourceWindow`，Runtime 重试且不推进水位）；无交易日、日历不可见
    （fail-open）或实体无历史（前上市 / 首次同步）时允许成功。
    """
    if result.fetched != 0:
        return
    days = calendar_open_days(engine, start=result.window_start, end=result.window_end)
    if not days:  # 窗口无交易日 / 日历不可见：允许 0 行成功
        return
    if not entity_has_history(
        engine, result.dataset, entity_id=result.entity_id, through=result.window_end
    ):
        return  # 前上市 / 首次同步：无法判定为空响应
    raise EmptySourceWindow(
        f"{label}：窗口 {result.window_start}~{result.window_end} 含 {len(days)} 个交易日，"
        f"但源端返回 0 行（{result.dataset} / {result.code}）；已按重试处理，不推进水位"
    )


def _sync_executor(sync_fn: Any, engine: Engine, hub: Any, *, code: str, source: Any, label: str):
    def executor(context: JobContext) -> JobResult:
        if context.window_start is None or context.window_end is None:
            raise ValueError(f"{label} 任务需要窗口（window_start / window_end）")
        if context.scope and context.scope != code:
            raise ValueError(f"intent scope 与注册代码不一致: {context.scope!r} != {code!r}")
        result = sync_fn(
            engine,
            hub,
            code=code,
            start=context.window_start,
            end=context.window_end,
            source=source,
        )
        _guard_empty_window(engine, label, result)
        return JobResult(rows_written=result.rows_written)

    return executor


def _watermark_handler(dataset: str, code: str, cache: LayeredCache | None):
    def mark_watermark(context: JobContext, result: JobResult, repository: MetaRepository) -> None:
        if context.window_end is None:
            return
        target = context.window_end
        # 窗口末日即今日且无数据：源端可能尚未发布 → 保留今日待下轮重试（防缺口永不回补）
        if target >= utcnow().date() and (result.rows_written or 0) == 0:
            target = target - timedelta(days=1)
        repository.advance_watermark(
            dataset,
            scope=code,
            watermark_time=datetime.combine(target, time(0, 0)),
        )
        if cache is not None:
            # 同步成功 → 按域失效（代际递增；缓存非权威，fail-open）
            cache.invalidate_domain(dataset.split(".", 1)[0])

    return mark_watermark


def register_adj_factor_task(
    registry: TaskRegistry,
    engine: Engine,
    hub: Any,
    *,
    code: str,
    source: Any = None,
    schedule: str | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    cache: LayeredCache | None = None,
) -> TaskSpec:
    """注册单标的复权因子同步任务（TASK-3.29；与日线同窗口/调度/水位约定）。"""
    return registry.register(
        TaskSpec(
            job_id=f"sync.{ADJ_FACTOR_DATASET}.{code}",
            kind=JobKind.SYNC.value,
            dataset=ADJ_FACTOR_DATASET,
            executor=_sync_executor(
                sync_adjust_factor,
                engine,
                hub,
                code=code,
                source=source,
                label="复权因子同步",
            ),
            schedule=schedule,
            priority=priority,
            max_attempts=max_attempts,
            scope=code,
            on_success=_watermark_handler(ADJ_FACTOR_DATASET, code, cache),
        )
    )


def register_daily_bar_task(
    registry: TaskRegistry,
    engine: Engine,
    hub: Any,
    *,
    code: str,
    source: Any = None,
    schedule: str | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    cache: LayeredCache | None = None,
) -> TaskSpec:
    """注册单标的日线同步任务（``scope=code``；窗口由调度或手动意图提供）。

    ``schedule`` 为 cron（5 段，UTC）或 ``interval:<秒>``；成功后自动推进水位
    （``meta.watermarks``），供断点续传/补数（窗口 = 水位+1 ~ 最近已收盘交易日）。
    """
    return registry.register(
        TaskSpec(
            job_id=f"sync.{DATASET}.{code}",
            kind=JobKind.SYNC.value,
            dataset=DATASET,
            executor=_sync_executor(
                sync_daily_bar, engine, hub, code=code, source=source, label="日线同步"
            ),
            schedule=schedule,
            priority=priority,
            max_attempts=max_attempts,
            scope=code,
            on_success=_watermark_handler(DATASET, code, cache),
        )
    )


def register_daily_status_task(
    registry: TaskRegistry,
    engine: Engine,
    hub: Any,
    *,
    code: str,
    source: Any = None,
    schedule: str | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    cache: LayeredCache | None = None,
) -> TaskSpec:
    """注册单标的每日状态同步任务（停牌/ST；与日线同窗口/调度/水位约定）。"""
    return registry.register(
        TaskSpec(
            job_id=f"sync.{STATUS_DATASET}.{code}",
            kind=JobKind.SYNC.value,
            dataset=STATUS_DATASET,
            executor=_sync_executor(
                sync_daily_status,
                engine,
                hub,
                code=code,
                source=source,
                label="每日状态同步",
            ),
            schedule=schedule,
            priority=priority,
            max_attempts=max_attempts,
            scope=code,
            on_success=_watermark_handler(STATUS_DATASET, code, cache),
        )
    )
