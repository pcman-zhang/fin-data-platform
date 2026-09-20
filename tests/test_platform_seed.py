"""参考数据种子导入（TASK-3.32 步骤①）：幂等 / 行数 / 范围 / 交易所主键 / 显式校验。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.storage.__main__ import main
from fin_data_platform.storage.schema import build_metadata
from fin_data_platform.storage.seed import (
    _parse_calendar_rows,
    _parse_market_rows,
    _rows,
    seed_reference,
)

_KNOWN_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture()
def engine():  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        for schema in ("cn_equity", "cn_fund", "ref", "meta", "mart"):
            connection.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    metadata, _specs = build_metadata()
    metadata.create_all(engine)
    return engine, metadata


def test_seed_reference_is_idempotent_and_complete(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    first = seed_reference(db)
    assert first["ref.market"] == 2
    assert first["ref.trade_calendar"] > 9000
    # 重跑零写入（存在性检查，不依赖方言 upsert）
    assert seed_reference(db) == {"ref.market": 0, "ref.trade_calendar": 0}

    calendar = metadata.tables["ref.trade_calendar"]
    with db.connect() as connection:
        total = connection.execute(select(func.count()).select_from(calendar)).scalar_one()
        open_days = connection.execute(
            select(func.count()).select_from(calendar).where(calendar.c.is_open.is_(True))
        ).scalar_one()
        earliest, latest = connection.execute(
            select(func.min(calendar.c.trade_date), func.max(calendar.c.trade_date))
        ).one()
    assert total == first["ref.trade_calendar"]
    assert 6000 < open_days < total  # 交易日 < 日历日
    assert str(earliest) == "2015-01-01"
    assert str(latest).startswith("2027-")

    market = metadata.tables["ref.market"]
    with db.connect() as connection:
        ids = {row[0] for row in connection.execute(select(market.c.exchange_id))}
    assert ids == {"XSHG", "XSHE"}


def test_seed_reference_covers_both_exchanges(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    seed_reference(db)
    calendar = metadata.tables["ref.trade_calendar"]
    with db.connect() as connection:
        per_exchange = dict(
            connection.execute(
                select(calendar.c.exchange_id, func.count()).group_by(calendar.c.exchange_id)
            ).all()
        )
    assert set(per_exchange) == {"XSHG", "XSHE"}
    assert per_exchange["XSHG"] == per_exchange["XSHE"]  # 同一日历长度


def test_seed_rejects_is_open_outside_whitelist() -> None:
    rows = [{"exchange_id": "XSHG", "trade_date": "2026-01-05", "is_open": "yes"}]
    with pytest.raises(ValueError, match="is_open 非法"):
        _parse_calendar_rows(rows, set(), _KNOWN_TIME)


def test_seed_rejects_duplicate_business_keys() -> None:
    calendar_row = {"exchange_id": "XSHG", "trade_date": "2026-01-05", "is_open": "1"}
    with pytest.raises(ValueError, match="业务键重复"):
        _parse_calendar_rows([calendar_row, dict(calendar_row)], set(), _KNOWN_TIME)
    market_row = {
        "exchange_id": "XSHG",
        "name": "上海证券交易所",
        "market": "cn",
        "timezone": "Asia/Shanghai",
        "currency": "CNY",
        "valid_from": "2026-01-05",
    }
    with pytest.raises(ValueError, match="业务键重复"):
        _parse_market_rows([market_row, dict(market_row)], set(), _KNOWN_TIME)


def test_seed_rejects_empty_and_missing_cells() -> None:
    with pytest.raises(ValueError, match="为空"):
        _parse_calendar_rows([], set(), _KNOWN_TIME)
    with pytest.raises(ValueError, match="trade_date 为空"):
        _parse_calendar_rows(
            [{"exchange_id": "XSHG", "trade_date": "", "is_open": "1"}], set(), _KNOWN_TIME
        )
    with pytest.raises(ValueError, match="日期非法"):
        _parse_calendar_rows(
            [
                {
                    "exchange_id": "XSHG",
                    "trade_date": "2026-01-05",
                    "is_open": "1",
                    "pretrade_date": "not-a-date",
                }
            ],
            set(),
            _KNOWN_TIME,
        )


def test_seed_rejects_missing_columns() -> None:
    with pytest.raises(ValueError, match="缺少列"):
        _rows("market.csv", ("exchange_id", "not_a_column"))


def test_storage_cli_requires_action(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main([]) == 2
    assert "--migrate" in capsys.readouterr().out
