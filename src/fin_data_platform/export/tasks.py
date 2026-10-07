"""导出任务挂载（TASK-3.10 / doc-12 §2.3）：全局任务 ``export.jobs``。

- 全局任务（``scope=""``，窗口 = 触发日）：**scope = export_id**（意图即导出请求）；
- 执行器：读取 ``meta.export_requests`` → 分块写出产物（Parquet / Arrow IPC）→ 更新状态；
- 失败：状态置 ``failed``（error 留痕）并抛出（Runtime 记录失败与重试）；
- 幂等：已成功完成的请求不重跑（返回既有行数）；
- 产物目录：``FDP_EXPORT_DIR``（默认 ``data/exports``；compose 挂载卷）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Engine, select

from fin_data_platform.export.schema import export_requests
from fin_data_platform.export.store import (
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    get_request,
    mark_failed,
    mark_running,
    mark_succeeded,
)
from fin_data_platform.export.writer import (
    DEFAULT_CHUNK_DAYS,
    DEFAULT_ENTITY_BATCH,
    write_export,
)
from fin_data_platform.runtime.models import JobKind
from fin_data_platform.runtime.registry import (
    JobContext,
    JobResult,
    TaskRegistry,
    TaskSpec,
)

logger = logging.getLogger(__name__)

#: 全局任务标识（由管理 API 提交意图；scope = export_id）
EXPORT_JOB = "export.jobs"
#: 任务归属数据集（请求表）
EXPORT_DATASET = "meta.export_requests"


def reconcile_stale_exports(engine: Engine) -> int:
    """启动对账：``running`` 但对应运行已非活跃（进程被杀 / 停机）→ 置 ``failed``。

    返回被修正的请求数；``pending`` 不受影响（仍可被意图执行）。
    """
    from fin_data_platform.runtime.schema import job_runs

    with engine.connect() as connection:
        rows = connection.execute(
            select(export_requests.c.export_id, export_requests.c.run_id).where(
                export_requests.c.status == STATUS_RUNNING
            )
        ).all()
        active = {
            int(item[0])
            for item in connection.execute(
                select(job_runs.c.run_id).where(
                    job_runs.c.status.in_(("queued", "running", "retrying"))
                )
            )
        }
    stale = [
        str(item[0]) for item in rows if item[1] is None or int(item[1]) not in active
    ]
    for export_id in stale:
        mark_failed(
            engine, export_id, error="运行中断（进程重启对账）：请重新提交导出"
        )
    return len(stale)


def _daily_window(now: datetime) -> list[tuple[date, date]]:
    """全局任务窗口 = 触发日（与全市场登记 / 质量扫描同款约定）。"""
    day = now.date()
    return [(day, day)]


def register_export_task(
    registry: TaskRegistry,
    engine: Engine,
    *,
    export_dir: Path | str,
    entity_batch: int = DEFAULT_ENTITY_BATCH,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    priority: int = 200,
    max_attempts: int = 2,
) -> TaskSpec:
    """注册导出任务（返回已注册定义）。"""
    directory = Path(export_dir)

    def executor(context: JobContext) -> JobResult:
        export_id = context.scope
        if not export_id:
            raise ValueError("导出任务缺少 scope（export_id）")
        request = get_request(engine, export_id)
        if request is None:
            raise ValueError(f"导出请求不存在：{export_id}")
        if request.status == STATUS_SUCCEEDED:
            return JobResult(rows_written=request.rows or 0)
        mark_running(engine, export_id, run_id=context.run.run_id)
        path = directory / f"{export_id}.{request.format}"
        try:
            rows, size = write_export(
                engine,
                request,
                path,
                entity_batch=entity_batch,
                chunk_days=chunk_days,
            )
        except Exception as exc:
            mark_failed(engine, export_id, error=f"{type(exc).__name__}: {exc}")
            raise
        mark_succeeded(engine, export_id, artifact_path=str(path), rows=rows, bytes=size)
        logger.info("导出完成：%s（%d 行 / %d 字节）", export_id, rows, size)
        return JobResult(rows_written=rows)

    return registry.register(
        TaskSpec(
            job_id=EXPORT_JOB,
            kind=JobKind.EXPORT.value,
            dataset=EXPORT_DATASET,
            executor=executor,
            priority=priority,
            max_attempts=max_attempts,
            scope="",
            window_provider=_daily_window,
        )
    )
