"""参考数据种子导入（TASK-3.32 步骤①）：包内 CSV → canonical 表（一次性首灌）。

语义：启动/迁移后调用，仅补齐缺失业务键（重跑空操作，幂等）；**非修订通道**——
后续数据修订走版本追加，不依赖本通道覆写。种子文件损坏（缺列 / 空文件 /
非法值 / 批次内业务键重复）时显式报错，不静默跳过。

约束：所有组合在 Python 内完成（不做 SQL JOIN）；导入前先取已有业务键，
仅插入缺失行——幂等且不依赖数据库方言的 upsert 语法。
"""

from __future__ import annotations

import csv
import importlib.resources
import io
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Engine, select

from fin_data_platform.storage.schema import build_metadata

_DATA_PACKAGE = "fin_data_platform.data"

_MARKET_COLUMNS = ("exchange_id", "name", "market", "timezone", "currency", "valid_from")
_CALENDAR_COLUMNS = ("exchange_id", "trade_date", "is_open", "pretrade_date")


def _rows(name: str, required: tuple[str, ...]) -> Iterator[dict[str, str]]:
    resource = importlib.resources.files(_DATA_PACKAGE).joinpath(name)
    reader = csv.DictReader(io.StringIO(resource.read_text(encoding="utf-8")))
    missing = [column for column in required if column not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"种子 {name} 缺少列: {', '.join(missing)}")
    return iter(reader)


def _cell(name: str, line: int, row: dict[str, str], column: str) -> str:
    value = (row.get(column) or "").strip()
    if not value:
        raise ValueError(f"{name} 第 {line} 行 {column} 为空")
    return value


def _day(name: str, line: int, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} 第 {line} 行日期非法: {value!r}") from exc


def _parse_market_rows(
    rows: Iterable[dict[str, str]],
    existing: set[tuple[str, date]],
    known_time: datetime,
) -> list[dict[str, Any]]:
    """校验并转换 market.csv（业务键：``exchange_id + valid_from``）。"""
    name = "market.csv"
    parsed: list[dict[str, Any]] = []
    seen: set[tuple[str, date]] = set()
    total = 0
    for line, row in enumerate(rows, start=2):  # 行号从 2 起（表头为 1）
        total += 1
        exchange_id = _cell(name, line, row, "exchange_id")
        valid_from = _day(name, line, _cell(name, line, row, "valid_from"))
        values = {
            "name": _cell(name, line, row, "name"),
            "market": _cell(name, line, row, "market"),
            "timezone": _cell(name, line, row, "timezone"),
            "currency": _cell(name, line, row, "currency"),
        }
        key = (exchange_id, valid_from)
        if key in seen:
            raise ValueError(f"{name} 第 {line} 行业务键重复: {key}")
        seen.add(key)
        if key in existing:  # 一次性首灌：已存在即跳过
            continue
        parsed.append(
            {
                "exchange_id": exchange_id,
                **values,
                "valid_from": valid_from,
                "valid_to": None,
                "knowledge_time": known_time,
                "version": 1,
            }
        )
    if total == 0:
        raise ValueError(f"种子 {name} 为空（无数据行）")
    return parsed


def _parse_calendar_rows(
    rows: Iterable[dict[str, str]],
    existing: set[tuple[str, date]],
    known_time: datetime,
) -> list[dict[str, Any]]:
    """校验并转换 trade_calendar.csv（业务键：``exchange_id + trade_date``）。"""
    name = "trade_calendar.csv"
    parsed: list[dict[str, Any]] = []
    seen: set[tuple[str, date]] = set()
    total = 0
    for line, row in enumerate(rows, start=2):
        total += 1
        exchange_id = _cell(name, line, row, "exchange_id")
        trade_date = _day(name, line, _cell(name, line, row, "trade_date"))
        raw_open = _cell(name, line, row, "is_open")
        if raw_open not in {"0", "1"}:
            raise ValueError(f"{name} 第 {line} 行 is_open 非法: {raw_open!r}（应为 0/1）")
        raw_pretrade = (row.get("pretrade_date") or "").strip()
        key = (exchange_id, trade_date)
        if key in seen:
            raise ValueError(f"{name} 第 {line} 行业务键重复: {key}")
        seen.add(key)
        if key in existing:
            continue
        parsed.append(
            {
                "exchange_id": exchange_id,
                "trade_date": trade_date,
                "is_open": raw_open == "1",
                "pretrade_date": (
                    _day(name, line, raw_pretrade) if raw_pretrade else None
                ),
                "knowledge_time": known_time,
                "version": 1,
            }
        )
    if total == 0:
        raise ValueError(f"种子 {name} 为空（无数据行）")
    return parsed


def seed_reference(engine: Engine) -> dict[str, int]:
    """导入 ``ref.market`` / ``ref.trade_calendar`` 种子；返回各表新增行数。"""
    metadata, _specs = build_metadata()
    known_time = datetime.now(UTC)

    market = metadata.tables["ref.market"]
    with engine.begin() as connection:
        existing_market: set[tuple[str, date]] = {
            (str(row[0]), row[1])
            for row in connection.execute(
                select(market.c.exchange_id, market.c.valid_from)
            )
        }
        market_rows = _parse_market_rows(
            _rows("market.csv", _MARKET_COLUMNS), existing_market, known_time
        )
        if market_rows:
            connection.execute(market.insert(), market_rows)

    calendar = metadata.tables["ref.trade_calendar"]
    with engine.begin() as connection:
        existing_days: set[tuple[str, date]] = {
            (str(row[0]), row[1])
            for row in connection.execute(
                select(calendar.c.exchange_id, calendar.c.trade_date)
            )
        }
        calendar_rows = _parse_calendar_rows(
            _rows("trade_calendar.csv", _CALENDAR_COLUMNS), existing_days, known_time
        )
        if calendar_rows:
            connection.execute(calendar.insert(), calendar_rows)

    return {"ref.market": len(market_rows), "ref.trade_calendar": len(calendar_rows)}
