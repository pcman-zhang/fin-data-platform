"""全市场基础信息与生命周期同步测试（TASK-3.35）。

覆盖：四类身份登记 / 股票两行生命周期 / ETF·基金 listed 行 / 指数仅身份 /
knowledge_time 口径 / 幂等重跑 / 退市日期修订（含旧开放行失效闭合）/ 遗留分类清洗 /
非法行跳过 / Runtime 任务装配与执行。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.ingestion import (
    MARKET_REGISTRY_JOB,
    register_market_registry_task,
    sync_market_registry,
)
from fin_data_platform.ingestion.bootstrap import supports_capability
from fin_data_platform.ingestion.market_registry import DATASET
from fin_data_platform.registry.store import EntityStore
from fin_data_platform.runtime import RuntimeApp, RuntimeConfig, SqlMetaRepository, TaskRegistry
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata

CODE_STOCK = "600519.SH"
CODE_DELISTED = "000001.SZ"
CODE_ETF = "510300.SH"
CODE_FUND = "110022.OF"
CODE_INDEX = "000300.SH"


class FakeHub:
    """参照数据桩：按 kind 返回帧并记录调用（attrs.source=tushare）。

    ``require_source=True`` 模拟 Hub 的显式来源要求（缺 source 报 ValueError）。
    """

    def __init__(
        self, frames: dict[str, list[dict]] | None = None, *, require_source: bool = False
    ) -> None:
        self._frames = frames or {}
        self._require_source = require_source
        self.calls: list[tuple[str, object]] = []

    def get_reference(self, kind: str, *, source: object = None) -> pd.DataFrame:
        self.calls.append((kind, source))
        if self._require_source and source is None:
            raise ValueError("必须显式指定 source")
        frame = pd.DataFrame(self._frames.get(kind, []))
        frame.attrs["source"] = "tushare"
        return frame


def _frames(*, delist_date: str = "2015-05-01") -> dict[str, list[dict]]:
    return {
        "stock_list": [
            {"code": CODE_STOCK, "name": "贵州茅台", "list_date": pd.Timestamp("2001-08-27")}
        ],
        "delist_list": [
            {
                "code": CODE_DELISTED,
                "name": "平安银行(退市示例)",
                "list_date": pd.Timestamp("1991-04-03"),
                "delist_date": pd.Timestamp(delist_date),
            }
        ],
        "etf_list": [
            {"code": CODE_ETF, "name": "沪深300ETF", "list_date": pd.Timestamp("2012-05-28")}
        ],
        "fund_list": [
            {"code": CODE_FUND, "name": "易方达消费行业", "list_date": pd.Timestamp("2010-08-20")}
        ],
        "index_list": [
            {"code": CODE_INDEX, "name": "沪深300", "list_date": pd.Timestamp("2005-04-08")}
        ],
    }


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


def _entities(engine, metadata):  # type: ignore[no-untyped-def]
    table = metadata.tables["ref.entity"]
    with engine.connect() as connection:
        return (
            connection.execute(
                select(table.c.code, table.c.entity_type, table.c.name, table.c.entity_class)
            )
            .mappings()
            .all()
        )


def _lifecycle(engine, metadata):  # type: ignore[no-untyped-def]
    table = metadata.tables[DATASET]
    with engine.connect() as connection:
        return (
            connection.execute(
                select(
                    table.c.entity_id,
                    table.c.status,
                    table.c.start_date,
                    table.c.end_date,
                    table.c.knowledge_time,
                    table.c.version,
                ).order_by(table.c.entity_id, table.c.start_date, table.c.version)
            )
            .mappings()
            .all()
        )


def test_identity_registration_four_kinds(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    result = sync_market_registry(db, FakeHub(_frames()))
    assert (result.entities_created, result.entities_refreshed) == (5, 0)
    rows = {row["code"]: row for row in _entities(db, metadata)}
    assert set(rows) == {CODE_STOCK, CODE_DELISTED, CODE_ETF, CODE_FUND, CODE_INDEX}
    assert rows[CODE_STOCK]["entity_type"] == "equity"
    assert rows[CODE_ETF]["entity_type"] == "etf"
    assert rows[CODE_FUND]["entity_type"] == "fund"
    assert rows[CODE_INDEX]["entity_type"] == "index"
    assert rows[CODE_STOCK]["name"] == "贵州茅台" and rows[CODE_STOCK]["entity_class"] is None
    # 退市股票同样登记身份（PIT Universe 需要）
    assert rows[CODE_DELISTED]["entity_type"] == "equity"


def test_stock_lifecycle_two_rows_and_knowledge_time(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    sync_market_registry(db, FakeHub(_frames()))
    rows = _lifecycle(db, metadata)
    store = EntityStore(db)
    delisted = store.get_entity(CODE_DELISTED)
    assert delisted is not None
    statuses = {
        row["status"]: row for row in rows if row["entity_id"] == delisted.entity_id
    }
    # 退市股票：listed [1991-04-03, 2015-04-30] + delisted [2015-05-01, null]
    assert set(statuses) == {"listed", "delisted"}
    assert statuses["listed"]["start_date"] == date(1991, 4, 3)
    assert statuses["listed"]["end_date"] == date(2015, 4, 30)
    assert statuses["delisted"]["start_date"] == date(2015, 5, 1)
    assert statuses["delisted"]["end_date"] is None
    # knowledge_time 首版 = 区间起点当日 15:00 CST（07:00 UTC）稳定值
    assert statuses["listed"]["knowledge_time"] == datetime(1991, 4, 3, 7, 0)
    assert statuses["delisted"]["knowledge_time"] == datetime(2015, 5, 1, 7, 0)
    assert statuses["listed"]["version"] == 1 and statuses["delisted"]["version"] == 1
    # 在市股票只有 listed 一行
    listed_stock = store.get_entity(CODE_STOCK)
    assert listed_stock is not None
    stock_only = [row for row in rows if row["entity_id"] == listed_stock.entity_id]
    assert len(stock_only) == 1 and stock_only[0]["status"] == "listed"
    assert stock_only[0]["end_date"] is None


def test_etf_fund_listed_only_and_index_skipped(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    sync_market_registry(db, FakeHub(_frames()))
    store = EntityStore(db)
    for code in (CODE_ETF, CODE_FUND):
        entity_id = store.get_entity(code).entity_id  # type: ignore[union-attr]
        rows = [row for row in _lifecycle(db, metadata) if row["entity_id"] == entity_id]
        assert len(rows) == 1 and rows[0]["status"] == "listed" and rows[0]["end_date"] is None
    index_id = store.get_entity(CODE_INDEX).entity_id  # type: ignore[union-attr]
    assert not [row for row in _lifecycle(db, metadata) if row["entity_id"] == index_id]


def test_rerun_is_idempotent(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    hub = FakeHub(_frames())
    sync_market_registry(db, hub)
    before = len(_lifecycle(db, metadata))
    second = sync_market_registry(db, hub)
    assert second.entities_created == 0 and second.entities_refreshed == 0
    assert second.entities_unchanged == 5
    assert (second.lifecycle_written, second.lifecycle_revisions) == (0, 0)
    assert len(_lifecycle(db, metadata)) == before


def test_delist_date_revision_updates_and_retracts(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    sync_market_registry(db, FakeHub(_frames(delist_date="2015-05-01")))
    entity_id = EntityStore(db).get_entity(CODE_DELISTED).entity_id  # type: ignore[union-attr]
    sync_market_registry(db, FakeHub(_frames(delist_date="2015-06-01")))

    rows = [row for row in _lifecycle(db, metadata) if row["entity_id"] == entity_id]
    latest: dict[date, dict] = {}
    for row in rows:
        current = latest.get(row["start_date"])
        if current is None or row["version"] > current["version"]:
            latest[row["start_date"]] = row
    # listed 行 end_date 修订到 2015-05-31（version+1）
    assert latest[date(1991, 4, 3)]["end_date"] == date(2015, 5, 31)
    assert latest[date(1991, 4, 3)]["version"] == 2
    # 新退市日产生新的 delisted 行；旧 delisted 行零长度闭合（失效标记）
    assert latest[date(2015, 6, 1)]["status"] == "delisted"
    assert latest[date(2015, 5, 1)]["end_date"] == date(2015, 5, 1)
    assert latest[date(2015, 5, 1)]["version"] == 2


def test_legacy_class_cleaned_and_name_backfilled(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    store = EntityStore(db)
    legacy = store.ensure_entity(code=CODE_STOCK, entity_type="equity", name="")
    assert legacy.entity_class is None
    store.update_entity(code=CODE_STOCK, entity_class="stock")  # 模拟历史遗留非法值

    result = sync_market_registry(db, FakeHub(_frames()))
    assert result.entities_refreshed == 1 and result.legacy_cleaned == 1
    refreshed = store.get_entity(CODE_STOCK)
    assert refreshed is not None
    assert refreshed.name == "贵州茅台" and refreshed.entity_class is None
    assert refreshed.entity_id == legacy.entity_id  # 主键稳定


def test_invalid_rows_are_skipped(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    frames = _frames()
    frames["stock_list"].append({"code": "600000.SH", "name": "浦发银行", "list_date": None})
    frames["etf_list"].append(
        {
            "code": "159999.SZ",
            "name": "退市ETF示例",
            "list_date": pd.Timestamp("2015-01-05"),
            "list_status": "D",
        }
    )
    result = sync_market_registry(db, FakeHub(frames))
    assert result.lifecycle_skipped == 2  # 缺 list_date + 已退市但缺退市日期
    store = EntityStore(db)
    for code in ("600000.SH", "159999.SZ"):
        entity_id = store.get_entity(code).entity_id  # type: ignore[union-attr]
        assert not [row for row in _lifecycle(db, metadata) if row["entity_id"] == entity_id]


def test_market_registry_task_through_runtime(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    registry = TaskRegistry()
    spec = register_market_registry_task(
        registry, db, FakeHub(_frames()), source="tushare"
    )
    assert spec.job_id == MARKET_REGISTRY_JOB
    assert spec.scope == ""
    assert spec.window_provider is not None
    day = date(2026, 9, 27)
    assert spec.window_provider(datetime(2026, 9, 27, 12, 0)) == [(day, day)]

    repository = SqlMetaRepository(db)
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://")),
        engine=db,
        repository=repository,
        registry=registry,
    )
    app.sync_metadata()
    assert app.submit(registry.intent(spec, window_start=day, window_end=day)) == "created"
    assert app.run_pending() == 1
    run = repository.list_runs()[0]
    assert run.status == "succeeded" and run.rows_written > 0
    # 同日重复触发：job_key 幂等（不重复执行）
    assert app.submit(registry.intent(spec, window_start=day, window_end=day)) == "duplicate"


def test_supports_capability_gate() -> None:
    from fin_data_hub import Source
    from fin_data_hub.sources import SourceRegistry
    from fin_data_hub.sources.akshare import AkShareAdapter

    class FakeAk:
        pass

    hub = type("Hub", (), {"registry": SourceRegistry([AkShareAdapter(ak_module=FakeAk())])})()
    assert not supports_capability(hub, Source.AKSHARE.value, "reference")
    assert supports_capability(hub, Source.AKSHARE.value, "bars")


def test_sync_forwards_source_to_hub(engine) -> None:  # type: ignore[no-untyped-def]
    """回归：source 必须转发给 hub（缺省会 ValueError；曾因吞异常导致空同步假成功）。"""
    db, _metadata = engine
    hub = FakeHub(_frames(), require_source=True)
    result = sync_market_registry(db, hub, source="tushare")
    assert result.entities_created == 5
    assert hub.calls and all(src == "tushare" for _kind, src in hub.calls)
