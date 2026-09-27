"""PIT Universe 推导（doc-10 §3.3）：由交易状态数据集（``listing_lifecycle``）推导。

交易状态不在注册表：universe 的权威来源是数据集行（PIT 区间 + 知识时间），
注册表只提供身份属性（as-of SCD2）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol

from fin_data_platform.registry._util import EPOCH, to_date, to_datetime
from fin_data_platform.registry.models import (
    EntityRecord,
    LifecycleRecord,
    LifecycleStatus,
)


class EntityLookup(Protocol):
    """universe 需要的最小身份查询接口（``EntityRegistry`` / ``RegistryReader`` 均可）。"""

    def entity(self, entity_id: int, *, as_of: Any = None) -> EntityRecord | None: ...

    def entity_many(
        self, entity_ids: Iterable[int], *, as_of: Any = None
    ) -> dict[int, EntityRecord]: ...

#: 在市状态（停牌仍属 universe；退市不在）
_IN_UNIVERSE = frozenset({LifecycleStatus.LISTED, LifecycleStatus.SUSPENDED})

_MIN_DT = datetime.min


def _as_lifecycle(raw: LifecycleRecord | Mapping[str, Any]) -> LifecycleRecord:
    if isinstance(raw, LifecycleRecord):
        return replace(
            raw,
            start_date=to_date(raw.start_date),
            end_date=to_date(raw.end_date),
        )
    return LifecycleRecord(
        entity_id=int(raw["entity_id"]),
        status=str(raw["status"]),
        start_date=to_date(raw.get("start_date")),
        end_date=to_date(raw.get("end_date")),
        knowledge_time=raw.get("knowledge_time"),
        version=int(raw.get("version") or 1),
    )


def _status(record: LifecycleRecord) -> LifecycleStatus:
    try:
        return LifecycleStatus(record.status)
    except ValueError as exc:
        raise ValueError(f"listing_lifecycle status 非法: {record.status!r}") from exc


def universe(
    registry: EntityLookup,
    lifecycle: Iterable[LifecycleRecord | Mapping[str, Any]],
    as_of: Any,
    *,
    knowledge_as_of: Any = None,
    entity_type: str | None = None,
    market: str | None = None,
) -> list[EntityRecord]:
    """as-of PIT Universe：交易状态行覆盖 ``as_of`` 且状态在市，实体身份 as-of 有效。

    - 生命周期区间语义：``start_date <= as_of <= end_date``（闭区间，``None`` 表示至今）；
    - 知识时间：``knowledge_as_of`` 提供时仅纳入 ``knowledge_time <= knowledge_as_of``
      的行（防事后更正污染历史 as-of）；未提供则假定调用方已按 as-of 预过滤
      （例如经 :func:`fin_data_platform.storage.as_of_query` 读取数据集）；
    - 同一实体多行覆盖时取 ``(start_date, version, knowledge_time)`` 最大者（append-only 修订）；
    - 无生命周期记录的实体不进入 universe（数据不足，防前视）。
    """
    target = to_date(as_of)
    if target is None:
        raise ValueError("as_of 不能为空")
    knowledge_limit = to_datetime(knowledge_as_of)
    ranked: dict[int, tuple[tuple[Any, int, datetime], LifecycleRecord]] = {}
    for raw in lifecycle:
        row = _as_lifecycle(raw)
        if knowledge_limit is not None:
            known = to_datetime(row.knowledge_time)
            if known is not None and known > knowledge_limit:
                continue
        start = row.start_date or EPOCH
        if start > target:
            continue
        if row.end_date is not None and target > row.end_date:
            continue
        rank = (start, row.version, to_datetime(row.knowledge_time) or _MIN_DT)
        current = ranked.get(row.entity_id)
        if current is None or rank > current[0]:
            ranked[row.entity_id] = (rank, row)
    result: list[EntityRecord] = []
    records = registry.entity_many(sorted(ranked), as_of=target)
    for entity_id, (_, row) in sorted(ranked.items()):
        if _status(row) not in _IN_UNIVERSE:
            continue
        record = records.get(entity_id)
        if record is None:
            continue
        if entity_type and record.entity_type != entity_type:
            continue
        if market and record.market != market:
            continue
        result.append(record)
    return result
