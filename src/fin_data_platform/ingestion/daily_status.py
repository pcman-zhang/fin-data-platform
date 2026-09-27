"""每日状态同步（TASK-3.32 步骤②）：停牌 / ST → ``cn_equity.daily_status``。

- 交易日取自落库日历 ``ref.trade_calendar``（预填充，不依赖运行时源调用）；
- 停牌来自 hub 市场事件 ``suspension``（tushare ``suspend_d``；复牌行不计停牌）；
- ST 由 ``namechange`` 区间推导（名称含 ST，含 ``*ST``/``SST`` 历史前缀）——
  区间可能早于同步窗口，故回看全历史后取窗口内交易日；
- PIT 语义与日线一致：首版 ``knowledge_time`` 取交易日收盘时刻（稳定值，重跑幂等）；
  状态变化时追加新版本（修订入库时刻可见），未变化不写新行。
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Any

import pandas as pd
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
DATASET = "cn_equity.daily_status"

#: 参与修订比对的状态字段
_VALUE_FIELDS = ("is_suspended", "is_st")

#: ST 区间回看起点：区间起点可能早于请求窗口（如多年前 ST 至今）
_NAMECHANGE_LOOKBACK = "1990-01-01"

#: 状态推导使用的交易所日历（iso MIC；沪深日历一致，单边即可）
DEFAULT_EXCHANGE = "XSHG"


@lru_cache(maxsize=1)
def _daily_status_table():
    metadata, _specs = build_metadata()
    return metadata.tables[DATASET]


@lru_cache(maxsize=1)
def _calendar_table():
    metadata, _specs = build_metadata()
    return metadata.tables["ref.trade_calendar"]


def _is_st_name(name: str) -> bool:
    """名称是否 ST 形态（覆盖 ``ST`` / ``*ST`` / 历史 ``SST`` / ``S*ST``）。"""
    normalized = name.strip().upper()
    return any(normalized.startswith(prefix) for prefix in ("*ST", "ST", "S*ST", "SST"))


def _suspended_dates(frame: pd.DataFrame) -> set[date]:
    """停牌日集合（``suspend_type=R`` 为复牌行，不计停牌）。"""
    suspended: set[date] = set()
    for row in frame.to_dict("records"):
        event_date = to_date(row.get("date"))
        if event_date is None:
            continue
        if str(row.get("suspend_type") or "").strip().upper() == "R":
            continue
        suspended.add(event_date)
    return suspended


def _st_intervals(frame: pd.DataFrame) -> list[tuple[date, date | None]]:
    """ST 生效区间列表（``end_date`` 为空表示至今）。"""
    intervals: list[tuple[date, date | None]] = []
    for row in frame.to_dict("records"):
        if not _is_st_name(str(row.get("name") or "")):
            continue
        start = to_date(row.get("start_date"))
        if start is None:
            continue
        intervals.append((start, to_date(row.get("end_date"))))
    return intervals


def _in_intervals(day: date, intervals: list[tuple[date, date | None]]) -> bool:
    return any(start <= day and (end is None or day <= end) for start, end in intervals)


def _trading_days(
    connection: Any, *, exchange: str, start: date, end: date
) -> list[date]:
    table = _calendar_table()
    rows = (
        connection.execute(
            select(table.c.trade_date).where(
                table.c.exchange_id == exchange,
                table.c.trade_date >= start,
                table.c.trade_date <= end,
                table.c.is_open.is_(True),
            )
        )
        .scalars()
        .all()
    )
    return sorted(row for row in rows if isinstance(row, date))


def _same_values(prior: Any, record: dict[str, Any]) -> bool:
    return all(bool(prior[field]) == bool(record[field]) for field in _VALUE_FIELDS)


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
        if current is None or (
            row["knowledge_time"],
            row["version"],
        ) > (
            current["knowledge_time"],
            current["version"],
        ):
            latest[key] = row
    return latest


def sync_daily_status(
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
    exchange: str = DEFAULT_EXCHANGE,
) -> SyncResult:
    """单标的每日状态同步：按落库日历逐交易日生成状态行（幂等 + 修订）。"""
    window_start = to_date(start)
    window_end = to_date(end)
    if window_start is None or window_end is None:
        raise ValueError(f"窗口非法: start={start!r}, end={end!r}")

    entity = EntityStore(engine).ensure_entity(
        code=code, entity_type=entity_type, name=name, market=market
    )
    suspension = hub.get_market_events(
        kind="suspension",
        codes=[code],
        start=window_start.isoformat(),
        end=window_end.isoformat(),
        source=source,
    )
    namechange = hub.get_market_events(
        kind="namechange",
        codes=[code],
        start=_NAMECHANGE_LOOKBACK,
        end=window_end.isoformat(),
        source=source,
    )
    provider = resolve_provider(suspension, source)
    suspended = _suspended_dates(suspension)
    intervals = _st_intervals(namechange)

    table = _daily_status_table()
    now = utcnow()
    rows: list[dict[str, Any]] = []
    with engine.begin() as connection:
        days = _trading_days(
            connection, exchange=exchange, start=window_start, end=window_end
        )
        latest = _latest_rows(
            connection,
            table,
            entity_id=entity.entity_id,
            start=window_start,
            end=window_end,
        )
        for day in days:
            record: dict[str, Any] = {
                "entity_id": entity.entity_id,
                "trade_date": day,
                "is_suspended": day in suspended,
                "is_st": _in_intervals(day, intervals),
                "ingest_time": now,
                "provider": provider,
            }
            prior = latest.get(day)
            if prior is None:
                record["knowledge_time"] = knowledge_time(day)
                record["version"] = 1
            elif _same_values(prior, record):
                continue  # 与最新版本一致：无修订
            else:
                record["knowledge_time"] = now
                record["version"] = int(prior["version"]) + 1
            rows.append(record)
        written = append_rows(connection, table, rows)
    return SyncResult(
        dataset=DATASET,
        code=code,
        entity_id=entity.entity_id,
        window_start=window_start,
        window_end=window_end,
        fetched=len(days),
        rows_written=written,
        provider=provider,
    )
