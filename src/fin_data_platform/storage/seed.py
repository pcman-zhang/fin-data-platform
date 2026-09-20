"""参考数据种子导入（TASK-3.32 步骤①）：包内 CSV → canonical 表（幂等）。

约束：所有组合在 Python 内完成（不做 SQL JOIN）；导入前先取已有业务键，
仅插入缺失行——幂等且不依赖数据库方言的 upsert 语法。
"""

from __future__ import annotations

import csv
import importlib.resources
from collections.abc import Iterator
from datetime import UTC, date, datetime

from sqlalchemy import Engine, select

from fin_data_platform.storage.schema import build_metadata

_DATA_PACKAGE = "fin_data_platform.data"


def _rows(name: str) -> Iterator[dict[str, str]]:
    resource = importlib.resources.files(_DATA_PACKAGE).joinpath(name)
    with resource.open("r", encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def seed_reference(engine: Engine) -> dict[str, int]:
    """导入 ``ref.market`` / ``ref.trade_calendar`` 种子；返回各表新增行数。"""
    metadata, _specs = build_metadata()
    known_time = datetime.now(UTC)

    market = metadata.tables["ref.market"]
    with engine.begin() as connection:
        existing_market = set(connection.execute(select(market.c.exchange_id, market.c.valid_from)))
        market_rows = [
            {
                "exchange_id": row["exchange_id"],
                "name": row["name"],
                "market": row["market"],
                "timezone": row["timezone"],
                "currency": row["currency"],
                "valid_from": date.fromisoformat(row["valid_from"]),
                "valid_to": None,
                "knowledge_time": known_time,
                "version": 1,
            }
            for row in _rows("market.csv")
            if (row["exchange_id"], date.fromisoformat(row["valid_from"])) not in existing_market
        ]
        if market_rows:
            connection.execute(market.insert(), market_rows)

    calendar = metadata.tables["ref.trade_calendar"]
    with engine.begin() as connection:
        existing_days = set(
            connection.execute(select(calendar.c.exchange_id, calendar.c.trade_date))
        )
        calendar_rows = [
            {
                "exchange_id": row["exchange_id"],
                "trade_date": date.fromisoformat(row["trade_date"]),
                "is_open": row["is_open"] == "1",
                "pretrade_date": (
                    date.fromisoformat(row["pretrade_date"]) if row["pretrade_date"] else None
                ),
                "knowledge_time": known_time,
                "version": 1,
            }
            for row in _rows("trade_calendar.csv")
            if (row["exchange_id"], date.fromisoformat(row["trade_date"])) not in existing_days
        ]
        if calendar_rows:
            connection.execute(calendar.insert(), calendar_rows)

    return {"ref.market": len(market_rows), "ref.trade_calendar": len(calendar_rows)}
