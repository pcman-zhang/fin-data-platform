"""分块批量导出（Parquet / Arrow IPC）：内存有界、不逐标的拉取（TASK-3.10）。

- **读取**：复用 PIT 查询内核（``query.read_rows``）——语义与在线读取一致（同一
  ``version_mode`` / ``as_of`` / 过滤与字段口径）；
- **分块**：**实体批（默认 500）× 时间块（默认 90 天）**；未给窗口时取表内事件时间范围；
  单块被查询上限截断即报错（提示缩小分块），不静默截断；
- **写出**：Parquet（zstd）或 Arrow IPC；**目标 schema 由物理表列类型预先构造**，
  空块跳过、逐块 cast（避免空块 / 全 NULL 列造成 schema 错配）；
  先写临时文件、成功后原子替换（失败不留半成品）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Boolean, Date, DateTime, Engine, Float, Integer, Numeric, func, select

from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.export.store import ExportRequest
from fin_data_platform.query import MAX_LIMIT, FilterClause, RowsQuery, read_rows
from fin_data_platform.query.reader import META_COLUMNS, platform_metadata

DEFAULT_ENTITY_BATCH = 500
DEFAULT_CHUNK_DAYS = 90


@dataclass(frozen=True, slots=True)
class ExportPlan:
    dataset: str
    version_mode: str
    as_of: datetime | None
    as_of_policy: str
    fallback_mode: str
    filters: tuple[Mapping[str, Any], ...]
    fields: tuple[str, ...]
    entities: tuple[int, ...]
    window: tuple[date, date] | None
    include_meta: bool


def write_export(
    engine: Engine,
    request: ExportRequest,
    path: Path,
    *,
    specs: Mapping[str, DatasetSpec] | None = None,
    entity_batch: int = DEFAULT_ENTITY_BATCH,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
) -> tuple[int, int]:
    """执行导出并写出产物；返回 ``(rows, bytes)``。"""
    dictionary = dict(specs) if specs is not None else load_all()
    spec = dictionary.get(request.dataset)
    if spec is None:
        raise ValueError(f"数据集不存在：{request.dataset}")
    plan = _plan(request)
    metadata = platform_metadata()
    table = metadata.tables.get(spec.storage.canonical_table)
    if table is None:
        raise ValueError(f"数据集表未落地：{spec.storage.canonical_table}")
    event = _event_field(spec, table)
    selected = _selected_fields(plan, spec, table)
    if not selected:
        raise ValueError("导出字段为空（投影均被隐藏）")
    target_schema = _arrow_schema(table, selected)

    window = plan.window
    if window is None and event is not None:
        window = _full_range(engine, table, event, plan.entities)
    groups = _entity_groups(engine, table, event, window, plan, entity_batch)
    chunks = _time_chunks(window, chunk_days) if event is not None else [None]

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    if temp.exists():
        temp.unlink()
    parquet = None
    stream = None
    sink = None
    rows_total = 0
    try:
        for entities in groups:
            for chunk in chunks:
                frame = _read_chunk(
                    engine, plan, entities=entities, window=chunk, specs=dictionary
                )
                arrow = pa.Table.from_pandas(frame, preserve_index=False).cast(target_schema)
                if arrow.num_rows == 0:
                    continue  # 空块跳过（schema 由目标 schema 保证一致）
                if request.format == "arrow":
                    if stream is None:
                        sink = pa.OSFile(str(temp), "wb")
                        stream = pa.ipc.new_stream(sink, target_schema)
                    stream.write_table(arrow)
                else:
                    if parquet is None:
                        parquet = pq.ParquetWriter(
                            temp, target_schema, compression="zstd"
                        )
                    parquet.write_table(arrow)
                rows_total += arrow.num_rows
        if parquet is None and stream is None:
            # 全空导出：写出带目标 schema 的空产物
            if request.format == "arrow":
                sink = pa.OSFile(str(temp), "wb")
                stream = pa.ipc.new_stream(sink, target_schema)
                stream.write_table(pa.Table.from_batches([], schema=target_schema))
            else:
                parquet = pq.ParquetWriter(temp, target_schema, compression="zstd")
                parquet.write_table(pa.Table.from_batches([], schema=target_schema))
    except Exception:
        if temp.exists():
            temp.unlink()
        raise
    finally:
        if parquet is not None:
            parquet.close()
        if stream is not None:
            stream.close()
        if sink is not None:
            sink.close()
    temp.replace(path)  # 原子替换：失败路径不留半成品
    return rows_total, path.stat().st_size


def _plan(request: ExportRequest) -> ExportPlan:
    params = request.params
    window: tuple[date, date] | None = None
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None and end is not None:
        if start > end:
            raise ValueError(f"窗口非法：start={start} > end={end}")
        window = (start, end)
    elif start is not None or end is not None:
        raise ValueError("窗口需同时提供 start 与 end")
    return ExportPlan(
        dataset=request.dataset,
        version_mode=str(params.get("version_mode") or "latest"),
        as_of=_as_datetime(params.get("as_of")),
        as_of_policy=str(params.get("as_of_policy") or "knowledge"),
        fallback_mode=str(params.get("fallback_mode") or "strict"),
        filters=tuple(params.get("filters") or ()),
        fields=tuple(str(name) for name in (params.get("fields") or ())),
        # 去重：同一实体跨批重复会导致结果行重复
        entities=tuple(dict.fromkeys(int(item) for item in (params.get("entities") or ()))),
        window=window,
        include_meta=bool(params.get("include_meta", False)),
    )


def _read_chunk(
    engine: Engine,
    plan: ExportPlan,
    *,
    entities: Sequence[int] | None,
    window: tuple[date, date] | None,
    specs: Mapping[str, DatasetSpec],
) -> Any:
    query = RowsQuery(
        dataset=plan.dataset,
        version_mode=plan.version_mode,
        as_of=plan.as_of,
        as_of_policy=plan.as_of_policy,
        fallback_mode=plan.fallback_mode,
        entities=tuple(entities or ()),
        window=window,
        fields=plan.fields,
        filters=tuple(_filter_clause(item) for item in plan.filters),
        limit=MAX_LIMIT,
        include_meta=plan.include_meta,
    )
    result = read_rows(engine, query, specs=specs)
    if result.meta.next_cursor is not None:  # 被 limit 截断（恰好 MAX_LIMIT 行不算）
        raise ValueError(
            f"单块行数超过查询上限（{MAX_LIMIT}）：请缩小实体批 / 时间块"
            f"（当前 entities={len(entities or ())}，window={window}）"
        )
    return result.frame


def _entity_groups(
    engine: Engine,
    table: Any,
    event: str | None,
    window: tuple[date, date] | None,
    plan: ExportPlan,
    entity_batch: int,
) -> list[tuple[int, ...] | None]:
    if "entity_id" not in table.c:
        return [None]
    if plan.entities:
        ids = list(plan.entities)
    else:
        statement = select(table.c.entity_id).distinct()
        if event is not None and window is not None:
            statement = statement.where(table.c[event].between(*window))
        with engine.connect() as connection:
            ids = [int(row[0]) for row in connection.execute(statement)]
    batch = max(int(entity_batch), 1)
    return [tuple(ids[index : index + batch]) for index in range(0, len(ids), batch)]


def _full_range(
    engine: Engine, table: Any, event: str, entities: Sequence[int]
) -> tuple[date, date] | None:
    statement = select(func.min(table.c[event]), func.max(table.c[event]))
    if entities:
        if "entity_id" not in table.c:
            raise ValueError("该数据集无 entity_id 列，不支持 entities 过滤")
        statement = statement.where(table.c.entity_id.in_(list(entities)))
    with engine.connect() as connection:
        low, high = connection.execute(statement).one()
    if low is None or high is None:
        return None
    return (_as_date(low), _as_date(high))  # type: ignore[return-value]


def _time_chunks(
    window: tuple[date, date] | None, chunk_days: int
) -> list[tuple[date, date] | None]:
    if window is None:
        return [None]
    span = max(int(chunk_days), 1)
    chunks: list[tuple[date, date] | None] = []
    cursor = window[0]
    while cursor <= window[1]:
        chunk_end = min(cursor + timedelta(days=span - 1), window[1])
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _selected_fields(plan: ExportPlan, spec: DatasetSpec, table: Any) -> list[str]:
    """与 ``query.read_rows`` 相同的投影规则（含 history / include_meta 口径）。"""
    available = [field.name for field in spec.fields]
    requested = list(plan.fields) if plan.fields else list(available)
    selected = [name for name in requested if name in table.c]
    if plan.version_mode == "history":
        for name in ("knowledge_time", "version"):
            if name in table.c and name not in selected:
                selected.append(name)
    elif not plan.include_meta:
        selected = [name for name in selected if name not in META_COLUMNS]
    return selected


def _arrow_schema(table: Any, columns: Sequence[str]) -> pa.Schema:
    """由物理表列类型构造目标 schema（空块 / 全 NULL 列不改变 schema）。"""
    return pa.schema(
        [pa.field(name, _arrow_type(table.c[name])) for name in columns]
    )


def _arrow_type(column: Any) -> pa.DataType:
    type_ = column.type
    if isinstance(type_, Boolean):
        return pa.bool_()
    if isinstance(type_, Integer):
        return pa.int64()
    if isinstance(type_, (Float, Numeric)):
        return pa.float64()
    if isinstance(type_, DateTime):
        return pa.timestamp("us", tz="UTC") if type_.timezone else pa.timestamp("us")
    if isinstance(type_, Date):
        return pa.date32()
    return pa.string()


def _event_field(spec: DatasetSpec, table: Any) -> str | None:
    for field in spec.fields:
        if field.pit_role == "event_time" and field.name in table.c:
            return field.name
    return None


def _filter_clause(item: Mapping[str, Any]) -> FilterClause:
    if "field" not in item or "op" not in item:
        raise ValueError("filters 条目需含 field / op / value")
    return FilterClause(
        field=str(item["field"]), op=str(item["op"]), value=item.get("value")
    )


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
