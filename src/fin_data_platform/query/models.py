"""PIT 行查询模型（doc-12 §2.2 / §3 / §4；REST 与未来 SDK 共用）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

#: 请求 version_mode 取值（显式必填，无默认）
VERSION_MODES = ("latest", "as_of", "history")
#: as_of 语义
AS_OF_POLICIES = ("knowledge", "publish")
#: publish 缺 publish_time 时的回退策略
FALLBACK_MODES = ("strict", "allow")


@dataclass(frozen=True, slots=True)
class FilterClause:
    """结构化过滤条件（op 白名单见 query.reader）。"""

    field: str
    op: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class RowsQuery:
    """dataset-generic PIT 行查询（doc-12 §2.2 参数集合）。"""

    dataset: str
    version_mode: str = ""
    as_of: datetime | None = None
    as_of_policy: str = "knowledge"
    fallback_mode: str = "strict"
    entities: tuple[int, ...] = ()
    window: tuple[date, date] | None = None
    fields: tuple[str, ...] = ()
    filters: tuple[FilterClause, ...] = ()
    order_by: tuple[str, ...] = ()
    limit: int = 1000
    cursor: str | None = None
    include_meta: bool = False


@dataclass(slots=True)
class RowsMeta:
    """响应元数据（doc-12 §3.4；对应响应体 ``meta`` 与响应头）。"""

    dataset: str
    version_mode: str
    as_of: datetime | None
    policy: str
    fallback: str | None
    semantic_version: int
    data_generation: str | None
    row_count: int
    generated_at: datetime
    warnings: list[str] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass(slots=True)
class RowsResult:
    frame: pd.DataFrame
    meta: RowsMeta
