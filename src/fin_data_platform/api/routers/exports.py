"""批量导出（TASK-3.10 / doc-12 §2.3）：提交 / 状态 / 下载。

- 提交：校验参数（数据集 / 字段 / 版本模式）→ 登记 ``meta.export_requests`` → 提交
  Runtime 意图（``export.jobs``，scope = export_id）→ 202；
- 状态：请求状态机（pending / running / succeeded / failed；失败含 error 留痕）；
- 下载：产物经共享卷（``FDP_EXPORT_DIR``）读取，就绪前 409。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from fin_data_platform.api.deps import ApiContext, get_context
from fin_data_platform.api.schemas import (
    ExportCreatedOut,
    ExportListOut,
    ExportOut,
    ExportRequestIn,
)
from fin_data_platform.control import JobNotRegistered
from fin_data_platform.export import (
    EXPORT_DATASET,
    EXPORT_JOB,
    create_request,
    get_request,
    list_requests,
)
from fin_data_platform.query import (
    AS_OF_POLICIES,
    FALLBACK_MODES,
    FILTER_OPS,
    VERSION_MODES,
    AsOfRequired,
    InvalidAsOfPolicy,
    InvalidField,
    InvalidVersionMode,
    NotFound,
    UnsupportedFilter,
    platform_metadata,
)
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.models import JobIntent, JobKind

router = APIRouter(prefix="/exports", tags=["exports"])

Context = Annotated[ApiContext, Depends(get_context)]

_MEDIA = {
    "parquet": "application/vnd.apache.parquet",
    "arrow": "application/vnd.apache.arrow.stream",
}


def _out(context: ApiContext, item) -> ExportOut:  # type: ignore[no-untyped-def]
    download_url = (
        f"/v1/exports/{item.export_id}/download"
        if item.status == "succeeded" and item.artifact_path
        else None
    )
    return ExportOut(
        export_id=item.export_id,
        dataset=item.dataset,
        status=item.status,
        format=item.format,
        rows=item.rows,
        bytes=item.bytes,
        error=item.error,
        run_id=item.run_id,
        created_at=item.created_at,
        finished_at=item.finished_at,
        download_url=download_url,
    )


@router.post(
    "",
    response_model=ExportCreatedOut,
    status_code=202,
    summary="提交异步导出（Parquet / Arrow；Runtime 执行）",
)
def create_export(payload: ExportRequestIn, context: Context) -> ExportCreatedOut:
    spec = context.specs.get(payload.dataset)
    if spec is None:
        raise NotFound(
            f"数据集不存在：{payload.dataset}", hint="数据集清单见 GET /v1/datasets"
        )
    if payload.version_mode not in VERSION_MODES:
        raise InvalidVersionMode(
            f"version_mode 非法：{payload.version_mode}",
            hint="可选 latest / as_of / history",
        )
    if payload.version_mode == "as_of" and payload.as_of is None:
        raise AsOfRequired("version_mode=as_of 时 as_of 必填", hint="as_of 不隐式取 now")
    if (payload.start is None) != (payload.end is None):
        raise UnsupportedFilter("start 与 end 需同时提供", hint="窗口 = [start, end]")
    if payload.start is not None and payload.end is not None and payload.start > payload.end:
        raise UnsupportedFilter("窗口非法：start > end")
    if payload.as_of_policy not in AS_OF_POLICIES:
        raise InvalidAsOfPolicy(
            f"as_of_policy 非法：{payload.as_of_policy}", hint="可选 knowledge / publish"
        )
    if payload.fallback_mode not in FALLBACK_MODES:
        raise InvalidAsOfPolicy(
            f"fallback_mode 非法：{payload.fallback_mode}", hint="可选 strict / allow"
        )
    known = {field.name for field in spec.fields}
    if payload.fields:
        unknown = [name for name in payload.fields if name not in known]
        if unknown:
            raise InvalidField(
                f"字段不存在：{unknown}",
                hint=f"{payload.dataset} 可用字段见 GET /v1/datasets/{payload.dataset}/schema",
            )
    for clause in payload.filters or ():
        if (
            not isinstance(clause, dict)
            or clause.get("field") not in known
            or clause.get("op") not in FILTER_OPS
        ):
            raise UnsupportedFilter(
                f"过滤条件非法：{clause!r}",
                hint=f"字段需在字典内，算子可选 {'/'.join(sorted(FILTER_OPS))}",
            )
    if payload.entities:
        table = platform_metadata().tables.get(spec.storage.canonical_table)
        if table is None or "entity_id" not in table.c:
            raise UnsupportedFilter(
                "该数据集无 entity_id 列，不支持 entities 过滤",
                hint="移除 entities 或改用 filters",
            )
    definition = next(
        (item for item in context.meta.list_defs() if item.job_id == EXPORT_JOB), None
    )
    if definition is None:
        raise JobNotRegistered(
            "导出任务未装配（export.jobs）", hint="检查 Runtime 部署与启动日志"
        )

    params = payload.model_dump(exclude={"request_id"}, mode="json")
    request = create_request(
        context.writer_engine,
        dataset=payload.dataset,
        params=params,
        format=payload.format,
        request_id=payload.request_id,
    )
    today = utcnow().date()
    run = context.meta.create_run(
        JobIntent(
            kind=JobKind.EXPORT.value,
            job_id=EXPORT_JOB,
            dataset=EXPORT_DATASET,
            scope=request.export_id,
            window_start=today,
            window_end=today,
            # 与注册的 TaskSpec 声明一致（重试预算 / 优先级）
            priority=definition.priority,
            max_attempts=definition.max_attempts,
        )
    )
    return ExportCreatedOut(
        export_id=request.export_id,
        status=request.status,
        run_id=run.run_id if run is not None else None,
    )


@router.get("", response_model=ExportListOut, summary="导出请求列表（最近优先）")
def list_exports(
    context: Context,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ExportListOut:
    items = list_requests(context.writer_engine, limit=limit)
    return ExportListOut(total=len(items), items=[_out(context, item) for item in items])


@router.get("/{export_id}", response_model=ExportOut, summary="导出请求状态")
def get_export(export_id: str, context: Context) -> ExportOut:
    request = get_request(context.writer_engine, export_id)
    if request is None:
        raise NotFound(f"导出请求不存在：{export_id}", hint="见 GET /v1/exports")
    return _out(context, request)


@router.get("/{export_id}/download", summary="下载导出产物")
def download_export(export_id: str, context: Context) -> FileResponse:
    request = get_request(context.writer_engine, export_id)
    if request is None:
        raise NotFound(f"导出请求不存在：{export_id}", hint="见 GET /v1/exports")
    if request.status != "succeeded" or not request.artifact_path:
        raise HTTPException(
            status_code=409,
            detail=f"导出未完成（status={request.status}）：{request.error or '稍后重试'}",
        )
    path = Path(request.artifact_path)
    if not path.is_file():
        raise NotFound(
            f"产物不存在（可能已被清理）：{request.artifact_path}",
            hint="重新提交导出",
        )
    return FileResponse(
        path,
        media_type=_MEDIA.get(request.format, "application/octet-stream"),
        filename=f"{export_id}.{request.format}",
    )
