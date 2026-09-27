"""Ingestion 共享原语：同步结果 / provider 识别 / 知识时间 / 空窗口守卫。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from typing import Any

import pandas as pd
from sqlalchemy import Engine, select

from fin_data_platform.registry._util import to_date
from fin_data_platform.storage.schema import build_metadata

#: provider 枚举（与数据字典一致）
PROVIDERS = frozenset({"tushare", "baostock", "wind", "akshare", "fuyao"})

#: A 股收盘 15:00（Asia/Shanghai）= 07:00 UTC
CLOSE_UTC = time(7, 0)

#: 状态推导与空窗口守卫使用的交易所日历（iso MIC；沪深日历一致，单边即可）
DEFAULT_EXCHANGE = "XSHG"


class EmptySourceWindow(RuntimeError):
    """窗口含交易日但源端返回 0 行（软失败：重试且不推进水位）。"""


@dataclass(frozen=True, slots=True)
class SyncResult:
    dataset: str
    code: str
    entity_id: int
    window_start: date
    window_end: date
    fetched: int
    rows_written: int
    provider: str


def knowledge_time(trade_date: date) -> datetime:
    """交易日知识时间（naive UTC：15:00 CST 收盘时刻；稳定值保证重跑幂等）。"""
    return datetime.combine(trade_date, CLOSE_UTC)


def resolve_provider(frame: pd.DataFrame, source: Any) -> str:
    raw = frame.attrs.get("source")
    provider = str(raw if raw is not None else (source or "")).lower()
    if provider not in PROVIDERS:
        raise ValueError(f"无法识别 provider: {provider!r}（期望 {sorted(PROVIDERS)}）")
    return provider


@lru_cache(maxsize=1)
def _calendar_table():  # type: ignore[no-untyped-def]
    """落库日历表（进程内缓存：build_metadata 开销较大）。"""
    metadata, _specs = build_metadata()
    return metadata.tables["ref.trade_calendar"]


@lru_cache(maxsize=8)
def _dataset_table(dataset: str):  # type: ignore[no-untyped-def]
    """数据集表 + 事件时间列名（进程内缓存；在市判定用）。"""
    metadata, specs = build_metadata()
    spec = specs[dataset]
    event_field = next(
        (field.name for field in spec.fields if field.pit_role == "event_time"), None
    )
    if event_field is None:
        raise ValueError(f"{dataset}: 未声明 event_time 字段，无法做在市判定")
    return metadata.tables[dataset], event_field


def trading_days(connection: Any, *, exchange: str, start: date, end: date) -> list[date]:
    """落库日历窗口内交易日（调用方持有连接；非交易日不返回）。"""
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
    days: set[date] = set()
    for row in rows:
        day = to_date(row)
        if day is not None:
            days.add(day)
    return sorted(days)


def calendar_open_days(
    engine: Engine, *, start: date, end: date, exchange: str = DEFAULT_EXCHANGE
) -> list[date] | None:
    """窗口内交易日；窗口无任何日历行（未覆盖 / 未导入）返回 ``None``（无法判定）。"""
    table = _calendar_table()
    with engine.connect() as connection:
        covered = connection.execute(
            select(table.c.trade_date)
            .where(
                table.c.exchange_id == exchange,
                table.c.trade_date >= start,
                table.c.trade_date <= end,
            )
            .limit(1)
        ).first()
        if covered is None:
            return None
        return trading_days(connection, exchange=exchange, start=start, end=end)


def entity_has_history(
    engine: Engine, dataset: str, *, entity_id: int, through: date
) -> bool:
    """在市判定：该实体在 ``through``（含）之前是否已有数据（前上市 / 首次同步 → ``False``）。"""
    table, event_field = _dataset_table(dataset)
    with engine.connect() as connection:
        row = connection.execute(
            select(table.c.entity_id)
            .where(table.c.entity_id == entity_id, table.c[event_field] <= through)
            .limit(1)
        ).first()
    return row is not None
