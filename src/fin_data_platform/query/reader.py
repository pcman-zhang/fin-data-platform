"""dataset-generic PIT 行查询（doc-12 §2.2 / §3 / §4；REST 与未来 SDK 共用）。

- **版本模式**：``latest``（当前最新）/ ``as_of``（知识时间可见最新；``as_of`` 必填）/
  ``history``（全版本，审计；始终含 ``version`` / ``knowledge_time``）；
- **as_of 语义**：``knowledge``（默认）/ ``publish``（按发布时刻；数据集缺
  ``publish_time`` 时按 ``fallback_mode``：``strict`` 报错 / ``allow`` 显式回退）；
- **过滤**：结构化 AST（op 白名单，字段必须已登记）；**游标**：keyset
  （``order_by`` + 业务键 + 物理键，方向感知）；
- **裁剪**：``fields`` 投影；``include_meta`` 控制版本类列（默认隐藏）；
- **读不写库**：仅 SELECT；不隐式替换语义（宁可报错）。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from typing import Any

import pandas as pd
from sqlalchemy import (
    Date,
    DateTime,
    Engine,
    Float,
    Integer,
    Numeric,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy.sql import ColumnElement

from fin_data_platform.access import SUPPORTED_PIT_CLASSES, UnsupportedPitClass
from fin_data_platform.access.reader import normalize_as_of
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.query.errors import (
    AsOfRequired,
    InvalidAsOfPolicy,
    InvalidDataset,
    InvalidField,
    InvalidVersionMode,
    PublishTimeMissing,
    UnsupportedFilter,
    VersionModeRequired,
)
from fin_data_platform.query.models import (
    AS_OF_POLICIES,
    FALLBACK_MODES,
    VERSION_MODES,
    FilterClause,
    RowsMeta,
    RowsQuery,
    RowsResult,
)
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.storage.schema import build_metadata

#: 默认隐藏的平台管理列（``include_meta=True`` 时返回；history 保留审计列）
META_COLUMNS = frozenset(
    {"knowledge_time", "publish_time", "version", "provider", "ingest_time"}
)
#: 过滤算子白名单（doc-12 §4.1）
FILTER_OPS = frozenset({"eq", "ne", "in", "between", "gt", "gte", "lt", "lte", "is_null"})
DEFAULT_LIMIT = 1000
MAX_LIMIT = 50000


@lru_cache(maxsize=1)
def platform_metadata():  # type: ignore[no-untyped-def]
    """平台全量 metadata（进程内缓存；构建开销较大）。"""
    return build_metadata()[0]


@dataclass(frozen=True, slots=True)
class _RankPlan:
    rank: ColumnElement[Any]
    where: ColumnElement[bool]
    policy_used: str
    warnings: tuple[str, ...]


def read_rows(
    engine: Engine,
    query: RowsQuery,
    *,
    specs: Mapping[str, DatasetSpec] | None = None,
    data_generation: str | None = None,
    clock: Any = utcnow,
) -> RowsResult:
    """执行 PIT 行查询（校验 + 单次 SELECT；返回 DataFrame 与元数据）。"""
    dictionary = specs if specs is not None else load_all()
    spec = dictionary.get(query.dataset)
    if spec is None:
        raise InvalidDataset(
            f"数据集不存在：{query.dataset}", hint="数据集清单见 GET /v1/datasets"
        )
    if spec.pit_class not in SUPPORTED_PIT_CLASSES:
        raise UnsupportedPitClass(
            f"数据集 pit_class={spec.pit_class} 暂不支持 PIT 行查询",
            hint="当前支持 market / versioned / snapshot（scd2 区间语义见路线图）",
        )
    if not query.version_mode:
        raise VersionModeRequired(
            "version_mode 必填", hint="显式选择 latest / as_of / history，不做隐式默认"
        )
    if query.version_mode not in VERSION_MODES:
        raise InvalidVersionMode(
            f"version_mode 非法：{query.version_mode}",
            hint="可选 latest / as_of / history",
        )
    if query.as_of_policy not in AS_OF_POLICIES:
        raise InvalidAsOfPolicy(
            f"as_of_policy 非法：{query.as_of_policy}", hint="可选 knowledge / publish"
        )
    if query.fallback_mode not in FALLBACK_MODES:
        raise InvalidAsOfPolicy(
            f"fallback_mode 非法：{query.fallback_mode}", hint="可选 strict / allow"
        )

    now = clock()
    if query.version_mode == "as_of":
        if query.as_of is None:
            raise AsOfRequired(
                "version_mode=as_of 时 as_of 必填", hint="as_of 不隐式取 now"
            )
        effective_as_of = normalize_as_of(query.as_of)
        meta_as_of: datetime | None = effective_as_of
    elif query.version_mode == "latest":
        effective_as_of = normalize_as_of(now)
        meta_as_of = None
    else:
        effective_as_of = None
        meta_as_of = None

    metadata = platform_metadata()
    table = metadata.tables.get(spec.storage.canonical_table)
    if table is None:
        raise InvalidDataset(
            f"数据集表未落地：{spec.storage.canonical_table}", hint="检查迁移与字典"
        )

    available = [field.name for field in spec.fields]
    requested = list(dict.fromkeys(query.fields)) if query.fields else list(available)
    unknown = [name for name in requested if name not in available]
    if unknown:
        raise InvalidField(
            f"字段不存在：{unknown}",
            hint=f"{query.dataset} 可用字段见 GET /v1/datasets/{query.dataset}/schema",
        )
    missing_physical = [name for name in requested if name not in table.c]
    if missing_physical:
        raise InvalidField(
            f"字段未在物理表落地：{missing_physical}", hint="检查迁移与字典一致性"
        )
    selected = list(requested)
    if query.version_mode == "history":
        for name in ("knowledge_time", "version"):
            if name in table.c and name not in selected:
                selected.append(name)
    elif not query.include_meta:
        selected = [name for name in selected if name not in META_COLUMNS]
    if not selected:
        raise InvalidField(
            "投影为空：请求字段均被隐藏",
            hint="include_meta=true 或调整 fields",
        )

    warnings: list[str] = []
    fallback: str | None = None
    if query.version_mode == "history":
        view: Any = table
        conditions: list[ColumnElement[bool]] = []
        if query.as_of is not None:
            warnings.append("history 模式返回全部版本，as_of 被忽略")
    else:
        assert effective_as_of is not None  # latest / as_of 均已在上面求值
        plan = _rank_plan(
            spec,
            table,
            policy=query.as_of_policy,
            fallback_mode=query.fallback_mode,
            as_of=effective_as_of,
        )
        warnings.extend(plan.warnings)
        if plan.policy_used != query.as_of_policy:
            fallback = plan.policy_used
        ranked = select(*table.c, plan.rank).where(plan.where)
        view = ranked.subquery("pit_rows")
        conditions = [view.c._rank == 1]

    if query.entities:
        if "entity_id" not in view.c:
            raise UnsupportedFilter(
                "entity_id 过滤不适用于该数据集", hint="该数据集无 entity_id 列"
            )
        conditions.append(view.c.entity_id.in_(list(query.entities)))
    if query.window is not None:
        event = _event_field(spec, view)
        if event is None:
            raise UnsupportedFilter(
                "start/end 过滤不适用于该数据集",
                hint="数据集无事件时间列（字典 pit_role=event_time）",
            )
        start, end = query.window
        if start > end:
            raise UnsupportedFilter("窗口非法：start > end")
        conditions.append(view.c[event].between(start, end))
    for clause in query.filters:
        conditions.append(_filter_clause(spec, view, clause))

    order_names, directions = _order_plan(spec, view, query.order_by)
    order_exprs = [
        view.c[name].desc() if desc else view.c[name].asc()
        for name, desc in zip(order_names, directions, strict=True)
    ]
    key_names = _key_plan(spec, view, order_names)
    if query.cursor:
        conditions.append(
            _cursor_condition(view, order_names, directions, key_names, query.cursor)
        )

    limit = query.limit or DEFAULT_LIMIT
    if limit > MAX_LIMIT:
        warnings.append(f"limit 超出上限（{MAX_LIMIT}），已截断")
        limit = MAX_LIMIT
    cursor_columns = [*order_names, *key_names]
    fetch_columns = list(dict.fromkeys([*selected, *cursor_columns]))
    statement = (
        select(*[view.c[name] for name in fetch_columns])
        .where(*conditions)
        .order_by(*order_exprs, *[view.c[name].asc() for name in key_names])
        .limit(limit + 1)
    )
    with engine.connect() as connection:
        records = connection.execute(statement).mappings().all()
    has_more = len(records) > limit
    records = records[:limit]
    frame = pd.DataFrame([dict(record) for record in records], columns=selected)
    next_cursor = None
    if has_more and records:
        last = dict(records[-1])
        cursor_values = [last[name] for name in cursor_columns]
        if any(value is None for value in cursor_values):
            warnings.append(
                "边界行排序键含空值：本次不提供游标（请改用非空排序列或过滤）"
            )
        else:
            next_cursor = _encode_cursor(cursor_columns, cursor_values)
    meta = RowsMeta(
        dataset=query.dataset,
        version_mode=query.version_mode,
        as_of=meta_as_of,
        policy=query.as_of_policy,
        fallback=fallback,
        semantic_version=spec.semantic_version,
        data_generation=data_generation,
        row_count=len(frame),
        generated_at=now,
        warnings=warnings,
        next_cursor=next_cursor,
    )
    return RowsResult(frame=frame, meta=meta)


def _rank_plan(
    spec: DatasetSpec,
    table: Any,
    *,
    policy: str,
    fallback_mode: str,
    as_of: datetime,
) -> _RankPlan:
    """版本去重计划：按知识时间 / 发布时刻取每个业务键的最新可见版本。"""
    if "knowledge_time" not in table.c:
        raise UnsupportedFilter(
            "数据集缺少知识时间列，无法按 PIT 读取", hint="见字典 physical_key"
        )
    partition = [table.c[name] for name in spec.business_key if name in table.c]
    if not partition:
        raise UnsupportedFilter(
            "数据集缺少业务键，无法按 PIT 去重", hint="见字典 business_key"
        )
    if len(partition) != len(spec.business_key):
        raise UnsupportedFilter(
            "业务键列未全部落地，无法按 PIT 去重", hint="见字典 business_key"
        )
    version_order = [table.c.version.desc()] if "version" in table.c else []
    knowledge_order = [table.c.knowledge_time.desc()]
    if policy == "publish":
        if "publish_time" in table.c:
            rank = (
                func.row_number()
                .over(
                    partition_by=partition,
                    order_by=[table.c.publish_time.desc(), *version_order],
                )
                .label("_rank")
            )
            return _RankPlan(
                rank=rank,
                where=table.c.publish_time <= as_of,
                policy_used="publish",
                warnings=(),
            )
        if fallback_mode == "strict":
            raise PublishTimeMissing(
                "数据集无 publish_time，无法按发布语义读取",
                hint="改用 as_of_policy=knowledge，或 fallback_mode=allow 显式回退",
            )
        rank = (
            func.row_number()
            .over(
                partition_by=partition,
                order_by=[*version_order, *knowledge_order],
            )
            .label("_rank")
        )
        return _RankPlan(
            rank=rank,
            where=table.c.knowledge_time <= as_of,
            policy_used="knowledge",
            warnings=(
                "publish_time 缺失：已回退 knowledge 口径（fallback_mode=allow）",
            ),
        )
    # 与访问面（access.reader）同口径：版本优先、知识时间次序
    rank = (
        func.row_number()
        .over(
            partition_by=partition,
            order_by=[*version_order, *knowledge_order],
        )
        .label("_rank")
    )
    return _RankPlan(
        rank=rank,
        where=table.c.knowledge_time <= as_of,
        policy_used="knowledge",
        warnings=(),
    )


def _event_field(spec: DatasetSpec, view: Any) -> str | None:
    for field in spec.fields:
        if field.pit_role == "event_time" and field.name in view.c:
            return field.name
    return None


def _filter_clause(spec: DatasetSpec, view: Any, clause: FilterClause) -> ColumnElement[bool]:
    known = {field.name for field in spec.fields}
    if clause.field not in view.c or clause.field not in known:
        raise InvalidField(f"过滤字段不存在：{clause.field}")
    if clause.op not in FILTER_OPS:
        raise UnsupportedFilter(
            f"不支持的过滤算子：{clause.op}", hint=f"可选 {'/'.join(sorted(FILTER_OPS))}"
        )
    column = view.c[clause.field]
    value = clause.value
    if clause.op == "eq":
        return column == _filter_value(column, value, op=clause.op)
    if clause.op == "ne":
        return column != _filter_value(column, value, op=clause.op)
    if clause.op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise UnsupportedFilter("op=in 需要非空数组")
        return column.in_(
            [_filter_value(column, item, op=clause.op) for item in value]
        )
    if clause.op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise UnsupportedFilter("op=between 需要 [start, end]")
        lower, upper = (_filter_value(column, item, op=clause.op) for item in value)
        return column.between(lower, upper)
    if clause.op in ("gt", "gte", "lt", "lte"):
        bound = _filter_value(column, value, op=clause.op)
        return {
            "gt": column > bound,
            "gte": column >= bound,
            "lt": column < bound,
            "lte": column <= bound,
        }[clause.op]
    # is_null：缺省 true（IS NULL）；仅接受布尔
    if value is None or value is True:
        return column.is_(None)
    if value is False:
        return column.is_not(None)
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
        return (
            column.is_(None)
            if value.lower() in ("true", "1", "yes")
            else column.is_not(None)
        )
    raise UnsupportedFilter("op=is_null 的 value 需为布尔（缺省 true = IS NULL）")


def _filter_value(column: Any, value: Any, *, op: str) -> Any:
    """按列类型校验/转换过滤值：非法值在绑定前报 422（而不是 500）。"""
    if value is None:
        if op in ("gt", "gte", "lt", "lte"):
            raise UnsupportedFilter(f"op={op} 需要 value")
        return None
    if isinstance(value, (dict, list, tuple)):
        raise UnsupportedFilter(f"op={op} 的值类型非法（需标量）")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if isinstance(column.type, Integer):
            try:
                return int(value)
            except ValueError as exc:
                raise UnsupportedFilter(f"op={op} 需要整数：{value!r}") from exc
        if isinstance(column.type, (Float, Numeric)):
            try:
                return float(value)
            except ValueError as exc:
                raise UnsupportedFilter(f"op={op} 需要数值：{value!r}") from exc
        if isinstance(column.type, DateTime):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise UnsupportedFilter(f"op={op} 需要 ISO 时间：{value!r}") from exc
        if isinstance(column.type, Date):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise UnsupportedFilter(f"op={op} 需要 ISO 日期：{value!r}") from exc
    return value


def _order_plan(
    spec: DatasetSpec, view: Any, order_by: Sequence[str]
) -> tuple[tuple[str, ...], tuple[bool, ...]]:
    names: list[str] = []
    directions: list[bool] = []
    requested = tuple(order_by) if order_by else tuple(spec.business_key)
    for item in requested:
        desc = item.startswith("-")
        name = item[1:] if desc else item
        if name not in view.c:
            raise InvalidField(f"排序字段不存在：{name}")
        if name not in names:
            names.append(name)
            directions.append(desc)
    if not names:
        raise UnsupportedFilter("无法确定排序字段（order_by 与 business_key 均为空）")
    return tuple(names), tuple(directions)


def _key_plan(spec: DatasetSpec, view: Any, order_names: Sequence[str]) -> tuple[str, ...]:
    keys: list[str] = []
    for name in [*spec.business_key, *spec.physical_key]:
        if name in view.c and name not in order_names and name not in keys:
            keys.append(name)
    return tuple(keys)


def _cursor_condition(
    view: Any,
    order_names: Sequence[str],
    directions: Sequence[bool],
    key_names: Sequence[str],
    cursor: str,
) -> ColumnElement[bool]:
    payload = _decode_cursor(cursor)
    expected = [*order_names, *key_names]
    if payload.get("columns") != expected:
        raise UnsupportedFilter(
            "游标与 order_by 不匹配", hint="请使用响应中的 next_cursor，勿混用排序"
        )
    values = payload.get("values") or []
    if len(values) != len(expected):
        raise UnsupportedFilter("游标损坏")
    if any(value is None for value in values):
        raise UnsupportedFilter(
            "游标含空值键", hint="该排序下不可分页；请改用非空排序列或过滤"
        )
    columns = [view.c[name] for name in expected]
    dirs = [*directions, *([False] * len(key_names))]
    clauses: list[ColumnElement[bool]] = []
    for index, (column, desc, value) in enumerate(
        zip(columns, dirs, values, strict=True)
    ):
        prefix = [columns[j] == _coerce_cursor_value(columns[j], values[j]) for j in range(index)]
        coerced = _coerce_cursor_value(column, value)
        comparison = column < coerced if desc else column > coerced
        clauses.append(and_(*prefix, comparison))
    return or_(*clauses)


def _coerce_cursor_value(column: Any, value: Any) -> Any:
    """游标值反序列化（严格：类型不合法报 422，不把绑定错误留给数据库）。"""
    if isinstance(value, (dict, list, tuple)):
        raise UnsupportedFilter("游标值类型非法")
    if isinstance(value, str):
        if isinstance(column.type, DateTime):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise UnsupportedFilter("游标时间值非法") from exc
        if isinstance(column.type, Date):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise UnsupportedFilter("游标日期值非法") from exc
        if isinstance(column.type, Integer):
            try:
                return int(value)
            except ValueError as exc:
                raise UnsupportedFilter("游标整数值非法") from exc
        if isinstance(column.type, (Float, Numeric)):
            try:
                return float(value)
            except ValueError as exc:
                raise UnsupportedFilter("游标数值非法") from exc
    return value


def _encode_cursor(columns: Sequence[str], values: Sequence[Any]) -> str:
    payload = {
        "columns": list(columns),
        "values": [
            value.isoformat() if isinstance(value, (date, datetime)) else value
            for value in values
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise UnsupportedFilter("游标非法", hint="请使用响应中的 next_cursor") from exc
    if not isinstance(payload, dict):
        raise UnsupportedFilter("游标非法")
    return payload
