"""空窗口守卫测试（TASK-3.33）：源端 0 行软失败 / 不推进水位 / fail-open。

覆盖：有历史数据时空窗口软失败（重试、水位不动）、无历史（前上市/首次同步）允许、
无交易日允许、日历不可见 fail-open、重试耗尽 DEAD、复权因子同路径、幂等重放不误判。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.ingestion import (
    register_adj_factor_task,
    register_daily_bar_task,
)
from fin_data_platform.ingestion.common import (
    EmptySourceWindow,
    SyncResult,
    calendar_open_days,
    entity_has_history,
)
from fin_data_platform.ingestion.tasks import _guard_empty_window
from fin_data_platform.registry.store import EntityStore
from fin_data_platform.runtime import RuntimeApp, RuntimeConfig, SqlMetaRepository, TaskRegistry
from fin_data_platform.runtime.models import JobIntent, JobStatus
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata

CODE = "600519.SH"
D1 = date(2026, 9, 10)
D2 = date(2026, 9, 11)
D3 = date(2026, 9, 14)
D4 = date(2026, 9, 15)


class FakeHub:
    def __init__(
        self, rows: list[dict] | None = None, factor_rows: list[dict] | None = None
    ) -> None:
        self._rows = rows or []
        self._factor_rows = factor_rows or []
        self.calls: list[tuple] = []

    def get_bars(self, codes, *, start: str, end: str, **_kw: Any) -> pd.DataFrame:
        self.calls.append(("bars", tuple(codes), start, end))
        frame = pd.DataFrame(self._rows)
        frame.attrs["source"] = "tushare"
        return frame

    def get_adjust_factors(self, codes, *, start: str, end: str, **_kw: Any) -> pd.DataFrame:
        self.calls.append(("factors", tuple(codes), start, end))
        frame = pd.DataFrame(self._factor_rows)
        frame.attrs["source"] = "tushare"
        return frame


def _bar(day: date) -> dict:
    return {
        "code": CODE,
        "date": pd.Timestamp(day),
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 100.0,
        "amount": 1000.0,
    }


def _factor(day: date) -> dict:
    return {"code": CODE, "date": pd.Timestamp(day), "adj_factor": 8.0}


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
    return engine, metadata


def _seed_calendar(engine, metadata, flags: dict[date, bool]) -> None:  # type: ignore[no-untyped-def]
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
                    "knowledge_time": pd.Timestamp("2026-09-01"),
                    "version": 1,
                }
                for day, is_open in flags.items()
            ],
        )


def _runtime(engine, hub, *, factor: bool = False, max_attempts: int = 3):  # type: ignore[no-untyped-def]
    registry = TaskRegistry()
    spec = (
        register_adj_factor_task(registry, engine, hub, code=CODE, max_attempts=max_attempts)
        if factor
        else register_daily_bar_task(registry, engine, hub, code=CODE, max_attempts=max_attempts)
    )
    repository = SqlMetaRepository(engine)
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://")),
        engine=engine,
        repository=repository,
        registry=registry,
    )
    app.sync_metadata()
    return app, spec, repository


def _run(app, spec, start: date, end: date) -> None:  # type: ignore[no-untyped-def]
    intent = JobIntent(
        kind=spec.kind,
        job_id=spec.job_id,
        dataset=spec.dataset,
        scope=spec.scope,
        window_start=start,
        window_end=end,
        priority=spec.priority,
        max_attempts=spec.max_attempts,
    )
    assert app.submit(intent) == "created"
    app.run_pending()


def _watermark(repository, dataset: str):  # type: ignore[no-untyped-def]
    mark = repository.get_watermark(dataset, scope=CODE)
    return None if mark is None or mark.watermark_time is None else mark.watermark_time.date()


def _last_run(repository):  # type: ignore[no-untyped-def]
    return repository.list_runs(limit=1)[0]


def test_empty_window_with_history_soft_fails_and_keeps_watermark(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_calendar(db, metadata, {D1: True, D2: True, D3: True, D4: True})
    hub = FakeHub([_bar(D1), _bar(D2)])
    app, spec, repo = _runtime(db, hub)
    _run(app, spec, D1, D2)
    assert _watermark(repo, spec.dataset) == D2

    hub._rows = []  # 源端空响应（通道故障被吞）
    _run(app, spec, D3, D4)
    run = _last_run(repo)
    assert run.status == JobStatus.RETRYING.value
    assert "EmptySourceWindow" in (run.error or "")
    assert str(D3) in (run.error or "") and str(D4) in (run.error or "")
    assert _watermark(repo, spec.dataset) == D2  # 不推进水位


def test_empty_window_retry_exhausted_to_dead(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_calendar(db, metadata, {D1: True, D2: True, D3: True})
    hub = FakeHub([_bar(D1)])
    app, spec, repo = _runtime(db, hub, max_attempts=1)
    _run(app, spec, D1, D1)

    hub._rows = []
    _run(app, spec, D2, D3)
    run = _last_run(repo)
    assert run.status == JobStatus.DEAD.value
    assert "EmptySourceWindow" in (run.error or "")
    assert _watermark(repo, spec.dataset) == D1


def test_empty_window_without_history_allowed(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_calendar(db, metadata, {D1: True, D2: True})
    app, spec, repo = _runtime(db, FakeHub())  # 无历史：前上市 / 首次同步
    _run(app, spec, D1, D2)
    run = _last_run(repo)
    assert run.status == JobStatus.SUCCEEDED.value and run.rows_written == 0
    assert _watermark(repo, spec.dataset) == D2


def test_empty_window_without_trading_days_allowed(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_calendar(db, metadata, {D1: False, D2: False})
    hub = FakeHub()
    app, spec, repo = _runtime(db, hub)
    EntityStore(db).ensure_entity(code=CODE)  # 实体已注册但窗口无交易日
    _run(app, spec, D1, D2)
    assert _last_run(repo).status == JobStatus.SUCCEEDED.value
    assert _watermark(repo, spec.dataset) == D2


def test_empty_window_without_calendar_fail_open(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    app, spec, repo = _runtime(db, FakeHub())  # 日历未导入：无法判定 → 允许
    _run(app, spec, D1, D2)
    assert _last_run(repo).status == JobStatus.SUCCEEDED.value


def test_adjust_factor_empty_window_soft_fails(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_calendar(db, metadata, {D1: True, D2: True})
    hub = FakeHub(factor_rows=[_factor(D1)])
    app, spec, repo = _runtime(db, hub, factor=True)
    _run(app, spec, D1, D1)
    assert _watermark(repo, spec.dataset) == D1

    hub._factor_rows = []
    _run(app, spec, D1, D2)  # 覆盖窗口（含已有数据日）：源端 0 行 → 软失败
    run = _last_run(repo)
    assert run.status == JobStatus.RETRYING.value
    assert "EmptySourceWindow" in (run.error or "")


def test_guard_matrix_does_not_flag_replay_or_missing_calendar(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_calendar(db, metadata, {D1: True, D2: True})
    entity_id = EntityStore(db).ensure_entity(code=CODE).entity_id

    def _result(fetched: int, rows_written: int) -> SyncResult:
        return SyncResult(
            dataset="cn_equity.daily_bar",
            code=CODE,
            entity_id=entity_id,
            window_start=D1,
            window_end=D2,
            fetched=fetched,
            rows_written=rows_written,
            provider="tushare",
        )

    # 幂等重放 / 部分缺失：fetched > 0 不判失败（即使 0 写入）
    _guard_empty_window(db, "日线同步", _result(fetched=2, rows_written=0))
    # 无历史（前上市）：允许
    _guard_empty_window(db, "日线同步", _result(fetched=0, rows_written=0))

    # 有历史 + 窗口含交易日 + 源端 0 行 → 软失败
    _seed(
        db,
        metadata,
        "cn_equity.daily_bar",
        [_bar_row(entity_id, D1)],
    )
    with pytest.raises(EmptySourceWindow, match="含 2 个交易日"):
        _guard_empty_window(db, "日线同步", _result(fetched=0, rows_written=0))


def test_primitives_calendar_and_history(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    assert calendar_open_days(db, start=D1, end=D2) is None  # 未导入：无法判定
    _seed_calendar(db, metadata, {D1: True, D2: False, D3: True})
    assert calendar_open_days(db, start=D1, end=D1) == [D1]
    assert calendar_open_days(db, start=D2, end=D2) == []  # 有日历行但非交易日
    assert calendar_open_days(db, start=D1, end=D3) == [D1, D3]

    entity_id = EntityStore(db).ensure_entity(code=CODE).entity_id
    assert entity_has_history(db, "cn_equity.daily_bar", entity_id=entity_id, through=D2) is False
    _seed(db, metadata, "cn_equity.daily_bar", [_bar_row(entity_id, D1)])
    assert entity_has_history(db, "cn_equity.daily_bar", entity_id=entity_id, through=D1) is True
    assert (
        entity_has_history(
            db, "cn_equity.daily_bar", entity_id=entity_id, through=date(2026, 9, 9)
        )
        is False
    )


def _bar_row(entity_id: int, day: date) -> dict:
    """最小 daily_bar 行（在市判定用；非空必填列）。"""
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "knowledge_time": pd.Timestamp("2026-09-12"),
        "ingest_time": pd.Timestamp("2026-09-12"),
        "provider": "tushare",
        "version": 1,
    }


def _seed(engine, metadata, dataset: str, rows: list[dict]) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as connection:
        connection.execute(metadata.tables[dataset].insert(), rows)
