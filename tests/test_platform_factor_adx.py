"""ADX 因子（TASK-3.30）：数值对账 / 预热边界 / as-of 防前视 / 端到端物化。

数据：单实体 40 个交易日，OHLC 为确定性合成序列（趋势 + 回撤）；因子恒为 1
（hfq/raw 数值相同）。参考实现：numpy 显式循环（与因子代码互不共享）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.derived.consistency import check_consistency, import_implementations
from fin_data_platform.derived.engine import DerivedEngine
from fin_data_platform.derived.factor_api import FactorAPI
from fin_data_platform.derived.registry import DEFAULT_REGISTRY
from fin_data_platform.derived.smoothing import wilder_smooth
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.storage.schema import build_metadata

DATASET = "cn_equity.daily_bar"
AS_OF = datetime(2026, 6, 30, 12, 0)
PERIOD = 14


def _synthetic_bars(count: int = 40) -> list[tuple[float, float, float]]:
    """确定性合成 OHLC（趋势 + 周期性回撤，ADX 非零且波动）。"""
    rows: list[tuple[float, float, float]] = []
    close = 100.0
    for index in range(count):
        close += 1.6 if index % 5 < 3 else -1.1
        high = close + 1.5 + (index % 3) * 0.2
        low = close - 1.2 - (index % 4) * 0.15
        rows.append((close, high, low))
    return rows


def _reference_adx(bars: list[tuple[float, float, float]]) -> list[float | None]:
    import numpy as np

    close = np.array([row[0] for row in bars], dtype="float64")
    high = np.array([row[1] for row in bars], dtype="float64")
    low = np.array([row[2] for row in bars], dtype="float64")
    size = len(bars)

    tr = np.zeros(size)
    plus_dm = np.zeros(size)
    minus_dm = np.zeros(size)
    for index in range(size):
        prev_close = close[index - 1] if index > 0 else close[index]
        tr[index] = max(
            high[index] - low[index],
            abs(high[index] - prev_close),
            abs(low[index] - prev_close),
        )
        if index > 0:
            up = high[index] - high[index - 1]
            down = low[index - 1] - low[index]
            plus_dm[index] = up if (up > down and up > 0) else 0.0
            minus_dm[index] = down if (down > up and down > 0) else 0.0

    def smooth(values: list[float]) -> list[float | None]:
        out: list[float | None] = []
        state: float | None = None
        seed: list[float] = []
        for value in values:
            if state is None:
                seed.append(value)
                if len(seed) < PERIOD:
                    out.append(None)
                    continue
                state = sum(seed) / PERIOD
            else:
                state += (value - state) / PERIOD
            out.append(state)
        return out

    tr_s = smooth(tr.tolist())
    plus_s = smooth(plus_dm.tolist())
    minus_s = smooth(minus_dm.tolist())

    dx: list[float | None] = []
    for index in range(size):
        if tr_s[index] is None:
            dx.append(None)
            continue
        if tr_s[index] <= 0:
            dx.append(0.0)
            continue
        plus_di = 100.0 * plus_s[index] / tr_s[index]
        minus_di = 100.0 * minus_s[index] / tr_s[index]
        total = plus_di + minus_di
        dx.append(0.0 if total <= 0 else 100.0 * abs(plus_di - minus_di) / total)
    return smooth([value for value in dx if value is not None])


@pytest.fixture()
def engine():  # type: ignore[no-untyped-def]
    import pandas as pd

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

    days = [item.date() for item in pd.bdate_range("2026-01-05", periods=40)]
    bars = _synthetic_bars(len(days))
    daily = metadata.tables[DATASET]
    factor = metadata.tables["cn_equity.adj_factor"]
    known = datetime(2026, 6, 1, 12, 0)  # 全部行在 as_of 之前可知
    with engine.begin() as connection:
        connection.execute(
            daily.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": day,
                    "high": high,
                    "low": low,
                    "close": close,
                    "knowledge_time": known,
                    "ingest_time": known,
                    "provider": "tushare",
                    "version": 1,
                }
                for day, (close, high, low) in zip(days, bars, strict=True)
            ],
        )
        connection.execute(
            factor.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": day,
                    "adj_factor": 1.0,
                    "knowledge_time": known,
                    "ingest_time": known,
                    "provider": "tushare",
                    "version": 1,
                }
                for day in days
            ],
        )
    return engine, metadata, days


def _engine_for(engine):  # type: ignore[no-untyped-def]
    specs = load_all()
    assert import_implementations(specs) == []
    return DerivedEngine(engine, specs=specs), specs


# ------------------------------------------------------------------ 原语与登记
def test_wilder_smooth_matches_textbook_recursion() -> None:
    values = [float(item) for item in range(1, 8)]
    smoothed = wilder_smooth(values, 3)
    assert smoothed[:2] == [None, None]
    assert smoothed[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert smoothed[3] == pytest.approx(2.0 + (4 - 2.0) / 3)
    assert smoothed[4] == pytest.approx(smoothed[3] + (5 - smoothed[3]) / 3)


def test_shipped_dictionary_and_registry_consistent() -> None:
    specs = load_all()
    assert check_consistency(specs) == []
    spec = DEFAULT_REGISTRY.get("adx")
    assert spec is not None and spec.version == 1


# ------------------------------------------------------------------ 数值对账
def test_adx_matches_reference_and_warmup_boundary(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata, days = engine
    derived, _specs = _engine_for(db)
    rows = derived.execute("adx", as_of=AS_OF).values.to_pylist()

    reference = _reference_adx(_synthetic_bars(len(days)))
    expected = [value for value in reference if value is not None]
    # 预热边界：前 2·14-2 = 26 根 bar 无输出
    assert len(rows) == len(expected) == len(days) - (2 * PERIOD - 2)
    assert str(rows[0]["trade_date"]) == str(days[2 * PERIOD - 2])
    actual = [row["adx"] for row in rows]
    assert actual == pytest.approx(expected, rel=1e-12)
    assert all(0.0 <= value <= 100.0 for value in actual)


def test_adx_respects_knowledge_time(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata, days = engine
    derived, _specs = _engine_for(db)
    before = [row["adx"] for row in derived.execute("adx", as_of=AS_OF).values.to_pylist()]

    # 最后一根 bar 被重述（知识时间在 as_of 之后）→ 早于重述的 as_of 不受影响
    daily = metadata.tables[DATASET]
    with db.begin() as connection:
        connection.execute(
            daily.insert(),
            [
                {
                    "entity_id": 1,
                    "trade_date": days[-1],
                    "high": 999.0,
                    "low": 900.0,
                    "close": 950.0,
                    "knowledge_time": datetime(2026, 7, 15, 12, 0),
                    "ingest_time": datetime(2026, 7, 15, 12, 0),
                    "provider": "tushare",
                    "version": 2,
                }
            ],
        )
    guarded = [row["adx"] for row in derived.execute("adx", as_of=AS_OF).values.to_pylist()]
    assert guarded == pytest.approx(before)


# ------------------------------------------------------------------ 端到端
def test_adx_materialize_and_factor_read(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata, _days = engine
    store = InMemoryAlgorithmStore()
    specs = load_all()
    assert import_implementations(specs) == []
    DerivedEngine(db, specs=specs, store=store).materialize("adx", as_of=utcnow())

    api = FactorAPI(db, specs=specs, store=store)
    result = api.read("adx", as_of=utcnow())
    assert result.meta.materialized is True
    assert result.meta.algorithm_id == "adx" and result.meta.algorithm_version == 1
    assert result.meta.data_generation == store.get_generation("mart.derived_daily_bar_adx")
    assert result.values.num_rows == len(_synthetic_bars(40)) - (2 * PERIOD - 2)
