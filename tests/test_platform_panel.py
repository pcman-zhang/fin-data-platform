"""时序查询面测试（TASK-3.13）：序列/截面/面板/版本/asof join 与口径。

覆盖：日历对齐与重采样锚定、缺口策略（none/ffill）、窗口算子 PIT 正确性、
vintage 与版本历史、asof join、宽表/长表、结构化异常。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.access.errors import InvalidAlignmentScope
from fin_data_platform.panel import (
    InvalidArgument,
    InvalidFill,
    Rolling,
    UnsupportedFrequency,
    asof_join,
    get_cross_section,
    get_panel,
    get_series,
    get_versions,
)
from fin_data_platform.storage.schema import build_metadata

DATASET = "cn_equity.daily_bar"
STATUS = "cn_equity.daily_status"
DAYS = [
    date(2026, 9, 7),
    date(2026, 9, 8),
    date(2026, 9, 9),
    date(2026, 9, 10),
    date(2026, 9, 11),
    date(2026, 9, 14),
    date(2026, 9, 15),
    date(2026, 9, 16),
    date(2026, 9, 17),
    date(2026, 9, 18),
]
MISSING = DAYS[4]  # 真缺失（无行情、无状态）
SUSPENDED = DAYS[5]  # 停牌（无行情、状态为停牌）
AS_OF = datetime(2026, 9, 20, 12, 0)
KNOWN = datetime(2026, 9, 18, 8, 0)


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

    calendar = metadata.tables["ref.trade_calendar"]
    with engine.begin() as connection:
        connection.execute(
            calendar.insert(),
            [
                {
                    "exchange_id": "XSHG",
                    "trade_date": day,
                    "is_open": True,
                    "pretrade_date": None,
                    "knowledge_time": datetime(2026, 9, 1),
                    "version": 1,
                }
                for day in DAYS
            ],
        )

    bars = metadata.tables[DATASET]
    rows = []
    for day in DAYS:
        if day in (MISSING, SUSPENDED):
            continue
        close = float(DAYS.index(day) + 1)
        rows.append(
            {
                "entity_id": 1,
                "trade_date": day,
                "open": close - 0.2,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 100.0,
                "amount": close * 100.0,
                "knowledge_time": KNOWN,
                "ingest_time": KNOWN,
                "provider": "tushare",
                "version": 1,
            }
        )
    # 实体 1 的 D1 重述（知识时间晚于 AS_OF）
    rows.append(
        {
            "entity_id": 1,
            "trade_date": DAYS[0],
            "open": 0.8,
            "high": 1.5,
            "low": 0.5,
            "close": 99.0,
            "volume": 100.0,
            "amount": 9900.0,
            "knowledge_time": datetime(2026, 9, 25, 8, 0),
            "ingest_time": datetime(2026, 9, 25, 8, 0),
            "provider": "tushare",
            "version": 2,
        }
    )
    # 实体 2：仅前三天（矩阵/截面用；列集合与实体 1 不同 → 单独插入）
    rows2 = [
        {
            "entity_id": 2,
            "trade_date": day,
            "close": float(DAYS.index(day) + 1),
            "volume": 50.0,
            "knowledge_time": KNOWN,
            "ingest_time": KNOWN,
            "provider": "tushare",
            "version": 1,
        }
        for day in DAYS[:3]
    ]
    with engine.begin() as connection:
        connection.execute(bars.insert(), rows)
        connection.execute(bars.insert(), rows2)
        # 复权因子（默认口径 hfq）：=1.0，保持原始价
        factors = metadata.tables["cn_equity.adj_factor"]
        factor_rows = [
            {
                "entity_id": 1,
                "trade_date": day,
                "adj_factor": 1.0,
                "knowledge_time": KNOWN,
                "ingest_time": KNOWN,
                "provider": "tushare",
                "version": 1,
            }
            for day in DAYS
            if day not in (MISSING, SUSPENDED)
        ] + [
            {
                "entity_id": 2,
                "trade_date": day,
                "adj_factor": 1.0,
                "knowledge_time": KNOWN,
                "ingest_time": KNOWN,
                "provider": "tushare",
                "version": 1,
            }
            for day in DAYS[:3]
        ]
        connection.execute(factors.insert(), factor_rows)
        connection.execute(
            metadata.tables[STATUS].insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": SUSPENDED,
                    "is_suspended": True,
                    "is_st": False,
                    "knowledge_time": KNOWN,
                    "ingest_time": KNOWN,
                    "provider": "tushare",
                    "version": 1,
                }
            ],
        )
    return engine, metadata


def _series(engine, **kwargs):  # type: ignore[no-untyped-def]
    dataset = kwargs.pop("dataset", DATASET)
    options = {
        "entities": [1],
        "fields": ["open", "high", "low", "close", "volume"],
        "start": DAYS[0],
        "end": DAYS[-1],
        "as_of": AS_OF,
        "calendar": "trading",
    }
    options.update(kwargs)
    return get_series(engine, dataset, **options)


def test_daily_alignment_marks_missing_and_suspended(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    frame = _series(db)
    assert list(frame["trade_date"]) == DAYS  # 预期行 = 交易日（非交易日无行）
    assert list(frame["status"]) == [
        "ok",
        "ok",
        "ok",
        "ok",
        "missing",
        "suspended",
        "ok",
        "ok",
        "ok",
        "ok",
    ]
    assert pd.isna(frame.loc[4, "close"])  # missing → NaN
    assert pd.isna(frame.loc[5, "close"])  # suspended → NaN
    assert frame.loc[0, "close"] == 1.0  # 未来重述（09-25 知识）不可见（as_of=09-20）
    assert frame.attrs["freq"] == "1d" and frame.attrs["calendar"] == "trading"


def test_weekly_resample_calendar_anchor(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    frame = _series(db, freq="1w", fields=["open", "high", "low", "close", "volume"])
    assert list(frame["trade_date"]) == [DAYS[4], DAYS[9]]  # 桶锚点 = 该期最后交易日
    first, second = frame.iloc[0], frame.iloc[1]
    assert first["close"] == 4.0  # last（跳过缺失日）
    assert first["open"] == 0.8 and first["high"] == 4.5 and first["low"] == 0.5
    assert first["volume"] == 400.0  # sum（缺失日不贡献）
    assert first["status"] == "ok"  # 桶内任一 ok → ok
    assert second["close"] == 10.0 and second["volume"] == 400.0  # 停牌日无成交
    assert second["status"] == "ok"


def test_fill_ffill_and_window_operators_are_pit_safe(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    filled = _series(db, fields=["close"], fill="ffill")
    assert list(filled["close"]) == [1.0, 2.0, 3.0, 4.0, 4.0, 4.0, 7.0, 8.0, 9.0, 10.0]

    rolling = _series(db, fields=["close"], rolling=Rolling(window=3, op="mean"))
    values = rolling["close_roll3_mean"]
    assert values.iloc[:2].isna().all()
    assert values.iloc[2] == 2.0 and values.iloc[3] == 3.0
    assert pd.isna(values.iloc[4])  # 窗口内含缺失 → 不足 min_periods
    assert values.iloc[8] == 8.0 and values.iloc[9] == 9.0

    change = _series(db, fields=["close"], fill="ffill", change=1)
    assert abs(change.loc[7, "close_chg1"] - (8.0 / 7.0 - 1.0)) < 1e-12


def test_resample_uses_only_as_of_visible_rows(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    # D3 的重述晚于 as_of：重采样/前值都不得使用
    bars = metadata.tables[DATASET]
    with db.begin() as connection:
        connection.execute(
            bars.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": DAYS[2],
                    "close": 999.0,
                    "knowledge_time": datetime(2026, 9, 25, 8, 0),
                    "ingest_time": datetime(2026, 9, 25, 8, 0),
                    "provider": "tushare",
                    "version": 2,
                }
            ],
        )
    frame = _series(db, freq="1w", fields=["close"])
    assert frame.iloc[0]["close"] == 4.0  # 未来重述不参与聚合


def test_cross_section(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    plain = get_cross_section(
        db, DATASET, date=DAYS[2], as_of=AS_OF, fields=["close"], entities=[1]
    )
    assert len(plain) == 1 and plain.iloc[0]["close"] == 3.0

    aligned = get_cross_section(
        db,
        DATASET,
        date=MISSING,
        as_of=AS_OF,
        fields=["close"],
        entities=[1, 2],
        calendar="trading",
    )
    assert list(aligned["entity_id"]) == [1, 2]
    assert list(aligned["status"]) == ["missing", "missing"]


def test_panel_wide_and_long(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    wide = get_panel(
        db,
        DATASET,
        entities=[1, 2],
        fields=["close"],
        start=DAYS[0],
        end=DAYS[-1],
        as_of=AS_OF,
        calendar="trading",
        shape="wide",
    )
    assert wide.index.name == "trade_date" and len(wide) == len(DAYS)
    assert list(wide.columns) == [(1, "close"), (2, "close")]
    assert wide.loc[DAYS[0], (1, "close")] == 1.0
    assert wide.loc[DAYS[0], (2, "close")] == 1.0
    assert pd.isna(wide.loc[DAYS[3], (2, "close")])  # 实体 2 无当日行

    long = get_panel(
        db,
        DATASET,
        entities=[1],
        fields=["close"],
        start=DAYS[0],
        end=DAYS[-1],
        as_of=AS_OF,
        calendar="trading",
        shape="long",
    )
    assert len(long) == len(DAYS) and "status" in long.columns


def test_versions_history_and_vintage(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    history = get_versions(
        db, DATASET, entities=[1], start=DAYS[0], end=DAYS[0], fields=["close"]
    )
    assert list(history["version"]) == [1, 2]
    assert list(history["close"]) == [1.0, 99.0]

    truncated = get_versions(
        db,
        DATASET,
        entities=[1],
        start=DAYS[0],
        end=DAYS[0],
        fields=["close"],
        as_of=AS_OF,
    )
    assert list(truncated["version"]) == [1]

    vintage = get_versions(
        db,
        DATASET,
        entities=[1],
        start=DAYS[0],
        end=DAYS[0],
        fields=["close"],
        mode="vintage",
    )
    assert list(vintage["version"]) == [1] and list(vintage["close"]) == [1.0]
    assert vintage.attrs["mode"] == "vintage"


def test_asof_join_backward_by_and_tolerance(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    left = _series(db, fields=["close"])
    right = pd.DataFrame(
        {
            "entity_id": [1, 1],
            "trade_date": [DAYS[1], DAYS[6]],
            "factor": [2.0, 3.0],
        }
    )
    joined = asof_join(
        left[["entity_id", "trade_date", "close"]],
        right,
        left_on="trade_date",
        by="entity_id",
    )
    assert len(joined) == len(left)
    assert list(joined["factor"].dropna().unique()) == [2.0, 3.0]
    assert pd.isna(joined.loc[0, "factor"])  # D1 之前无右表观测
    assert joined.loc[2, "factor"] == 2.0  # 取最近 ≤ 观测

    limited = asof_join(
        left[["entity_id", "trade_date", "close"]],
        right,
        left_on="trade_date",
        by="entity_id",
        tolerance=pd.Timedelta(days=2),
    )
    assert pd.isna(limited.loc[4, "factor"])  # 超过容忍期（3 天 > 2 天）
    assert limited.loc[6, "factor"] == 3.0  # 同日观测

    with pytest.raises(InvalidArgument, match="对齐方向"):
        asof_join(left, right, left_on="trade_date", direction="sideways")


def test_structured_errors(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    with pytest.raises(UnsupportedFrequency, match="频率不受支持"):
        _series(db, freq="5m")
    with pytest.raises(InvalidFill, match="缺口策略"):
        _series(db, fill="nearest")
    with pytest.raises(InvalidArgument, match="calendar"):
        _series(db, calendar="natural")
    with pytest.raises(InvalidArgument, match="agg 仅在"):
        _series(db, agg={"close": "last"})
    with pytest.raises(InvalidArgument, match="shape"):
        get_panel(
            db,
            DATASET,
            entities=[1],
            fields=["close"],
            start=DAYS[0],
            end=DAYS[-1],
            as_of=AS_OF,
            shape="tall",
        )
    with pytest.raises(InvalidArgument, match="window"):
        Rolling(window=0)
    with pytest.raises(InvalidAlignmentScope):
        _series(db, entities=None)  # 对齐路径需要显式 entities


def test_asof_join_multi_entity_by_group() -> None:
    """多实体（by 分组）asof join：on 列非全局单实体排序也必须可用。"""
    d1, d2, d3 = DAYS[0], DAYS[1], DAYS[2]
    left = pd.DataFrame(
        {
            "entity_id": [1, 2, 1, 2, 1, 2],
            "trade_date": [d1, d1, d2, d2, d3, d3],
            "close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    )
    right = pd.DataFrame(
        {
            "entity_id": [1, 2, 1, 2],
            "trade_date": [d1, d1, d3, d3],
            "factor": [10.0, 20.0, 30.0, 40.0],
        }
    )
    joined = asof_join(left, right, left_on="trade_date", by="entity_id")
    assert list(joined["entity_id"]) == [1, 1, 1, 2, 2, 2]  # 输出按 by 分组排序
    # 每实体各自向前取最近观测（D2 无右表观测 → 沿用 D1）
    assert list(joined["factor"]) == [10.0, 10.0, 30.0, 20.0, 20.0, 40.0]
