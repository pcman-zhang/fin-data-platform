"""访问面对齐读取测试（TASK-3.31）：交易日历对齐 / 状态列 / 缺失=NaN。

覆盖：三态与非交易日无行、日历与状态的 PIT（as_of 与版本）、复权组合、
结构化异常（scope / 不支持 / 日历不可见）、状态数据集缺省、零写入与缺省不变。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.access import (
    STATUS_MISSING,
    STATUS_OK,
    STATUS_SUSPENDED,
    AlignmentCalendarUnavailable,
    InvalidAlignmentScope,
    UnsupportedAlignment,
    read,
)
from fin_data_platform.dictionary import load_all
from fin_data_platform.storage.schema import build_metadata

DATASET = "cn_equity.daily_bar"
FACTOR = "cn_equity.adj_factor"
STATUS = "cn_equity.daily_status"
CALENDAR = "ref.trade_calendar"

D1 = date(2026, 9, 10)  # 交易日
D2 = date(2026, 9, 11)  # 交易日（停牌）
D3 = date(2026, 9, 14)  # 交易日（ST）
D4 = date(2026, 9, 15)  # 交易日（真缺失）
D5 = date(2026, 9, 12)  # 非交易日（周六）
D6 = date(2026, 9, 16)  # 交易日（盘中停牌：有 bar）

WINDOW = (D1, D6)
AS_OF = datetime(2026, 9, 17, 12, 0)
CALENDAR_KNOWN = datetime(2026, 9, 1, 12, 0)
KNOWN = datetime(2026, 9, 17, 8, 0)


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
    metadata, _ = build_metadata()
    metadata.create_all(engine)
    return engine, metadata


def _seed(engine, metadata, dataset: str, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as connection:
        connection.execute(metadata.tables[dataset].insert(), rows)


def _calendar_rows(known: datetime = CALENDAR_KNOWN, **days: datetime) -> list[dict[str, Any]]:
    flags = {D1: True, D2: True, D3: True, D4: True, D5: False, D6: True}
    flags.update(days)
    return [
        {
            "exchange_id": "XSHG",
            "trade_date": day,
            "is_open": is_open,
            "pretrade_date": None,
            "knowledge_time": known,
            "version": 1,
        }
        for day, is_open in flags.items()
    ]


def _bar(entity: int, day: date, close: float, known: datetime = KNOWN, version: int = 1) -> dict:
    return {
        "entity_id": entity,
        "trade_date": day,
        "close": close,
        "volume": 100.0,
        "knowledge_time": known,
        "ingest_time": known,
        "provider": "tushare",
        "version": version,
    }


def _factor(entity: int, day: date, value: float, known: datetime = KNOWN) -> dict:
    return {
        "entity_id": entity,
        "trade_date": day,
        "adj_factor": value,
        "knowledge_time": known,
        "ingest_time": known,
        "provider": "tushare",
        "version": 1,
    }


def _status(
    entity: int,
    day: date,
    *,
    suspended: bool = False,
    st: bool = False,
    known: datetime = KNOWN,
    version: int = 1,
) -> dict:
    return {
        "entity_id": entity,
        "trade_date": day,
        "is_suspended": suspended,
        "is_st": st,
        "knowledge_time": known,
        "ingest_time": known,
        "provider": "tushare",
        "version": version,
    }


def _read(engine, *, fields: list[str] | None = None, **kwargs: Any):  # type: ignore[no-untyped-def]
    options: dict[str, Any] = {
        "entities": [1],
        "window": WINDOW,
        "as_of": AS_OF,
        "adjust": "raw",
        "align_calendar": True,
    }
    options.update(kwargs)
    return read(engine, DATASET, fields or ["close"], **options)


def _rows(result) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return result.table.to_pylist()


def _seed_three_states(engine, metadata) -> None:  # type: ignore[no-untyped-def]
    _seed(engine, metadata, CALENDAR, _calendar_rows())
    _seed(
        engine,
        metadata,
        DATASET,
        [_bar(1, D1, 10.0), _bar(1, D3, 11.0), _bar(1, D6, 12.0)],
    )
    _seed(
        engine,
        metadata,
        STATUS,
        [
            _status(1, D2, suspended=True),
            _status(1, D3, st=True),
            _status(1, D6, suspended=True),  # 盘中停牌：当日有 bar
        ],
    )


def test_align_marks_three_states(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_three_states(db, metadata)
    result = _read(db)
    rows = _rows(result)
    assert list(result.table.column_names) == [
        "entity_id",
        "trade_date",
        "close",
        "status",
        "is_suspended",
        "is_st",
    ]
    assert [row["trade_date"] for row in rows] == [D1, D2, D3, D4, D6]  # 非交易日 D5 无行
    assert [row["status"] for row in rows] == [
        STATUS_OK,
        STATUS_SUSPENDED,
        STATUS_OK,
        STATUS_MISSING,
        STATUS_OK,
    ]
    # 停牌 / 缺失行保留但数值缺失（Arrow null；转 pandas 即 NaN），不填充
    assert pd.isna(rows[1]["close"]) and pd.isna(rows[3]["close"])
    assert rows[2]["is_st"] is True and rows[2]["close"] == 11.0
    assert rows[4]["is_suspended"] is True and rows[4]["close"] == 12.0  # 盘中停牌以 bar 为准
    assert rows[1]["is_suspended"] is True
    assert result.meta.aligned is True
    assert result.meta.calendar_dataset == CALENDAR
    assert result.meta.status_dataset == STATUS
    assert result.meta.trading_days == 5
    assert result.meta.row_count == 5


def test_align_multi_entity_grid_and_sort(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed(db, metadata, CALENDAR, _calendar_rows())
    _seed(db, metadata, DATASET, [_bar(2, D1, 9.0), _bar(1, D3, 11.0)])
    result = _read(db, entities=[2, 1])  # 输入顺序打乱：输出按业务键排序
    rows = _rows(result)
    assert len(rows) == 10  # 2 标的 × 5 交易日
    assert [(row["entity_id"], row["trade_date"]) for row in rows[:5]] == [
        (1, day) for day in (D1, D2, D3, D4, D6)
    ]
    by_key = {(row["entity_id"], row["trade_date"]): row["status"] for row in rows}
    assert by_key[(1, D3)] == STATUS_OK
    assert by_key[(1, D1)] == STATUS_MISSING
    assert by_key[(2, D1)] == STATUS_OK
    assert by_key[(2, D6)] == STATUS_MISSING


def test_align_calendar_pit_filters_future_rows(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    future = datetime(2026, 9, 20, 8, 0)
    rows = _calendar_rows()
    late = {D3, D4, D5, D6}
    for row in rows:
        if row["trade_date"] in late:
            row["knowledge_time"] = future
    _seed(db, metadata, CALENDAR, rows)
    result = _read(db)
    assert result.meta.trading_days == 2  # 仅 D1/D2 在 as_of 可见
    assert [row["trade_date"] for row in _rows(result)] == [D1, D2]


def test_align_calendar_invisible_raises(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed(db, metadata, CALENDAR, _calendar_rows(known=datetime(2026, 9, 20, 8, 0)))
    with pytest.raises(AlignmentCalendarUnavailable) as excinfo:
        _read(db)
    assert excinfo.value.code == "alignment_calendar_unavailable"
    assert "窗口" in str(excinfo.value)
    assert "覆盖" in excinfo.value.hint and "2015" in excinfo.value.hint


def test_align_reserved_field_conflict_raises(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    # 状态数据集自身含对齐保留列（is_suspended/is_st）：显式报错，不覆盖字段
    with pytest.raises(UnsupportedAlignment) as excinfo:
        read(
            db,
            STATUS,
            ["is_suspended"],
            as_of=AS_OF,
            entities=[1],
            window=WINDOW,
            align_calendar=True,
        )
    assert excinfo.value.code == "unsupported_alignment"
    assert "保留列" in str(excinfo.value)


def test_align_status_pit_and_latest_version(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed(db, metadata, CALENDAR, _calendar_rows())
    _seed(
        db,
        metadata,
        STATUS,
        [
            _status(1, D2, suspended=False, known=datetime(2026, 9, 11, 8, 0), version=1),
            _status(1, D2, suspended=True, known=datetime(2026, 9, 12, 8, 0), version=2),
            # 未来版本：as_of=09-17 时不可见
            _status(1, D2, suspended=False, known=datetime(2026, 9, 20, 8, 0), version=3),
        ],
    )
    early = _read(db, as_of=datetime(2026, 9, 11, 12, 0))
    assert _rows(early)[1]["status"] == STATUS_MISSING  # v1：未停牌且无 bar → 缺失
    late = _read(db)
    assert _rows(late)[1]["status"] == STATUS_SUSPENDED  # v2 最新可见版本
    assert _rows(late)[1]["is_suspended"] is True


def test_align_status_absent_warns_and_bi_state(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed(db, metadata, CALENDAR, _calendar_rows())
    _seed(db, metadata, DATASET, [_bar(1, D1, 10.0)])
    specs = {name: spec for name, spec in load_all().items() if name != STATUS}
    result = _read(db, specs=specs)
    assert list(result.table.column_names) == ["entity_id", "trade_date", "close", "status"]
    assert [row["status"] for row in _rows(result)] == [
        STATUS_OK,
        STATUS_MISSING,
        STATUS_MISSING,
        STATUS_MISSING,
        STATUS_MISSING,
    ]
    assert result.meta.status_dataset is None
    assert result.meta.warnings and "daily_status" in result.meta.warnings[0]


def test_align_combines_adjust(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed(db, metadata, CALENDAR, _calendar_rows())
    _seed(db, metadata, DATASET, [_bar(1, D1, 10.0), _bar(1, D3, 11.0)])
    _seed(db, metadata, FACTOR, [_factor(1, D1, 2.0), _factor(1, D3, 3.0)])
    result = _read(db, adjust="hfq")
    rows = _rows(result)
    assert rows[0]["close"] == 20.0  # 10 × 2
    assert rows[2]["close"] == 33.0  # 11 × 3
    assert pd.isna(rows[1]["close"])  # 停牌行仍缺失，未参与组合
    assert result.meta.adjust == "hfq"
    assert result.meta.adjusted_fields == ("close",)
    assert result.meta.factor_dataset == FACTOR


def test_align_scope_errors(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    with pytest.raises(InvalidAlignmentScope) as excinfo:
        read(db, DATASET, ["close"], as_of=AS_OF, entities=[1], align_calendar=True)
    assert excinfo.value.code == "invalid_alignment_scope"
    with pytest.raises(InvalidAlignmentScope):
        read(db, DATASET, ["close"], as_of=AS_OF, window=WINDOW, align_calendar=True)
    with pytest.raises(InvalidAlignmentScope, match="窗口非法"):
        _read(db, window=(D6, D1))


def test_align_unsupported_dataset(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed(db, metadata, CALENDAR, _calendar_rows())
    # 字典声明了日历年却缺该条目：显式报错，不静默降级
    specs = {name: spec for name, spec in load_all().items() if name != CALENDAR}
    with pytest.raises(UnsupportedAlignment) as excinfo:
        _read(db, specs=specs)
    assert excinfo.value.code == "unsupported_alignment"
    assert "不在字典中" in str(excinfo.value)


def test_align_empty_entities_returns_empty(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed(db, metadata, CALENDAR, _calendar_rows())
    result = _read(db, entities=[])
    assert result.meta.row_count == 0 and result.meta.trading_days == 5
    assert list(result.table.column_names) == [
        "entity_id",
        "trade_date",
        "close",
        "status",
        "is_suspended",
        "is_st",
    ]


def test_align_default_off_and_zero_writes(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_three_states(db, metadata)
    tables = [CALENDAR, DATASET, STATUS, FACTOR]

    def _counts() -> dict[str, int]:
        with db.connect() as connection:
            return {
                name: int(
                    connection.execute(
                        select(func.count()).select_from(metadata.tables[name])
                    ).scalar_one()
                )
                for name in tables
            }

    before = _counts()
    plain = read(
        db, DATASET, ["close"], as_of=AS_OF, entities=[1], window=WINDOW, adjust="raw"
    )
    assert plain.meta.aligned is False and plain.meta.trading_days is None
    assert list(plain.table.column_names) == ["entity_id", "trade_date", "close"]
    _read(db)
    assert _counts() == before  # 读路径零写入
