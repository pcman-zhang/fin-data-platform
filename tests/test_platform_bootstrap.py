"""TASK-3.6 切片 3 测试：SyncSettings 解析 + 配置驱动的 Runtime 装配。"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.ingestion import SyncSettings, build_sync_runtime
from fin_data_platform.runtime import RuntimeConfig, SqlMetaRepository
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata

DAY1 = date(2026, 9, 10)
DAY2 = date(2026, 9, 11)
CODE = "600519.SH"
JOB_ID = f"sync.cn_equity.daily_bar.{CODE}"
FACTOR_JOB_ID = f"sync.cn_equity.adj_factor.{CODE}"


class FakeHub:
    def get_bars(
        self,
        codes,
        *,
        start: str,
        end: str,
        freq: str = "1d",
        adjust: str | None = None,
        source=None,
        **_: object,
    ) -> pd.DataFrame:
        rows = [
            {
                "code": CODE,
                "date": pd.Timestamp("2026-09-10"),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "volume": 1000.0,
                "amount": 10500.0,
            },
            {
                "code": CODE,
                "date": pd.Timestamp("2026-09-11"),
                "open": 10.5,
                "high": 11.5,
                "low": 10.0,
                "close": 11.0,
                "volume": 1200.0,
                "amount": 13200.0,
            },
        ]
        frame = pd.DataFrame(
            [row for row in rows if start <= row["date"].date().isoformat() <= end]
        )
        frame.attrs["source"] = "tushare"
        return frame

    def get_adjust_factors(
        self, codes, *, start: str, end: str, source=None, **_: object
    ) -> pd.DataFrame:
        rows = [
            {"code": CODE, "date": pd.Timestamp("2026-09-10"), "adj_factor": 8.0},
            {"code": CODE, "date": pd.Timestamp("2026-09-11"), "adj_factor": 8.5},
        ]
        frame = pd.DataFrame(
            [row for row in rows if start <= row["date"].date().isoformat() <= end]
        )
        frame.attrs["source"] = "tushare"
        return frame

    def get_trade_calendar(self, *, start: str, end: str, source=None) -> pd.DataFrame:
        days = [day for day in (DAY1, DAY2) if day.isoformat() <= end]
        return pd.DataFrame({"date": days, "is_open": [True] * len(days)})


class _NoFactorAdapter:
    capabilities: frozenset = frozenset()


class _NoFactorRegistry:
    def get(self, source: str) -> _NoFactorAdapter:
        return _NoFactorAdapter()


class FakeAkshareHub(FakeHub):
    """akshare 形态：不声明复权因子能力（注册时应跳过因子任务）。"""

    registry = _NoFactorRegistry()


def _config() -> RuntimeConfig:
    return RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://"), role="all")


@pytest.fixture()
def engine():
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
    return engine


def test_sync_settings_from_env() -> None:
    assert SyncSettings.from_env({}) is None

    settings = SyncSettings.from_env(
        {
            "FDP_SYNC_CODES": "600519.SH, 000001.SZ ,600519.SH",
            "FDP_SYNC_START": "2026-09-01",
            "FDP_SYNC_SOURCE": "tushare",
            "FDP_SYNC_SCHEDULE": "0 9 * * 1-5",
        }
    )
    assert settings is not None
    assert settings.codes == ("600519.SH", "000001.SZ")  # 去重、去空白
    assert settings.start == date(2026, 9, 1)
    assert settings.source == "tushare"
    assert settings.schedule == "0 9 * * 1-5"

    base = {"FDP_SYNC_CODES": "600519.SH", "FDP_SYNC_START": "2026-09-01"}

    with pytest.raises(ValueError, match="FDP_SYNC_START"):
        SyncSettings.from_env({"FDP_SYNC_CODES": "600519.SH"})
    with pytest.raises(ValueError, match="非法日期"):
        SyncSettings.from_env({**base, "FDP_SYNC_START": "09/01/2026"})
    # source 必填且必须为已知数据源（Hub 不做隐式路由）
    with pytest.raises(ValueError, match="FDP_SYNC_SOURCE"):
        SyncSettings.from_env(base)
    with pytest.raises(ValueError, match="非法数据源"):
        SyncSettings.from_env({**base, "FDP_SYNC_SOURCE": "Tushare"})
    # schedule 预校验（干净退出，不走 traceback）
    schedule_base = {**base, "FDP_SYNC_SOURCE": "tushare"}
    with pytest.raises(ValueError, match="FDP_SYNC_SCHEDULE"):
        SyncSettings.from_env({**schedule_base, "FDP_SYNC_SCHEDULE": "garbage"})
    with pytest.raises(ValueError, match="FDP_SYNC_SCHEDULE"):
        SyncSettings.from_env({**schedule_base, "FDP_SYNC_SCHEDULE": "interval:abc"})
    # 直接构造同样校验
    with pytest.raises(ValueError, match="FDP_SYNC_SOURCE"):
        SyncSettings(codes=("600519.SH",), start=DAY1, source="")


def test_build_sync_runtime_wires_tasks_and_windows(engine) -> None:
    settings = SyncSettings(codes=(CODE,), start=DAY1, source="tushare")
    app = build_sync_runtime(_config(), settings, hub=FakeHub(), engine=engine)

    specs = app.registry.specs()
    assert [spec.job_id for spec in specs] == [JOB_ID, FACTOR_JOB_ID]
    assert specs[0].schedule is None  # 未配置 → 手动/轮询
    app.sync_metadata()

    # 水位窗口由 provider 装配：首次 = [起点, 最近已收盘]（日线 + 复权因子各一条）
    intents = app.tick(now=datetime(2026, 9, 12, 8, 0))
    assert {intent.job_id for intent in intents} == {JOB_ID, FACTOR_JOB_ID}
    for intent in intents:
        assert intent.window_start == DAY1
        assert intent.window_end == DAY2

    for intent in intents:
        assert app.submit(intent) == "created"
    assert app.run_pending() == 2

    metadata, _ = build_metadata()
    for dataset in ("cn_equity.daily_bar", "cn_equity.adj_factor"):
        table = metadata.tables[dataset]
        with engine.begin() as connection:
            rows = connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
        assert int(rows) == 2, dataset

    repo = SqlMetaRepository(engine)
    for dataset in ("cn_equity.daily_bar", "cn_equity.adj_factor"):
        mark = repo.get_watermark(dataset, scope=CODE)
        assert mark is not None and mark.watermark_time is not None
        assert mark.watermark_time.date() == DAY2, dataset


def test_build_sync_runtime_without_settings(engine) -> None:
    app = build_sync_runtime(_config(), None, engine=engine)
    assert app.registry.specs() == []
    assert app.tick(now=datetime(2026, 9, 12, 8, 0)) == []


def test_build_sync_runtime_skips_factor_task_without_capability(engine) -> None:
    """源无复权因子能力（akshare）时只注册日线任务，窗口/执行照常。"""
    settings = SyncSettings(codes=(CODE,), start=DAY1, source="akshare")
    app = build_sync_runtime(_config(), settings, hub=FakeAkshareHub(), engine=engine)

    assert [spec.job_id for spec in app.registry.specs()] == [JOB_ID]
    app.sync_metadata()
    intents = app.tick(now=datetime(2026, 9, 12, 8, 0))
    assert {intent.job_id for intent in intents} == {JOB_ID}
    for intent in intents:
        assert app.submit(intent) == "created"
    assert app.run_pending() == 1

    metadata, _ = build_metadata()
    table = metadata.tables["cn_equity.daily_bar"]
    with engine.begin() as connection:
        rows = connection.execute(select(func.count()).select_from(table)).scalar_one()
    assert int(rows) == 2
    factor_table = metadata.tables["cn_equity.adj_factor"]
    with engine.begin() as connection:
        factor_rows = connection.execute(
            select(func.count()).select_from(factor_table)
        ).scalar_one()
    assert int(factor_rows) == 0
