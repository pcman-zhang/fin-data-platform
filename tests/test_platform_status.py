"""每日状态同步测试（TASK-3.32 步骤②）：停牌/ST 推导、三态日历、幂等与修订。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.ingestion import sync_daily_status
from fin_data_platform.ingestion.daily_status import (
    _is_st_name,
    _suspended_dates,
)
from fin_data_platform.storage.schema import build_metadata

CODE = "600519.SH"
D1 = date(2026, 9, 10)  # 交易日
D2 = date(2026, 9, 11)  # 交易日（停牌）
D3 = date(2026, 9, 14)  # 交易日（ST）
D4 = date(2026, 9, 12)  # 非交易日（周六）
WINDOW_START = "2026-09-10"
WINDOW_END = "2026-09-14"


class FakeHub:
    def __init__(
        self,
        *,
        suspension: list[dict] | None = None,
        namechange: list[dict] | None = None,
    ) -> None:
        self._suspension = suspension or []
        self._namechange = namechange or []
        self.calls: list[tuple] = []

    def get_market_events(
        self, *, kind: str, start: str, end: str, codes=None, source=None, **_: object
    ) -> pd.DataFrame:
        self.calls.append((kind, tuple(codes or ()), start, end))
        rows = self._suspension if kind == "suspension" else self._namechange
        frame = pd.DataFrame(rows)
        frame.attrs["source"] = "tushare"
        return frame


@pytest.fixture()
def engine():  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        for schema in ("cn_equity", "cn_fund", "ref", "meta"):
            connection.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    metadata, _ = build_metadata()
    metadata.create_all(engine)
    calendar = metadata.tables["ref.trade_calendar"]
    with engine.begin() as connection:
        connection.execute(
            calendar.insert(),
            [
                {
                    "exchange_id": "XSHG",
                    "trade_date": day,
                    "is_open": is_open,
                    "pretrade_date": None,
                    "knowledge_time": pd.Timestamp("2026-01-01"),
                    "version": 1,
                }
                for day, is_open in ((D1, True), (D2, True), (D4, False), (D3, True))
            ],
        )
    return engine, metadata


def _status_rows(engine, table):  # type: ignore[no-untyped-def]
    with engine.connect() as connection:
        return (
            connection.execute(
                select(
                    table.c.trade_date,
                    table.c.is_suspended,
                    table.c.is_st,
                    table.c.version,
                ).order_by(table.c.trade_date)
            )
            .mappings()
            .all()
        )


def test_sync_daily_status_marks_suspension_and_st(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    hub = FakeHub(
        suspension=[
            {"code": CODE, "date": pd.Timestamp(D2), "suspend_type": "S"},
            {"code": CODE, "date": pd.Timestamp(D3), "suspend_type": "R"},
        ],
        namechange=[
            {"code": CODE, "name": "贵州茅台", "start_date": pd.Timestamp("2020-01-01")},
            {
                "code": CODE,
                "name": "ST茅台",
                "start_date": pd.Timestamp(D3),
                "end_date": None,
            },
        ],
    )
    result = sync_daily_status(
        db, hub, code=CODE, start=WINDOW_START, end=WINDOW_END
    )
    assert result.fetched == 3  # 非交易日 D4 不产出预期行
    assert result.rows_written == 3

    rows = _status_rows(db, metadata.tables["cn_equity.daily_status"])
    assert [row["trade_date"] for row in rows] == [D1, D2, D3]
    assert [(row["is_suspended"], row["is_st"]) for row in rows] == [
        (False, False),
        (True, False),  # S 停牌；R 复牌行不计
        (False, True),  # S 行前已复牌；ST 区间起始
    ]
    assert all(row["version"] == 1 for row in rows)
    # namechange 回看起点早于窗口（区间可能早于窗口开始）
    namechange_call = [call for call in hub.calls if call[0] == "namechange"][0]
    assert namechange_call[2] < WINDOW_START


def test_sync_daily_status_is_idempotent_and_revises(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    hub = FakeHub(suspension=[{"code": CODE, "date": pd.Timestamp(D2), "suspend_type": "S"}])
    sync_daily_status(db, hub, code=CODE, start=WINDOW_START, end=WINDOW_END)
    again = sync_daily_status(db, hub, code=CODE, start=WINDOW_START, end=WINDOW_END)
    assert again.rows_written == 0  # 状态未变：无修订

    # 状态变化（D3 新增停牌）→ 仅该日追加新版本
    hub._suspension = [
        {"code": CODE, "date": pd.Timestamp(D2), "suspend_type": "S"},
        {"code": CODE, "date": pd.Timestamp(D3), "suspend_type": "S"},
    ]
    revised = sync_daily_status(db, hub, code=CODE, start=WINDOW_START, end=WINDOW_END)
    assert revised.rows_written == 1

    table = metadata.tables["cn_equity.daily_status"]
    with db.connect() as connection:
        latest: dict[date, tuple[bool, int]] = {}
        for row in (
            connection.execute(
                select(
                    table.c.trade_date,
                    table.c.is_suspended,
                    table.c.version,
                    table.c.knowledge_time,
                ).order_by(table.c.trade_date, table.c.knowledge_time, table.c.version)
            )
            .mappings()
            .all()
        ):
            latest[row["trade_date"]] = (row["is_suspended"], row["version"])
        total = connection.execute(
            select(func.count()).select_from(table)
        ).scalar_one()
    assert total == 4  # 3 首版 + 1 修订
    assert latest[D1] == (False, 1)
    assert latest[D2] == (True, 1)
    assert latest[D3] == (True, 2)


def test_sync_daily_status_without_calendar_rows(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    result = sync_daily_status(
        db, FakeHub(), code=CODE, start="2026-10-01", end="2026-10-09"
    )
    assert (result.fetched, result.rows_written) == (0, 0)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ST股份", True),
        ("*ST股份", True),
        ("SST前锋", True),
        ("S*ST前锋", True),
        ("贵州茅台", False),
        ("TCL科技", False),
    ],
)
def test_is_st_name(name: str, expected: bool) -> None:
    assert _is_st_name(name) is expected


def test_suspended_dates_skips_resume_rows() -> None:
    frame = pd.DataFrame(
        [
            {"code": CODE, "date": pd.Timestamp(D1), "suspend_type": "S"},
            {"code": CODE, "date": pd.Timestamp(D2), "suspend_type": "R"},
            {"code": CODE, "date": pd.Timestamp(D3), "suspend_type": "s"},
        ]
    )
    assert _suspended_dates(frame) == {D1, D3}
