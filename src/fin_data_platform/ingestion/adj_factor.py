"""复权因子同步：FinDataHub → Canonical（``cn_equity.adj_factor``；TASK-3.29）。

PIT 语义与日线一致：``knowledge_time`` 取交易日收盘时刻（稳定值），源值变化时
追加修订版本（不改写历史）；同值重跑不写新行。
"""

from __future__ import annotations

import math
from datetime import date
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, select

from fin_data_platform.ingestion.common import (
    SyncResult,
    knowledge_time,
    resolve_provider,
)
from fin_data_platform.registry._util import to_date
from fin_data_platform.registry.store import EntityStore
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.storage.schema import build_metadata
from fin_data_platform.storage.writers import append_rows

#: 目标数据集（字典键）
DATASET = "cn_equity.adj_factor"

#: 参与修订比对的数值字段
_VALUE_FIELDS = ("adj_factor",)


@lru_cache(maxsize=1)
def _factor_table() -> Any:
    """目标表（进程内缓存：build_metadata 解析字典开销较大）。"""
    metadata, _specs = build_metadata()
    return metadata.tables[DATASET]


def _same_values(prior: Any, record: dict[str, Any]) -> bool:
    for field in _VALUE_FIELDS:
        left, right = prior[field], record[field]
        if left is None or right is None:
            return False
        if not math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9):
            return False
    return True


def _latest_rows(
    connection: Any, table: Any, *, entity_id: int, start: date, end: date
) -> dict[date, Any]:
    """窗口内每个交易日的当前最新版本行。"""
    rows = (
        connection.execute(
            select(
                table.c.trade_date,
                table.c.knowledge_time,
                table.c.version,
                *(table.c[name] for name in _VALUE_FIELDS),
            ).where(
                table.c.entity_id == entity_id,
                table.c.trade_date >= start,
                table.c.trade_date <= end,
            )
        )
        .mappings()
        .all()
    )
    latest: dict[date, Any] = {}
    for row in rows:
        key = row["trade_date"]
        current = latest.get(key)
        if current is None or (row["knowledge_time"], row["version"]) > (
            current["knowledge_time"],
            current["version"],
        ):
            latest[key] = row
    return latest


def sync_adjust_factor(
    engine: Engine,
    hub: Any,
    *,
    code: str,
    start: date | str,
    end: date | str,
    source: Any = None,
    entity_type: str = "equity",
    name: str = "",
    market: str = "cn",
) -> SyncResult:
    """单标的复权因子同步：首版幂等写入；源值变化时追加修订版本。"""
    window_start = to_date(start)
    window_end = to_date(end)
    if window_start is None or window_end is None:
        raise ValueError(f"窗口非法: start={start!r}, end={end!r}")

    entity = EntityStore(engine).ensure_entity(
        code=code, entity_type=entity_type, name=name, market=market
    )
    frame = hub.get_adjust_factors(
        [code],
        start=window_start.isoformat(),
        end=window_end.isoformat(),
        source=source,
    )
    provider = resolve_provider(frame, source)
    table = _factor_table()

    now = utcnow()
    rows: list[dict[str, Any]] = []
    with engine.begin() as connection:
        latest = _latest_rows(
            connection,
            table,
            entity_id=entity.entity_id,
            start=window_start,
            end=window_end,
        )
        for item in frame.to_dict("records"):
            trade_date = to_date(item.get("date"))
            value = item.get("adj_factor")
            if trade_date is None or value is None:
                continue
            record: dict[str, Any] = {
                "entity_id": entity.entity_id,
                "trade_date": trade_date,
                "adj_factor": float(value),
                "ingest_time": now,
                "provider": provider,
            }
            prior = latest.get(trade_date)
            if prior is None:
                record["knowledge_time"] = knowledge_time(trade_date)
                record["version"] = 1
            elif _same_values(prior, record):
                continue  # 与最新版本一致：无修订
            else:
                record["knowledge_time"] = now  # 重述：修订入库时刻可见
                record["version"] = int(prior["version"]) + 1
            rows.append(record)
        written = append_rows(connection, table, rows)
    return SyncResult(
        dataset=DATASET,
        code=code,
        entity_id=entity.entity_id,
        window_start=window_start,
        window_end=window_end,
        fetched=len(frame.index),
        rows_written=written,
        provider=provider,
    )
