"""Ingestion 最小 sync 切片测试（TASK-3.6 切片 1）：FakeHub → canonical 幂等写入。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.ingestion import (
    register_adj_factor_task,
    register_daily_bar_task,
    sync_adjust_factor,
    sync_daily_bar,
)
from fin_data_platform.registry.schema import entity
from fin_data_platform.runtime import (
    RuntimeApp,
    RuntimeConfig,
    SqlMetaRepository,
    TaskRegistry,
)
from fin_data_platform.runtime.models import JobIntent
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata

DAY1 = date(2026, 9, 10)
DAY2 = date(2026, 9, 11)


class FakeHub:
    def __init__(
        self,
        rows: list[dict],
        source_attr: str | None = "akshare",
        factor_rows: list[dict] | None = None,
    ) -> None:
        self._rows = rows
        self._factor_rows = factor_rows or []
        self._source_attr = source_attr
        self.calls: list[tuple] = []

    def get_adjust_factors(
        self, codes, *, start: str, end: str, source=None, **_: object
    ) -> pd.DataFrame:
        self.calls.append(("factors", tuple(codes), start, end, source))
        frame = pd.DataFrame(self._factor_rows)
        if self._source_attr is not None:
            frame.attrs["source"] = self._source_attr
        return frame

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
        self.calls.append((tuple(codes), start, end, adjust, source))
        frame = pd.DataFrame(self._rows)
        if self._source_attr is not None:
            frame.attrs["source"] = self._source_attr
        return frame


def _bars() -> list[dict]:
    return [
        {
            "code": "600519.SH",
            "date": pd.Timestamp("2026-09-10"),
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": 10.5,
            "volume": 1000.0,
            "amount": 10500.0,
        },
        {
            "code": "600519.SH",
            "date": pd.Timestamp("2026-09-11"),
            "open": 10.5,
            "high": 11.5,
            "low": 10.0,
            "close": 11.0,
            "volume": 1200.0,
            "amount": 13200.0,
        },
    ]


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


def _count(engine, table) -> int:
    with engine.begin() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def test_sync_daily_bar_is_idempotent(engine) -> None:
    hub = FakeHub(_bars())
    first = sync_daily_bar(engine, hub, code="600519.SH", start=DAY1, end=DAY2)
    assert (first.fetched, first.rows_written, first.provider) == (2, 2, "akshare")

    second = sync_daily_bar(engine, hub, code="600519.SH", start=DAY1, end=DAY2)
    assert second.entity_id == first.entity_id  # 稳定主键（实体注册表）
    assert second.rows_written == 0  # 物理键冲突跳过

    metadata, _ = build_metadata()
    assert _count(engine, metadata.tables["cn_equity.daily_bar"]) == 2
    assert _count(engine, entity) == 1


def test_sync_requires_known_provider(engine) -> None:
    hub = FakeHub(_bars(), source_attr=None)
    with pytest.raises(ValueError, match="无法识别 provider"):
        sync_daily_bar(engine, hub, code="600519.SH", start=DAY1, end=DAY2)


def test_sync_task_through_runtime(engine) -> None:
    hub = FakeHub(_bars())
    registry = TaskRegistry()
    spec = register_daily_bar_task(registry, engine, hub, code="600519.SH")
    repo = SqlMetaRepository(engine)
    config = RuntimeConfig(
        storage=StorageConfig(write_dsn="sqlite://"),
        role="all",
        worker_count=1,
        tick_interval=0.01,
        worker_interval=0.01,
    )
    app = RuntimeApp(config, engine=engine, repository=repo, registry=registry)
    app.sync_metadata()

    intent = JobIntent(
        kind="sync",
        job_id=spec.job_id,
        dataset="cn_equity.daily_bar",
        scope="600519.SH",
        window_start=DAY1,
        window_end=DAY2,
    )
    assert app.submit(intent) == "created"
    assert app.run_pending() == 1

    runs = repo.list_runs()
    assert [run.status for run in runs] == ["succeeded"]
    assert runs[0].rows_written == 2

    # 同窗口重复提交：幂等（duplicate），不重复执行
    assert app.submit(intent) == "duplicate"
    assert app.run_pending() == 0
    metadata, _ = build_metadata()
    assert _count(engine, metadata.tables["cn_equity.daily_bar"]) == 2
    assert hub.calls == [(("600519.SH",), DAY1.isoformat(), DAY2.isoformat(), None, None)]


def test_sync_appends_revision_when_values_change(engine) -> None:
    hub = FakeHub(_bars())
    first = sync_daily_bar(engine, hub, code="600519.SH", start=DAY1, end=DAY2)
    assert first.rows_written == 2

    changed = _bars()
    changed[0]["close"] = 99.0  # 源端修订
    second = sync_daily_bar(engine, FakeHub(changed), code="600519.SH", start=DAY1, end=DAY2)
    assert second.rows_written == 1  # 仅修订日追加新版本

    metadata, _ = build_metadata()
    table = metadata.tables["cn_equity.daily_bar"]
    with engine.begin() as connection:
        rows = connection.execute(
            select(table.c.trade_date, table.c.close, table.c.version)
            .where(table.c.entity_id == first.entity_id)
            .order_by(table.c.trade_date, table.c.version)
        ).all()
    assert len(rows) == 3  # 历史版本保留 + 修订版本
    day1 = [(float(row[1]), int(row[2])) for row in rows if row[0] == DAY1]
    assert day1 == [(10.5, 1), (99.0, 2)]

    third = sync_daily_bar(engine, FakeHub(changed), code="600519.SH", start=DAY1, end=DAY2)
    assert third.rows_written == 0  # 值未再变化：无新版本


# ------------------------------------------------------------------ 复权因子（TASK-3.29）
def _factors() -> list[dict]:
    return [
        {"code": "600519.SH", "date": pd.Timestamp("2026-09-10"), "adj_factor": 8.0},
        {"code": "600519.SH", "date": pd.Timestamp("2026-09-11"), "adj_factor": 8.5},
    ]


def _count_factor(engine) -> int:
    from fin_data_platform.ingestion.adj_factor import _factor_table

    with engine.begin() as connection:
        return int(
            connection.execute(select(func.count()).select_from(_factor_table())).scalar_one()
        )


def test_sync_adjust_factor_idempotent_and_revision(engine) -> None:
    hub = FakeHub([], factor_rows=_factors())
    first = sync_adjust_factor(
        engine, hub, code="600519.SH", start=DAY1, end=DAY2, source="akshare"
    )
    assert (first.fetched, first.rows_written, first.provider) == (2, 2, "akshare")
    assert first.dataset == "cn_equity.adj_factor"

    second = sync_adjust_factor(
        engine, hub, code="600519.SH", start=DAY1, end=DAY2, source="akshare"
    )
    assert second.rows_written == 0  # 同值重跑：幂等
    assert _count_factor(engine) == 2

    # 源值修订 → 追加新版本（不改写历史）
    revised = _factors()
    revised[1]["adj_factor"] = 9.0
    hub._factor_rows = revised
    third = sync_adjust_factor(
        engine, hub, code="600519.SH", start=DAY1, end=DAY2, source="akshare"
    )
    assert third.rows_written == 1
    assert _count_factor(engine) == 3


def test_adjust_factor_task_through_runtime(engine) -> None:
    repository = SqlMetaRepository(engine)
    registry = TaskRegistry()
    spec = register_adj_factor_task(
        registry, engine, FakeHub([], factor_rows=_factors()), code="600519.SH"
    )
    assert spec.job_id == "sync.cn_equity.adj_factor.600519.SH"
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://")),
        engine=engine,
        repository=repository,
        registry=registry,
    )
    app.sync_metadata()
    app.submit(
        JobIntent(
            kind="sync",
            job_id=spec.job_id,
            dataset="cn_equity.adj_factor",
            scope="600519.SH",
            window_start=DAY1,
            window_end=DAY2,
        )
    )
    assert app.run_pending() == 1
    run = repository.list_runs()[0]
    assert run.status == "succeeded" and run.rows_written == 2
    mark = repository.get_watermark("cn_equity.adj_factor", scope="600519.SH")
    assert mark is not None and mark.watermark_time is not None
