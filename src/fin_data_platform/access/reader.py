"""规范化读取（访问面核心；doc-10 §3.1、doc-11 §3.7）。

职责：

1. **PIT 读取**：``knowledge_time <= as_of`` + 每业务键最高 ``version``（重述去重）；
   区间/事件窗口与实体过滤；
2. **口径组合**：复权（``qfq = raw × f / f_anchor``、``hfq = raw × f``；
   锚点 = ``as_of`` 可见因子中按事件时间最新的一条）——**只在这里实现一次**，
   按需读取与读模型内联（``read_sql(literal=True)``）共用同一 SQL；
3. 口径由数据字典声明（``adjust`` 块；未声明 = 无复权），不支持的口径报
   :class:`~fin_data_platform.access.errors.UnsupportedAdjust`，不静默替换。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, bindparam, text

from fin_data_platform.access.errors import (
    AccessError,
    UnknownDataset,
    UnknownField,
    UnsupportedAdjust,
    UnsupportedPitClass,
)
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec

if TYPE_CHECKING:
    import pyarrow as pa

#: v1 支持的 pit_class（均带 knowledge_time + version）
SUPPORTED_PIT_CLASSES = frozenset({"market", "versioned", "snapshot"})

#: 有效口径取值（``raw`` = 不调整；字典 default 的 ``none`` 归一为 ``raw``）
ADJUST_MODES = frozenset({"raw", "qfq", "hfq"})


@dataclass(frozen=True, slots=True)
class ReadMeta:
    """读取元数据（PIT 与口径审计）。"""

    dataset: str
    as_of: datetime
    adjust: str
    semantic_version: int
    row_count: int
    factor_dataset: str | None = None
    #: 实际被复权的字段（请求口径作用于无可复权字段时为空——审计口径以本字段为准）
    adjusted_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class ReadResult:
    dataset: str
    table: pa.Table
    meta: ReadMeta


def normalize_as_of(value: datetime) -> datetime:
    """统一为 naive UTC（存储层口径；aware 输入先转 UTC）。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _quote(name: str) -> str:
    return f'"{name}"'


def _sql_literal(value: date | datetime) -> str:
    """时间值 → SQL 字面量（跨 SQLite / PostgreSQL / DuckDB 的纯字符串形式）。"""
    if isinstance(value, datetime):
        return f"'{value:%Y-%m-%d %H:%M:%S.%f}'"
    return f"'{value:%Y-%m-%d}'"


def _event_time_field(spec: DatasetSpec) -> str | None:
    for item in spec.fields:
        if item.pit_role == "event_time":
            return item.name
    return None


def dataset_asof_sql(
    spec: DatasetSpec,
    fields: Sequence[str],
    *,
    as_of: datetime,
    entity_ids: Sequence[int] | None = None,
    window: tuple[date, date] | None = None,
    literal: bool = False,
) -> tuple[str, dict[str, Any]]:
    """按字典构造 as-of 读取 SQL（业务键 + 字段；去重取当前可见版本）。

    ``literal=True`` 时把参数渲染为 SQL 字面量（返回空参数；供内联视图），
    否则返回命名参数（供 :func:`read` 绑定）。
    """
    if spec.pit_class not in SUPPORTED_PIT_CLASSES:
        raise UnsupportedPitClass(
            f"{spec.dataset}: pit_class={spec.pit_class} 暂不支持规范化读取",
            hint="v1 支持 market / versioned / snapshot",
        )
    business_key = list(spec.business_key)
    selected = list(dict.fromkeys([*business_key, *fields]))
    columns = ", ".join(_quote(name) for name in selected)
    partition = ", ".join(_quote(name) for name in business_key)
    params: dict[str, Any] = {} if literal else {"as_of": normalize_as_of(as_of)}

    if literal:
        conditions = [f"knowledge_time <= {_sql_literal(normalize_as_of(as_of))}"]
    else:
        conditions = ["knowledge_time <= :as_of"]
    if entity_ids is not None:
        if "entity_id" not in business_key:
            raise UnknownField(
                f"{spec.dataset}: 业务键不含 entity_id，无法按实体过滤",
                hint="请改用窗口过滤或全量读取",
            )
        if literal:
            if not entity_ids:
                conditions.append("1 = 0")  # 空集合：与绑定路径的 0 行语义一致
            else:
                values = ", ".join(str(value) for value in entity_ids)
                conditions.append(f"entity_id IN ({values})")
        else:
            conditions.append("entity_id IN :entity_ids")
            params["entity_ids"] = tuple(entity_ids)
    event_field = _event_time_field(spec)
    if window is not None:
        if event_field is None:
            raise UnknownField(
                f"{spec.dataset}: 无 event_time 字段，无法按窗口过滤",
                hint="请移除窗口参数或补充字典 pit_role",
            )
        if literal:
            start, end = (_sql_literal(item) for item in window)
            conditions.append(f"{_quote(event_field)} BETWEEN {start} AND {end}")
        else:
            conditions.append(f"{_quote(event_field)} BETWEEN :window_start AND :window_end")
            params["window_start"], params["window_end"] = window

    where = " AND ".join(conditions)
    sql = (
        f"SELECT {columns} FROM (\n"
        f"    SELECT {columns},\n"
        f"           ROW_NUMBER() OVER (\n"
        f"               PARTITION BY {partition}\n"
        f"               ORDER BY version DESC, knowledge_time DESC\n"
        f"           ) AS _rank\n"
        f"    FROM {spec.storage.canonical_table}\n"
        f"    WHERE {where}\n"
        f") ranked\n"
        "WHERE _rank = 1\n"
        f"ORDER BY {partition}"
    )
    return sql, params


def _adjust_plan(
    specs: Mapping[str, DatasetSpec], spec: DatasetSpec, requested: str | None
) -> tuple[str, DatasetSpec | None]:
    """解析有效口径 → ``(mode, factor_spec)``；不支持的口径直接报错。"""
    declared = spec.adjust
    mode: str
    if requested == "none":  # 文档口径别名（docs/sdk.md：none = 不调整）
        requested = "raw"
    if requested is None:
        mode = "raw" if declared is None else declared.default
        if mode == "none":
            mode = "raw"
    else:
        mode = requested
    if mode not in ADJUST_MODES:
        raise UnsupportedAdjust(
            f"口径非法：{mode!r}",
            hint=f"可选: {sorted(ADJUST_MODES)}",
        )
    if mode == "raw":
        return "raw", None
    if declared is None or mode not in declared.modes:
        allowed = declared.modes if declared else []
        raise UnsupportedAdjust(
            f"{spec.dataset} 未声明口径 {mode}",
            hint=f"该数据集支持: {allowed or ['raw']}",
        )
    factor_spec = specs.get(declared.factor_dataset)
    if factor_spec is None:
        raise UnknownDataset(
            f"因子数据集不存在：{declared.factor_dataset}",
            hint="检查字典 adjust.factor_dataset",
        )
    return mode, factor_spec


def _adjusted_sql(
    spec: DatasetSpec,
    fields: Sequence[str],
    *,
    as_of: datetime,
    mode: str,
    factor_spec: DatasetSpec,
    entity_ids: Sequence[int] | None,
    window: tuple[date, date] | None,
    literal: bool,
) -> tuple[str, dict[str, Any]]:
    """复权组合 SQL（``qfq``/``hfq``；与 :func:`read` 同一实现）。"""
    declared = spec.adjust
    assert declared is not None  # _adjust_plan 已保证
    business = set(spec.business_key)
    adjustable = [name for name in fields if name in set(declared.fields)]
    passthrough = [
        name for name in fields if name not in set(declared.fields) and name not in business
    ]
    if not adjustable:
        return dataset_asof_sql(
            spec,
            fields,
            as_of=as_of,
            entity_ids=entity_ids,
            window=window,
            literal=literal,
        )

    raw_sql, params = dataset_asof_sql(
        spec,
        fields,
        as_of=as_of,
        entity_ids=entity_ids,
        window=window,
        literal=literal,
    )
    factor_bk = list(factor_spec.business_key)
    factor_sql, factor_params = dataset_asof_sql(
        factor_spec,
        [declared.factor_field],
        as_of=as_of,
        entity_ids=entity_ids if "entity_id" in factor_bk else None,
        literal=literal,
    )
    params.update(factor_params)

    join = " AND ".join(f"r.{_quote(name)} = f.{_quote(name)}" for name in factor_bk)
    event_field = _event_time_field(factor_spec) or factor_bk[-1]
    entity_keys = [name for name in factor_bk if name != event_field]
    if not entity_keys:
        raise AccessError(
            f"{spec.dataset}: 因子数据集 {factor_spec.dataset} 业务键缺少非事件时间键，"
            "无法计算 as-of 锚点",
            hint="为因子数据集补充实体级业务键（字典 adjust.factor_dataset）",
        )
    entity_join = " AND ".join(f"r.{_quote(name)} = a.{_quote(name)}" for name in entity_keys)
    entity_cols = ", ".join(_quote(name) for name in entity_keys)
    factor_col = _quote(declared.factor_field)

    if mode == "qfq":
        expressions = [
            f"r.{_quote(name)} * f.{factor_col} / a.{factor_col} AS {_quote(name)}"
            for name in adjustable
        ]
    else:  # hfq
        expressions = [
            f"r.{_quote(name)} * f.{factor_col} AS {_quote(name)}" for name in adjustable
        ]
    select_cols = [f"r.{_quote(name)}" for name in spec.business_key]
    select_cols.extend(expressions)
    select_cols.extend(f"r.{_quote(name)}" for name in passthrough)

    sql = (
        "SELECT " + ", ".join(select_cols) + "\n"
        f"FROM (\n{raw_sql}\n) AS r\n"
        f"LEFT JOIN (\n{factor_sql}\n) AS f ON {join}\n"
        "LEFT JOIN (\n"
        f"    SELECT {entity_cols}, {factor_col} FROM (\n"
        f"        SELECT {entity_cols}, {factor_col},\n"
        "               ROW_NUMBER() OVER (\n"
        f"                   PARTITION BY {entity_cols} ORDER BY {_quote(event_field)} DESC\n"
        "               ) AS _anchor_rank\n"
        f"        FROM (\n{factor_sql}\n) AS factor_pit\n"
        "    ) AS factor_ranked\n"
        "    WHERE _anchor_rank = 1\n"
        f") AS a ON {entity_join}\n"
        f"ORDER BY {', '.join(f'r.{_quote(name)}' for name in spec.business_key)}"
    )
    return sql, params


def read_sql(
    spec: DatasetSpec,
    fields: Sequence[str],
    *,
    as_of: datetime,
    adjust: str | None = None,
    specs: Mapping[str, DatasetSpec] | None = None,
    entity_ids: Sequence[int] | None = None,
    window: tuple[date, date] | None = None,
    literal: bool = False,
) -> tuple[str, dict[str, Any], str]:
    """渲染规范化读取 SQL（返回 ``(sql, params, effective_adjust)``）。

    ``literal=True`` 供读模型内联（字面量参数）；``read`` 与内联共用本函数，
    保证两条路径语义一致（单一实现）。
    """
    dictionary = specs if specs is not None else load_all()
    mode, factor_spec = _adjust_plan(dictionary, spec, adjust)
    if mode == "raw" or factor_spec is None:
        sql, params = dataset_asof_sql(
            spec,
            fields,
            as_of=as_of,
            entity_ids=entity_ids,
            window=window,
            literal=literal,
        )
        return sql, params, "raw"
    sql, params = _adjusted_sql(
        spec,
        fields,
        as_of=as_of,
        mode=mode,
        factor_spec=factor_spec,
        entity_ids=entity_ids,
        window=window,
        literal=literal,
    )
    return sql, params, mode


def read(
    engine: Engine,
    dataset: str,
    fields: Sequence[str] | None = None,
    *,
    as_of: datetime,
    adjust: str | None = None,
    entities: Sequence[int] | None = None,
    window: tuple[date, date] | None = None,
    specs: Mapping[str, DatasetSpec] | None = None,
) -> ReadResult:
    """规范化读取：PIT（as_of）+ 口径组合（缺省取字典 default）。

    返回 Arrow 表（业务键 + 请求字段）与 :class:`ReadMeta`。
    """
    import pandas as pd
    import pyarrow as pa

    dictionary = specs if specs is not None else load_all()
    spec = dictionary.get(dataset)
    if spec is None:
        raise UnknownDataset(
            f"数据集不存在：{dataset}",
            hint=f"可选数据集 {len(dictionary)} 个；见数据字典",
        )
    available = {item.name for item in spec.fields}
    selected = list(dict.fromkeys(fields)) if fields else [item.name for item in spec.fields]
    unknown = [name for name in selected if name not in available]
    if unknown:
        raise UnknownField(
            f"字段不存在：{unknown}",
            hint=f"{dataset} 可用字段见字典",
        )
    sql, params, mode = read_sql(
        spec,
        selected,
        as_of=as_of,
        adjust=adjust,
        specs=dictionary,
        entity_ids=entities,
        window=window,
    )
    statement = text(sql)
    if entities is not None:
        statement = statement.bindparams(bindparam("entity_ids", expanding=True))
    with engine.connect() as connection:
        frame = pd.read_sql(statement, connection, params=params)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    declared = spec.adjust
    adjusted_fields = tuple(
        name
        for name in selected
        if mode != "raw" and declared is not None and name in set(declared.fields)
    )
    meta = ReadMeta(
        dataset=dataset,
        as_of=as_of,
        adjust=mode,
        semantic_version=spec.semantic_version,
        row_count=table.num_rows,
        factor_dataset=(
            declared.factor_dataset if adjusted_fields and declared is not None else None
        ),
        adjusted_fields=adjusted_fields,
    )
    return ReadResult(dataset=dataset, table=table, meta=meta)
