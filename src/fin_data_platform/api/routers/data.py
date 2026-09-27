"""REST 数据面（TASK-3.7 / doc-12）：PIT 行 / 访问面 Raw / 因子 / 新鲜度 —— SDK 薄封装。

- 只读、无写入端点；语义与 SDK 共用同一内核（``query`` / ``access`` / ``derived.factor_api``）；
- 响应头按 doc-12 §3.4（X-As-Of / X-Version-Mode / X-Semantic-Version / X-Data-Generation /
  X-Freshness-Lag / X-Query-Rows / X-Query-Cost / X-Cache / X-Request-Id / ETag）；
- ``If-None-Match`` → 304；``Accept: application/vnd.apache.arrow.stream`` 或 ``format=arrow``
  返回 Arrow IPC；JSON 默认；
- 错误统一 RFC 9457（处理器在 app.py；code/hint 来自各层结构化异常）。

首期不提供 API Key 鉴权与调用审计（个人平台定位；认证授权与审计留待增强）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from fin_data_platform.access import read as access_read
from fin_data_platform.api.deps import ApiContext, get_context
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.query import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ArrowUnavailable,
    FilterClause,
    InvalidAsOf,
    RowsQuery,
    UnsupportedFilter,
    platform_metadata,
    read_rows,
)
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.calendar import StoredTradeCalendar

router = APIRouter(tags=["data"])

Context = Annotated[ApiContext, Depends(get_context)]

ARROW_MEDIA = "application/vnd.apache.arrow.stream"


# ---------------------------------------------------------------- 通用工具
def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _parse_as_of(raw: str | None) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise InvalidAsOf(
            f"as_of 非法：{raw!r}", hint="ISO8601（如 2025-01-01T12:00:00+08:00）"
        ) from exc


def _parse_date(raw: str | None, *, name: str) -> date | None:
    if raw is None or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise UnsupportedFilter(f"{name} 非法：{raw!r}", hint="ISO 日期 YYYY-MM-DD") from exc


def _split(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_filters(raw: str | None) -> tuple[FilterClause, ...]:
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UnsupportedFilter(
            "filters 不是合法 JSON",
            hint='形如 [{"field":"trade_date","op":"between","value":["2024-01-01","2024-12-31"]}]',
        ) from exc
    if not isinstance(payload, list):
        raise UnsupportedFilter("filters 需为数组")
    clauses: list[FilterClause] = []
    for item in payload:
        if not isinstance(item, dict) or "field" not in item or "op" not in item:
            raise UnsupportedFilter("filters 条目需含 field / op / value")
        clauses.append(
            FilterClause(field=str(item["field"]), op=str(item["op"]), value=item.get("value"))
        )
    return tuple(clauses)


def _window(start: date | None, end: date | None) -> tuple[date, date] | None:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise UnsupportedFilter("start 与 end 需同时提供", hint="窗口 = [start, end]")
    return (start, end)


def _rows_payload(frame: Any) -> list[dict[str, Any]]:
    """DataFrame → JSON 安全行（NaN/NaT → null；日期/时间/Decimal 归一）。"""
    clean = frame.astype(object).where(frame.notna(), None)
    records = clean.to_dict(orient="records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, (datetime, date)):
                record[key] = value.isoformat()
            elif isinstance(value, Decimal):
                record[key] = float(value)
    return records


def _validate_format(format_param: str | None) -> str | None:
    if format_param is None:
        return None
    value = format_param.strip().lower()
    if value not in ("json", "arrow"):
        raise UnsupportedFilter(
            f"format 非法：{format_param!r}", hint="可选 json / arrow"
        )
    return value


def _wants_arrow(request: Request, format_param: str | None) -> bool:
    if format_param:
        return format_param.lower() == "arrow"
    return ARROW_MEDIA in request.headers.get("accept", "")


def _etag(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, default=str, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f'W/"{digest}"'


def _respond(
    request: Request,
    *,
    frame: Any,
    meta: dict[str, Any],
    headers: dict[str, str],
    etag: str,
    format_param: str | None,
) -> Response:
    common = {"ETag": etag, "Vary": "Accept, Accept-Encoding", **headers}
    incoming = request.headers.get("if-none-match", "")
    if incoming == "*" or etag in [part.strip() for part in incoming.split(",")]:
        return Response(status_code=304, headers=common)
    if _wants_arrow(request, format_param):
        try:
            import pyarrow as pa
            import pyarrow.ipc as ipc
        except ImportError as exc:  # 依赖缺失不静默降级
            raise ArrowUnavailable(
                "Arrow 响应不可用（未安装 pyarrow）", hint="安装 pyarrow 或改用 JSON"
            ) from exc
        table = pa.Table.from_pandas(frame, preserve_index=False)
        sink = BytesIO()
        with ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        return Response(content=sink.getvalue(), media_type=ARROW_MEDIA, headers=common)
    return JSONResponse(content={"meta": meta, "rows": _rows_payload(frame)}, headers=common)


def _generation(context: ApiContext, spec: DatasetSpec | None) -> str | None:
    """读模型构建代次（X-Data-Generation；未构建过则 None）。"""
    if spec is None:
        return None
    target = spec.storage.read_model
    for row in context.algorithms.list_generations():
        if row.read_model == target:
            return row.generation
    return None


def _watermark_marks(context: ApiContext) -> dict[str, list[datetime]]:
    """按数据集聚合水位（一次读取，供滞后与数据版本令牌共用）。"""
    marks: dict[str, list[datetime]] = {}
    for item in context.meta.list_watermarks():
        if item.watermark_time is None:
            continue
        marks.setdefault(item.dataset, []).append(item.watermark_time)
    return marks


def _freshness_lag(
    context: ApiContext, dataset: str, *, marks: list[datetime] | None = None
) -> int | None:
    """交易日滞后：最近已收盘交易日（16:30 CST 口径）− 水位；无水位返回 None。"""
    if marks is None:
        marks = _watermark_marks(context).get(dataset, [])
    if not marks:
        return None
    mark = max(marks).date()
    latest = StoredTradeCalendar(context.read_engine).last_closed(utcnow())
    if latest is None or latest <= mark:
        return 0
    calendar_table = platform_metadata().tables["ref.trade_calendar"]
    with context.read_engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count(func.distinct(calendar_table.c.trade_date))).where(
                    calendar_table.c.is_open.is_(True),
                    calendar_table.c.trade_date > mark,
                    calendar_table.c.trade_date <= latest,
                )
            ).scalar_one()
        )


def _dataset_version(
    context: ApiContext,
    dataset: str,
    spec: DatasetSpec | None,
    *,
    marks: list[datetime] | None = None,
) -> str | None:
    """数据版本令牌（ETag 组成部分）。

    取「水位 ∪ 最近一次成功运行」——平台写入一律经任务执行（doc-10），
    两者任一推进都会改变令牌；两者都没有时回退到表内最大知识时间（ref 类小表）。
    缺少该令牌时，canonical 数据集的 ETag 不随新数据变化（客户端会拿到陈旧 304）。
    """
    if marks is None:
        marks = _watermark_marks(context).get(dataset, [])
    from fin_data_platform.runtime.schema import job_runs

    with context.writer_engine.connect() as connection:
        last_run = connection.execute(
            select(func.max(job_runs.c.finished_at)).where(
                job_runs.c.dataset == dataset,
                job_runs.c.status == "succeeded",
            )
        ).scalar_one_or_none()
    candidates = [*marks, *([last_run] if last_run is not None else [])]
    if candidates:
        return max(candidates).isoformat()
    if spec is None:
        return None
    table = platform_metadata().tables.get(spec.storage.canonical_table)
    if table is None or "knowledge_time" not in table.c:
        return None
    with context.read_engine.connect() as connection:
        value = connection.execute(
            select(func.max(table.c.knowledge_time))
        ).scalar_one_or_none()
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------- PIT 行（doc-12 §2.2）
@router.get(
    "/datasets/{dataset}/rows",
    summary="PIT 行查询（dataset-generic；latest / as_of / history）",
)
def dataset_rows(
    request: Request,
    context: Context,
    dataset: str,
    version_mode: Annotated[str, Query(description="latest / as_of / history（必填）")] = "",
    as_of: Annotated[str | None, Query(description="ISO8601；as_of 模式必填")] = None,
    as_of_policy: Annotated[str, Query()] = "knowledge",
    fallback_mode: Annotated[str, Query()] = "strict",
    entity_id: Annotated[list[int] | None, Query(description="实体 ID（可重复）")] = None,
    start: Annotated[str | None, Query(description="事件时间起（ISO 日期）")] = None,
    end: Annotated[str | None, Query(description="事件时间止（ISO 日期）")] = None,
    fields: Annotated[str | None, Query(description="逗号分隔字段投影")] = None,
    filters: Annotated[str | None, Query(description="结构化过滤 JSON 数组")] = None,
    order_by: Annotated[str | None, Query(description="逗号分隔；- 前缀为降序")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
    include_meta: Annotated[bool, Query()] = False,
    format: Annotated[str | None, Query(description="json / arrow（覆盖 Accept）")] = None,
) -> Response:
    format = _validate_format(format)
    query = RowsQuery(
        dataset=dataset,
        version_mode=version_mode,
        as_of=_parse_as_of(as_of),
        as_of_policy=as_of_policy,
        fallback_mode=fallback_mode,
        entities=tuple(entity_id or ()),
        window=_window(_parse_date(start, name="start"), _parse_date(end, name="end")),
        fields=_split(fields),
        filters=_parse_filters(filters),
        order_by=_split(order_by),
        limit=limit,
        cursor=cursor,
        include_meta=include_meta,
    )
    spec = context.specs.get(dataset)
    result = read_rows(
        context.read_engine,
        query,
        specs=context.specs,
        data_generation=_generation(context, spec),
    )
    frame = result.frame
    marks = _watermark_marks(context)
    lag = _freshness_lag(context, dataset, marks=marks.get(dataset, []))
    headers = {
        "X-Request-Id": _request_id(request),
        "X-Dataset": dataset,
        "X-Version-Mode": result.meta.version_mode,
        "X-Semantic-Version": str(result.meta.semantic_version),
        "X-Query-Rows": str(result.meta.row_count),
        "X-Query-Cost": (
            f"rows={result.meta.row_count};bytes={int(frame.memory_usage(deep=True).sum())}"
        ),
        "X-Cache": "MISS",
    }
    if result.meta.as_of is not None:
        headers["X-As-Of"] = result.meta.as_of.isoformat()
    if spec is not None:
        headers["X-Read-Model-Version"] = spec.storage.read_model
    if result.meta.data_generation:
        headers["X-Data-Generation"] = result.meta.data_generation
    if lag is not None:
        headers["X-Freshness-Lag"] = str(lag)
    if result.meta.fallback:
        headers["X-Publish-Fallback"] = result.meta.fallback
    meta = {
        "dataset": result.meta.dataset,
        "version_mode": result.meta.version_mode,
        "as_of": result.meta.as_of.isoformat() if result.meta.as_of else None,
        "policy": result.meta.policy,
        "fallback": result.meta.fallback,
        "semantic_version": result.meta.semantic_version,
        "data_generation": result.meta.data_generation,
        "row_count": result.meta.row_count,
        "warnings": result.meta.warnings,
        "generated_at": result.meta.generated_at.isoformat(),
        "next_cursor": result.meta.next_cursor,
    }
    version_token = _dataset_version(context, dataset, spec, marks=marks.get(dataset, []))
    etag = _etag(
        "rows",
        dataset,
        version_mode,
        result.meta.as_of,
        as_of_policy,
        fallback_mode,
        sorted(entity_id or ()),
        start,
        end,
        fields,
        filters,
        order_by,
        limit,
        cursor,
        include_meta,
        _wants_arrow(request, format),
        result.meta.semantic_version,
        result.meta.data_generation,
        version_token,
    )
    return _respond(
        request, frame=frame, meta=meta, headers=headers, etag=etag, format_param=format
    )


# ---------------------------------------------------------------- 访问面 Raw（doc-12 §2.4）
@router.get(
    "/raw/{dataset}/rows",
    summary="访问面 Raw 读取（PIT + 复权口径组合 + 可选日历对齐）",
)
def raw_rows(
    request: Request,
    context: Context,
    dataset: str,
    as_of: Annotated[str, Query(description="ISO8601（必填：PIT 严格，不隐式取 now）")] = "",
    adjust: Annotated[str | None, Query(description="none / qfq / hfq；缺省取字典声明")] = None,
    align_calendar: Annotated[bool, Query()] = False,
    entity_id: Annotated[list[int] | None, Query()] = None,
    start: Annotated[str | None, Query()] = None,
    end: Annotated[str | None, Query()] = None,
    fields: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = MAX_LIMIT,
    format: Annotated[str | None, Query()] = None,
) -> Response:
    parsed_as_of = _parse_as_of(as_of)
    if parsed_as_of is None:
        from fin_data_platform.query import AsOfRequired

        raise AsOfRequired("访问面读取 as_of 必填", hint="PIT 严格；不隐式取 now")
    format = _validate_format(format)
    result = access_read(
        context.read_engine,
        dataset,
        _split(fields) or None,
        as_of=parsed_as_of,
        adjust=adjust,
        entities=tuple(entity_id or ()) or None,
        window=_window(_parse_date(start, name="start"), _parse_date(end, name="end")),
        align_calendar=align_calendar,
        specs=context.specs,
    )
    frame = result.table.to_pandas()
    warnings = list(result.meta.warnings)
    if len(frame) > limit:
        frame = frame.head(limit)
        warnings.append(f"结果超过 limit（{limit}），已截断；大范围请走导出或 PIT 行端点")
    meta = {
        "dataset": result.meta.dataset,
        "as_of": result.meta.as_of.isoformat(),
        "adjust": result.meta.adjust,
        "semantic_version": result.meta.semantic_version,
        "row_count": len(frame),
        "factor_dataset": result.meta.factor_dataset,
        "adjusted_fields": list(result.meta.adjusted_fields),
        "aligned": result.meta.aligned,
        "calendar_dataset": result.meta.calendar_dataset,
        "status_dataset": result.meta.status_dataset,
        "trading_days": result.meta.trading_days,
        "warnings": warnings,
    }
    spec = context.specs.get(dataset)
    headers = {
        "X-Request-Id": _request_id(request),
        "X-Dataset": dataset,
        "X-As-Of": result.meta.as_of.isoformat(),
        "X-Adjust": result.meta.adjust,
        "X-Semantic-Version": str(result.meta.semantic_version),
        "X-Query-Rows": str(len(frame)),
        "X-Query-Cost": (
            f"rows={len(frame)};bytes={int(frame.memory_usage(deep=True).sum())}"
        ),
        "X-Cache": "MISS",
    }
    generation = _generation(context, spec)
    if generation:
        headers["X-Data-Generation"] = generation
    etag = _etag(
        "raw",
        dataset,
        result.meta.as_of,
        adjust,
        sorted(entity_id or ()),
        start,
        end,
        fields,
        align_calendar,
        limit,
        _wants_arrow(request, format),
        result.meta.semantic_version,
        generation,
        _dataset_version(
            context, dataset, spec, marks=_watermark_marks(context).get(dataset, [])
        ),
    )
    return _respond(
        request, frame=frame, meta=meta, headers=headers, etag=etag, format_param=format
    )


# ---------------------------------------------------------------- 因子（doc-12 §2.4）
@router.get(
    "/factors/{output}/rows",
    summary="因子读取（严格 as_of 对齐；可 pin algorithm_id）",
)
def factor_rows(
    request: Request,
    context: Context,
    output: str,
    as_of: Annotated[str, Query(description="ISO8601（必填）")] = "",
    dataset: Annotated[str | None, Query()] = None,
    algorithm_id: Annotated[str | None, Query(description="pin 算法版本（缺省当前）")] = None,
    entity_id: Annotated[list[int] | None, Query()] = None,
    start: Annotated[str | None, Query()] = None,
    end: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = MAX_LIMIT,
    format: Annotated[str | None, Query()] = None,
) -> Response:
    from fin_data_platform.query import AsOfRequired

    parsed_as_of = _parse_as_of(as_of)
    if parsed_as_of is None:
        raise AsOfRequired("因子读取 as_of 必填", hint="严格对齐；不隐式取 now")
    format = _validate_format(format)
    api = context.factors
    if api is None:  # 注入缺失时按上下文即时构建（测试替身可覆盖）
        from fin_data_platform.derived.factor_api import FactorAPI

        api = FactorAPI(
            context.writer_engine, specs=context.specs, store=context.algorithms
        )
    result = api.read(
        output,
        as_of=parsed_as_of,
        dataset=dataset,
        algorithm_id=algorithm_id,
        entities=tuple(entity_id or ()) or None,
        window=_window(_parse_date(start, name="start"), _parse_date(end, name="end")),
    )
    frame = result.values.to_pandas()
    warnings: list[str] = []
    if len(frame) > limit:
        frame = frame.head(limit)
        warnings.append(f"结果超过 limit（{limit}），已截断")
    meta = {
        "output": result.output,
        "dataset": result.meta.dataset,
        "algorithm_id": result.meta.algorithm_id,
        "algorithm_version": result.meta.algorithm_version,
        "as_of": result.meta.as_of.isoformat(),
        "materialized": result.meta.materialized,
        "data_generation": result.meta.data_generation,
        "computed_at": (
            result.meta.computed_at.isoformat() if result.meta.computed_at else None
        ),
        "upstream_fingerprint": result.meta.upstream_fingerprint,
        "row_count": len(frame),
        "warnings": warnings,
    }
    headers = {
        "X-Request-Id": _request_id(request),
        "X-Dataset": result.meta.dataset,
        "X-As-Of": result.meta.as_of.isoformat(),
        "X-Algorithm-Id": result.meta.algorithm_id,
        "X-Query-Rows": str(len(frame)),
        "X-Query-Cost": (
            f"rows={len(frame)};bytes={int(frame.memory_usage(deep=True).sum())}"
        ),
        "X-Cache": "MISS",
    }
    if result.meta.data_generation:
        headers["X-Data-Generation"] = result.meta.data_generation
    etag = _etag(
        "factor",
        result.meta.dataset,
        output,
        result.meta.algorithm_id,
        result.meta.algorithm_version,
        result.meta.as_of,
        sorted(entity_id or ()),
        start,
        end,
        limit,
        _wants_arrow(request, format),
        result.meta.data_generation,
    )
    return _respond(
        request, frame=frame, meta=meta, headers=headers, etag=etag, format_param=format
    )


# ---------------------------------------------------------------- 新鲜度（doc-12 §2.1）
@router.get("/freshness", summary="新鲜度（水位 / 交易日滞后 / 质量覆盖）")
def freshness(context: Context) -> dict[str, Any]:
    from fin_data_platform.quality import fetch_summary

    marks_by_dataset = _watermark_marks(context)
    last_success: dict[str, datetime] = {}
    from fin_data_platform.runtime.schema import job_runs

    with context.writer_engine.connect() as connection:
        for row in connection.execute(
            select(job_runs.c.dataset, func.max(job_runs.c.finished_at))
            .where(job_runs.c.status == "succeeded")
            .group_by(job_runs.c.dataset)
        ):
            if row[1] is not None:
                last_success[str(row[0])] = row[1]
    quality = fetch_summary(context.writer_engine)
    quality_by_dataset = {entry["dataset"]: entry for entry in quality["datasets"]}
    # 数据集全集：未采集 / 未跑质量的也列出（watermark=None），便于监控缺失
    datasets = sorted(context.specs.keys())
    items: list[dict[str, Any]] = []
    for dataset in datasets:
        marks = marks_by_dataset.get(dataset, [])
        mark = max(marks) if marks else None
        record: dict[str, Any] = {
            "dataset": dataset,
            "watermark": mark.date().isoformat() if mark else None,
            "lag_days": _freshness_lag(context, dataset, marks=marks),
            "last_success_at": (
                last_success[dataset].isoformat() if dataset in last_success else None
            ),
        }
        q = quality_by_dataset.get(dataset)
        if q is not None:
            record["coverage_ratio"] = q.get("coverage_ratio")
            record["quality_failed"] = q.get("failed", 0) + q.get("error", 0)
        items.append(record)
    return {
        "day": quality.get("day"),
        "generated_at": quality.get("generated_at"),
        "datasets": items,
    }
