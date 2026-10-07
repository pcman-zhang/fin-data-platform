"""批量导出（TASK-3.10 / doc-12 §2.3）：异步导出任务 + 分块写出 + 请求状态。

- :func:`register_export_task`：全局任务 ``export.jobs``（Runtime 执行；scope=export_id）；
- :func:`write_export`：分块批量写 Parquet / Arrow IPC（实体批 × 时间块，内存有界）；
- :mod:`fin_data_platform.export.store`：``meta.export_requests`` 读写（状态机）。
"""

from fin_data_platform.export.store import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    ExportRequest,
    create_request,
    get_request,
    list_requests,
    mark_failed,
    mark_running,
    mark_succeeded,
)
from fin_data_platform.export.tasks import (
    EXPORT_DATASET,
    EXPORT_JOB,
    reconcile_stale_exports,
    register_export_task,
)
from fin_data_platform.export.writer import (
    DEFAULT_CHUNK_DAYS,
    DEFAULT_ENTITY_BATCH,
    ExportPlan,
    write_export,
)

__all__ = [
    "DEFAULT_CHUNK_DAYS",
    "DEFAULT_ENTITY_BATCH",
    "EXPORT_DATASET",
    "EXPORT_JOB",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "ExportPlan",
    "ExportRequest",
    "create_request",
    "get_request",
    "list_requests",
    "mark_failed",
    "mark_running",
    "mark_succeeded",
    "reconcile_stale_exports",
    "register_export_task",
    "write_export",
]
