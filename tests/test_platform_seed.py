"""参考数据种子导入（TASK-3.32 步骤①）：幂等 / 行数 / 范围 / 交易所主键。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.storage.schema import build_metadata
from fin_data_platform.storage.seed import seed_reference


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
