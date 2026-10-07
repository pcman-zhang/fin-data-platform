"""导出请求读写（``meta.export_requests``）：提交 / 状态机 / 查询。

状态机：``pending`` → ``running`` → ``succeeded`` / ``failed``（失败保留 error 供排障，
重试由 Runtime 运行记录驱动）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select, update

from fin_data_platform.export.schema import export_requests
from fin_data_platform.runtime._util import utcnow

_TABLE = export_requests

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExportRequest:
    export_id: str
    dataset: str
    params: dict[str, Any]
    status: str
    format: str
    artifact_path: str | None = None
    rows: int | None = None
    bytes: int | None = None
    error: str | None = None
    run_id: int | None = None
    request_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


def create_request(
    engine: Engine,
    *,
    dataset: str,
    params: dict[str, Any],
    format: str = "parquet",
    request_id: str | None = None,
) -> ExportRequest:
    """登记导出请求（``pending``；``export_id`` 由平台生成，作为任务 scope）。"""
    export_id = uuid.uuid4().hex[:16]
    now = utcnow()
    row = {
        "export_id": export_id,
        "dataset": dataset,
        "params": json.dumps(params, ensure_ascii=False, default=str),
        "status": STATUS_PENDING,
        "format": format,
        "request_id": request_id,
        "created_at": now,
        "updated_at": now,
    }
    with engine.begin() as connection:
        connection.execute(_TABLE.insert().values(**row))
    return ExportRequest(
        export_id=export_id,
        dataset=dataset,
        params=params,
        status=STATUS_PENDING,
        format=format,
        request_id=request_id,
        created_at=now,
        updated_at=now,
    )


def get_request(engine: Engine, export_id: str) -> ExportRequest | None:
    with engine.connect() as connection:
        row = connection.execute(
            select(_TABLE).where(_TABLE.c.export_id == export_id)
        ).mappings().one_or_none()
    return _row(row) if row is not None else None


def list_requests(engine: Engine, *, limit: int = 50) -> list[ExportRequest]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(_TABLE).order_by(_TABLE.c.created_at.desc()).limit(limit)
        ).mappings()
        return [_row(row) for row in rows]


def mark_running(engine: Engine, export_id: str, *, run_id: int | None = None) -> None:
    # 清空 finished_at：重试等待期间不得残留上次失败时间
    _update(
        engine,
        export_id,
        status=STATUS_RUNNING,
        run_id=run_id,
        error=None,
        finished_at=None,
    )


def mark_succeeded(
    engine: Engine, export_id: str, *, artifact_path: str, rows: int, bytes: int
) -> None:
    _update(
        engine,
        export_id,
        status=STATUS_SUCCEEDED,
        artifact_path=artifact_path,
        rows=rows,
        bytes=bytes,
        error=None,
        finished=True,
    )


def mark_failed(engine: Engine, export_id: str, *, error: str) -> None:
    _update(engine, export_id, status=STATUS_FAILED, error=error[:2000], finished=True)


def _update(engine: Engine, export_id: str, *, finished: bool = False, **values: Any) -> None:
    now = utcnow()
    values["updated_at"] = now
    if finished:
        values["finished_at"] = now
    with engine.begin() as connection:
        connection.execute(
            update(_TABLE).where(_TABLE.c.export_id == export_id).values(**values)
        )


def _row(row: Any) -> ExportRequest:
    return ExportRequest(
        export_id=str(row["export_id"]),
        dataset=str(row["dataset"]),
        params=json.loads(row["params"] or "{}"),
        status=str(row["status"]),
        format=str(row["format"]),
        artifact_path=row["artifact_path"],
        rows=row["rows"],
        bytes=row["bytes"],
        error=row["error"],
        run_id=row["run_id"],
        request_id=row["request_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )
