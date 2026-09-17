"""as-of 输入读取（doc-10 §3.5 / doc-12 §3.3）。

- 输入引用 = ``dataset.field``（来自字典 ``derived.inputs``）；
- **as-of 纪律**：只读 ``knowledge_time <= as_of`` 的行（防前视）；
- **重述去重**：同业务键多版本取最高 ``version``（与读模型 ``is_latest`` 口径一致）；
- **窗口/实体过滤**：事件时间列（``pit_role=event_time``）按窗口裁剪；
  ``entity_id`` 在业务键内时按实体过滤；
- 输出：``{dataset.field: pa.Table}``，列 = 数据集业务键 + 所请求字段。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, bindparam, text

from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec

if TYPE_CHECKING:
    import pyarrow as pa

#: v1 支持的输入 pit_class（均带 knowledge_time + version；scd2 区间语义后续引入）
SUPPORTED_PIT_CLASSES = frozenset({"market", "versioned", "snapshot"})


def input_view_name(ref: str) -> str:
    """输入引用的计算视图名（算法 SQL 与内联模板共用）：``a.b.c`` → ``a__b__c``。"""
    return ref.replace(".", "__")


def normalize_as_of(value: datetime) -> datetime:
    """统一为 naive UTC（存储层口径；aware 输入先转 UTC）。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _quote(name: str) -> str:
    return f'"{name}"'


def _event_time_field(spec: DatasetSpec) -> str | None:
    for field in spec.fields:
        if field.pit_role == "event_time":
            return field.name
    return None


def _sql_literal(value: date | datetime) -> str:
    """把时间值渲染为 SQL 字面量（跨 SQLite / PostgreSQL / DuckDB 的纯字符串形式）。"""
    if isinstance(value, datetime):
        return f"'{value:%Y-%m-%d %H:%M:%S.%f}'"
    return f"'{value:%Y-%m-%d}'"


def dataset_asof_sql(
    spec: DatasetSpec,
    fields: Sequence[str],
    *,
    as_of: datetime,
    entity_ids: Sequence[int] | None = None,
    window: tuple[date, date] | None = None,
    literal: bool = False,
) -> tuple[str, dict[str, Any]]:
    """按字典构造 as-of 读取 SQL（业务键 + 指定字段，去重后取当前可见版本）。

    ``literal=True`` 时把参数渲染为 SQL 字面量（返回空参数；供读模型视图内联），
    否则返回命名参数（供 :func:`read_inputs` 绑定）。
    """
    if spec.pit_class not in SUPPORTED_PIT_CLASSES:
        raise NotImplementedError(
            f"{spec.dataset}: pit_class={spec.pit_class} 暂不支持作为派生输入"
            "（v1 支持 market / versioned / snapshot）"
        )
    business_key = list(spec.business_key)
    selected = [*business_key, *fields]
    columns = ", ".join(_quote(name) for name in selected)
    partition = ", ".join(_quote(name) for name in business_key)
    params: dict[str, Any] = {} if literal else {"as_of": normalize_as_of(as_of)}

    if literal:
        conditions = [f"knowledge_time <= {_sql_literal(normalize_as_of(as_of))}"]
    else:
        conditions = ["knowledge_time <= :as_of"]
    if entity_ids is not None:
        if "entity_id" not in business_key:
            raise ValueError(f"{spec.dataset}: 业务键不含 entity_id，无法按实体过滤")
        if literal:
            values = ", ".join(str(value) for value in entity_ids)
            conditions.append(f"entity_id IN ({values})")
        else:
            conditions.append("entity_id IN :entity_ids")
            params["entity_ids"] = tuple(entity_ids)
    event_field = _event_time_field(spec)
    if window is not None:
        if event_field is None:
            raise ValueError(f"{spec.dataset}: 无 event_time 字段，无法按窗口过滤")
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


def read_inputs(
    engine: Engine,
    refs: Sequence[str],
    *,
    as_of: datetime,
    specs: Mapping[str, DatasetSpec] | None = None,
    entity_ids: Sequence[int] | None = None,
    window: tuple[date, date] | None = None,
) -> dict[str, pa.Table]:
    """读取派生输入（按 ``dataset.field`` 分组，一次读表多处投影）。"""
    import pandas as pd
    import pyarrow as pa

    dictionary = specs if specs is not None else load_all()
    grouped: dict[str, list[str]] = {}
    for ref in refs:
        dataset, separator, field = ref.rpartition(".")
        if not separator or dataset not in dictionary:
            raise ValueError(f"派生输入不存在：{ref}")
        field_names = {item.name for item in dictionary[dataset].fields}
        if field not in field_names:
            raise ValueError(f"派生输入字段不存在：{ref}")
        grouped.setdefault(dataset, []).append(field)

    tables: dict[str, pa.Table] = {}
    with engine.connect() as connection:
        for dataset, fields in grouped.items():
            spec = dictionary[dataset]
            unique_fields = list(dict.fromkeys(fields))
            sql, params = dataset_asof_sql(
                spec,
                unique_fields,
                as_of=as_of,
                entity_ids=entity_ids,
                window=window,
            )
            statement = text(sql)
            if entity_ids is not None:
                # IN 展开绑定（SQLite/PostgreSQL 同构）
                statement = statement.bindparams(bindparam("entity_ids", expanding=True))
            frame = pd.read_sql(statement, connection, params=params)
            table = pa.Table.from_pandas(frame, preserve_index=False)
            for field in unique_fields:
                selected = [*spec.business_key, field]
                tables[f"{dataset}.{field}"] = table.select(selected)
    return tables
