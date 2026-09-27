"""版本历史 / vintage 查询（TASK-3.13）：同一业务键与事件时间的版本序列。

- ``mode="history"``：返回全部可见版本（业务键 + 审计列 + 字段），按
  ``(键…, 事件时间, knowledge_time, version)`` 排序；
- ``mode="vintage"``：每个 ``(键…, 事件时间)`` 取**首个可见版本**（as-first-reported）；
- ``as_of`` 可选：给定时按 ``knowledge_time <= as_of`` 截断（PIT）；不给定时返回
  全部历史版本（含晚于当前知识的版本，用于审计回溯）。

本模块直读 canonical 单表（不走访问面的版本去重——版本历史本身即查询目标）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import Engine, bindparam, text

from fin_data_platform.access.errors import UnknownDataset, UnknownField
from fin_data_platform.access.reader import normalize_as_of
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.panel.errors import InvalidArgument

#: 版本审计列（按字典字段存在性过滤）
_AUDIT_FIELDS = ("knowledge_time", "version", "ingest_time", "publish_time")

MODES = frozenset({"history", "vintage"})


def _quote(name: str) -> str:
    return f'"{name}"'


def _event_time_field(spec: DatasetSpec) -> str:
    for field in spec.fields:
        if field.pit_role == "event_time":
            return field.name
    raise InvalidArgument(f"{spec.dataset}: 无事件时间字段，无法做版本查询")


def get_versions(
    engine: Engine,
    dataset: str,
    *,
    entities: Sequence[int] | None = None,
    start: date | None = None,
    end: date | None = None,
    fields: Sequence[str] | None = None,
    as_of: datetime | None = None,
    mode: str = "history",
    specs: Mapping[str, DatasetSpec] | None = None,
) -> pd.DataFrame:
    """版本历史（``history``）或 as-first-reported（``vintage``）查询。"""
    if mode not in MODES:
        raise InvalidArgument(f"mode 取值非法：{mode!r}", hint=f"可选 {sorted(MODES)}")
    dictionary = specs if specs is not None else load_all()
    spec = dictionary.get(dataset)
    if spec is None:
        raise UnknownDataset(f"数据集不存在：{dataset}", hint="见数据字典")
    event_field = _event_time_field(spec)
    available = {field.name for field in spec.fields}
    requested = list(dict.fromkeys(fields)) if fields else []
    unknown = [name for name in requested if name not in available]
    if unknown:
        raise UnknownField(f"字段不存在：{unknown}", hint=f"{dataset} 可用字段见字典")
    audit = [name for name in _AUDIT_FIELDS if name in available]
    selected = list(dict.fromkeys([*spec.business_key, *audit, *requested]))

    conditions: list[str] = []
    params: dict[str, Any] = {}
    expanding = False
    if as_of is not None:
        conditions.append("knowledge_time <= :as_of")
        params["as_of"] = normalize_as_of(as_of)
    if entities is not None:
        if "entity_id" not in spec.business_key:
            raise UnknownField(
                f"{dataset}: 业务键不含 entity_id，无法按实体过滤",
                hint="请改用窗口过滤或全量读取",
            )
        conditions.append("entity_id IN :entity_ids")
        params["entity_ids"] = tuple(entities)
        expanding = True
    if start is not None:
        conditions.append(f"{_quote(event_field)} >= :start")
        params["start"] = start
    if end is not None:
        conditions.append(f"{_quote(event_field)} <= :end")
        params["end"] = end
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    order = ", ".join(
        _quote(name) for name in [*spec.business_key, "knowledge_time", "version"]
    )
    sql = (
        f"SELECT {', '.join(_quote(name) for name in selected)} "
        f"FROM {spec.storage.canonical_table}{where} ORDER BY {order}"
    )
    statement = text(sql)
    if expanding:
        statement = statement.bindparams(bindparam("entity_ids", expanding=True))
    with engine.connect() as connection:
        frame = pd.read_sql(statement, connection, params=params)
    if mode == "vintage":
        # 显式排序后取首个版本（as-first-reported）：不依赖 SQL 排序的隐含契约
        frame = (
            frame.sort_values(
                [*spec.business_key, "knowledge_time", "version"], kind="stable"
            )
            .drop_duplicates(subset=[*spec.business_key], keep="first")
            .reset_index(drop=True)
        )
    frame.attrs.update(dataset=dataset, mode=mode)
    if as_of is not None:
        frame.attrs["as_of"] = as_of
    return frame
