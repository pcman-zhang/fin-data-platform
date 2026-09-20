"""Ingestion 共享原语：同步结果 / provider 识别 / 知识时间。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import pandas as pd

#: provider 枚举（与数据字典一致）
PROVIDERS = frozenset({"tushare", "baostock", "wind", "akshare", "fuyao"})

#: A 股收盘 15:00（Asia/Shanghai）= 07:00 UTC
CLOSE_UTC = time(7, 0)


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
