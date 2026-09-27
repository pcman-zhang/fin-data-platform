"""数据质量检查（TASK-3.5）单测：规则 / 完整性 / 时效性 / 对账 / 运行时与 API。"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    MetaData,
    Table,
    Text,
    create_engine,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.pool import StaticPool

from fin_data_platform.api.app import create_app
from fin_data_platform.api.deps import ApiContext
from fin_data_platform.control import ControlClient
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.quality import (
    DEFAULT_DATASETS,
    QUALITY_JOB,
    CheckResult,
    fetch_results,
    fetch_summary,
    register_quality_task,
    run_quality_scan,
    write_results,
)
from fin_data_platform.quality.models import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
)
from fin_data_platform.quality.reconcile import cross_source_checks
from fin_data_platform.quality.rules import plan_rules
from fin_data_platform.runtime import RuntimeApp, RuntimeConfig, SqlMetaRepository, TaskRegistry
from fin_data_platform.runtime.repository import InMemoryMetaRepository
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata

DAY = date(2026, 9, 25)
D0, D1, D2 = date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)
CODE_A, CODE_B, CODE_C = "600519.SH", "000858.SZ", "000004.SZ"
SCOPE = (CODE_A, CODE_B, CODE_C)
NOW = datetime(2026, 9, 25, 10, 0)


# ---------------------------------------------------------------- 夹具
def _demo_metadata() -> MetaData:
    """规则单测用的最小表（demo 表用代理主键，允许构造重复物理键）。"""

    metadata = MetaData()
    Table(
        "demo",
        metadata,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("entity_id", BigInteger, nullable=False),
        Column("trade_date", Date, nullable=False),
        Column("version", BigInteger, nullable=False),
        Column("close", Double),
        Column("volume", Double),
        Column("status", Text),
        Column("knowledge_time", DateTime()),
        schema="cn_equity",
    )
    Table(
        "target",
        metadata,
        Column("entity_id", BigInteger, primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("knowledge_time", DateTime()),
        schema="ref",
    )
    Table(
        "target_open",
        metadata,
        Column("entity_id", BigInteger, primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("is_open", Boolean(), nullable=False),
        Column("knowledge_time", DateTime()),
        schema="ref",
    )
    return metadata


@pytest.fixture()
def db():  # type: ignore[no-untyped-def]
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
    demo = _demo_metadata()
    demo.create_all(engine)
    return engine, metadata, demo


def _insert(engine, table, rows: list[dict]) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as connection:
        connection.execute(insert(table), rows)


# ---- 数据行助手（收敛测试数据构造的长度；字段与字典 schema 对齐）
def _demo_row(
    row_id: int,
    entity_id: int,
    day: date,
    *,
    close: float | None,
    volume: float,
    status: str,
    version: int = 1,
) -> dict:
    return {
        "id": row_id,
        "entity_id": entity_id,
        "trade_date": day,
        "version": version,
        "close": close,
        "volume": volume,
        "status": status,
    }


def _entity_row(entity_id: int, code: str, name: str, since: date) -> dict:
    return {
        "entity_id": entity_id,
        "entity_type": "equity",
        "market": "cn",
        "code": code,
        "name": name,
        "valid_from": since,
        "knowledge_time": NOW,
        "version": 1,
    }


def _market_row() -> dict:
    return {
        "exchange_id": "XSHG",
        "name": "上交所",
        "market": "cn",
        "timezone": "Asia/Shanghai",
        "currency": "CNY",
        "valid_from": date(1990, 1, 1),
        "knowledge_time": NOW,
        "version": 1,
    }


def _calendar_row(day: date) -> dict:
    return {
        "exchange_id": "XSHG",
        "trade_date": day,
        "is_open": True,
        "knowledge_time": NOW,
        "version": 1,
    }


def _lifecycle_row(entity_id: int, status: str, start: date, end: date | None) -> dict:
    return {
        "entity_id": entity_id,
        "status": status,
        "start_date": start,
        "end_date": end,
        "knowledge_time": NOW,
        "version": 1,
        "provider": "tushare",
    }


def _bar_row(
    entity_id: int,
    day: date,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": None,
        "knowledge_time": NOW,
        "ingest_time": NOW,
        "version": 1,
        "provider": "tushare",
    }


def _factor_row(entity_id: int, day: date) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "adj_factor": 1.0,
        "knowledge_time": NOW,
        "ingest_time": NOW,
        "version": 1,
        "provider": "tushare",
    }


def _status_row(entity_id: int, day: date, *, suspended: bool = False) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "is_suspended": suspended,
        "is_st": False,
        "knowledge_time": NOW,
        "ingest_time": NOW,
        "version": 1,
        "provider": "tushare",
    }


def _spec(quality: list[dict]) -> DatasetSpec:
    """最小数据集定义（规则单测；覆盖来自字典模型而非目录校验）。"""
    return DatasetSpec.model_validate(
        {
            "dataset": "cn_equity.demo",
            "semantic_version": 1,
            "domain": "cn_equity",
            "description": "demo",
            "pit_class": "market",
            "business_key": ["entity_id", "trade_date"],
            "physical_key": ["entity_id", "trade_date", "version"],
            "grain": "标的 × 交易日",
            "update_sla": {
                "frequency": "daily",
                "earliest_available": "T+0 18:00",
                "latest_available": "T+0 22:00",
                "tolerance": "2h",
            },
            "sources": [{"provider": "tushare", "endpoint": "daily"}],
            "coverage": {
                "universe": "demo",
                "universe_source": "self",
                "history_start": "2020-01-01",
                "expected_dates": {
                    "calendar": "ref.trade_calendar",
                    "frequency": "daily",
                },
            },
            "storage": {
                "canonical_table": "cn_equity.demo",
                "read_model": "mart.demo_v1",
                "read_model_impl": "view",
                "partition_strategy": "none",
                "partition_interval": "none",
                "retention": "all",
            },
            "quality": quality,
            "lineage": {"upstream": [], "transform": "raw"},
            "mappings": [],
            "fields": [
                {
                    "name": "entity_id",
                    "type": "int64",
                    "nullable": False,
                    "description": "实体",
                    "pit_role": "none",
                },
                {
                    "name": "trade_date",
                    "type": "date",
                    "nullable": False,
                    "description": "交易日",
                    "pit_role": "event_time",
                },
                {
                    "name": "version",
                    "type": "int64",
                    "nullable": False,
                    "description": "版本",
                    "pit_role": "none",
                },
                {
                    "name": "close",
                    "type": "float64",
                    "nullable": True,
                    "description": "收盘",
                    "pit_role": "none",
                },
                {
                    "name": "volume",
                    "type": "float64",
                    "nullable": True,
                    "description": "成交量",
                    "pit_role": "none",
                },
                {
                    "name": "status",
                    "type": "enum",
                    "enum": ["listed", "delisted"],
                    "nullable": True,
                    "description": "状态",
                    "pit_role": "none",
                },
            ],
        }
    )


def _violations(engine, planned) -> int:  # type: ignore[no-untyped-def]
    assert planned.query is not None
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(planned.query.subquery())
            ).scalar_one()
        )


# ---------------------------------------------------------------- 规则（SQL 规划）
def test_rule_plans_and_violations(db) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, demo = db
    table = demo.tables["cn_equity.demo"]
    rows = [
        _demo_row(1, 1, D0, close=10.0, volume=100.0, status="listed"),
        _demo_row(2, 1, D1, close=100.0, volume=110.0, status="listed"),
        _demo_row(3, 2, D0, close=None, volume=5.0, status="listed"),
        _demo_row(4, 2, D1, close=8.0, volume=-3.0, status="listed"),
        _demo_row(5, 3, D0, close=6.0, volume=6.0, status="unknown"),
        _demo_row(6, 3, D1, close=-1.0, volume=6.0, status="listed"),
        _demo_row(7, 4, D0, close=5.0, volume=5.0, status="listed"),
        _demo_row(8, 4, D0, close=5.5, volume=5.0, status="listed"),
        _demo_row(9, 5, D0, close=4.0, volume=4.0, status="listed"),
        _demo_row(10, 6, D0, close=3.0, volume=3.0, status="listed"),
    ]
    _insert(engine, table, rows)
    _insert(
        engine,
        demo.tables["ref.target"],
        [
            {"entity_id": 1, "trade_date": D0},
            {"entity_id": 1, "trade_date": D1},
            {"entity_id": 2, "trade_date": D0},
            {"entity_id": 2, "trade_date": D1},
            {"entity_id": 3, "trade_date": D0},
            {"entity_id": 3, "trade_date": D1},
            {"entity_id": 4, "trade_date": D0},
            {"entity_id": 6, "trade_date": D0},
        ],
    )
    _insert(
        engine,
        demo.tables["ref.target_open"],
        [
            {"entity_id": 1, "trade_date": D0, "is_open": True},
            {"entity_id": 1, "trade_date": D1, "is_open": True},
            {"entity_id": 2, "trade_date": D0, "is_open": True},
            {"entity_id": 2, "trade_date": D1, "is_open": True},
            {"entity_id": 3, "trade_date": D0, "is_open": True},
            {"entity_id": 3, "trade_date": D1, "is_open": True},
            {"entity_id": 4, "trade_date": D0, "is_open": True},
            {"entity_id": 5, "trade_date": D0, "is_open": True},
            {"entity_id": 6, "trade_date": D0, "is_open": False},
        ],
    )
    spec = _spec(
        [
            {"rule": "unique", "keys": ["entity_id", "trade_date", "version"], "severity": "error"},
            {"rule": "not_null", "fields": ["close"], "severity": "error"},
            {"rule": "range", "field": "volume", "min": 0, "severity": "error"},
            {
                "rule": "enum",
                "field": "status",
                "values": ["listed", "delisted"],
                "severity": "warn",
            },
            {"rule": "expression", "expr": "close >= 0", "severity": "error"},
            {"rule": "jump", "field": "close", "max_ratio": 2.0, "severity": "warn"},
            {"rule": "reconcile", "against": "ref.target", "severity": "warn"},
            {"rule": "reconcile", "against": "ref.target_open", "severity": "warn"},
            {"rule": "reconcile", "against": "raw.missing", "severity": "warn"},
        ]
    )
    plans = plan_rules(spec, demo.tables["cn_equity.demo"], metadata=demo, window=(D0, D1))
    unique, not_null, range_, enum, expression, jump, target, target_open, missing = plans
    assert _violations(engine, unique) == 2  # 重复键的两行
    assert _violations(engine, not_null) == 1  # close 为 NULL
    assert _violations(engine, range_) == 1  # volume=-3
    assert _violations(engine, enum) == 1  # status=unknown
    assert _violations(engine, expression) == 1  # close=-1（NULL 行不判违规）
    assert _violations(engine, jump) == 1  # 10 → 100（ratio 9 > 2）
    assert _violations(engine, target) == 1  # entity 5 不在 ref.target
    assert _violations(engine, target_open) == 1  # entity 6 当日 is_open=false
    # 共享键 = 业务键 ∩ 目标表（knowledge_time 等平台管理列不参与对账）
    assert target.note == "共享键: entity_id, trade_date"
    assert target_open.note == "共享键: entity_id, trade_date"
    assert missing.skip_reason is not None  # raw.missing 未落地 → 跳过
    # 窗口过滤：D0 当日 volume=-3 之外无其他范围违规
    only_d0 = plan_rules(
        spec, demo.tables["cn_equity.demo"], metadata=demo, window=(D0, D0)
    )[2]
    assert _violations(engine, only_d0) == 0


# ---------------------------------------------------------------- 端到端扫描（真字典 schema）
def _seed_market(db, *, suspended_missing: bool = False) -> None:  # type: ignore[no-untyped-def]
    engine, metadata, _ = db
    entity = metadata.tables["ref.entity"]
    market = metadata.tables["ref.market"]
    calendar = metadata.tables["ref.trade_calendar"]
    lifecycle = metadata.tables["cn_equity.listing_lifecycle"]
    bars = metadata.tables["cn_equity.daily_bar"]
    factors = metadata.tables["cn_equity.adj_factor"]
    status = metadata.tables["cn_equity.daily_status"]

    _insert(
        engine,
        entity,
        [
            _entity_row(1, CODE_A, "贵州茅台", date(2001, 8, 27)),
            _entity_row(2, CODE_B, "五粮液", date(2001, 1, 1)),
            _entity_row(3, CODE_C, "国华退", date(1991, 4, 3)),
        ],
    )
    _insert(engine, market, [_market_row()])
    _insert(engine, calendar, [_calendar_row(day) for day in (D0, D1, D2)])
    _insert(
        engine,
        lifecycle,
        [
            _lifecycle_row(1, "listed", date(2001, 8, 27), None),
            _lifecycle_row(2, "listed", date(2001, 1, 1), None),
            _lifecycle_row(3, "listed", date(1991, 4, 3), date(2015, 4, 30)),
            _lifecycle_row(3, "delisted", date(2015, 5, 1), None),
        ],
    )
    _insert(
        engine,
        bars,
        [
            _bar_row(1, D0, 100.0, 110.0, 95.0, 100.0, 500.0),
            _bar_row(1, D1, 1700.0, 1750.0, 1690.0, 1700.0, -5.0),
            _bar_row(1, D2, 1650.0, 1600.0, 1680.0, 1650.0, 800.0),
            _bar_row(2, D0, 1000.0, 1010.0, 990.0, 1000.0, 120.0),
            _bar_row(2, D1, 1010.0, 1020.0, 1000.0, 1010.0, 130.0),
        ],
    )
    _insert(
        engine,
        factors,
        [
            _factor_row(1, D0),
            _factor_row(1, D1),
            _factor_row(1, D2),
            _factor_row(2, D0),
            _factor_row(2, D1),
        ],
    )
    status_rows = [
        _status_row(1, D0),
        _status_row(1, D1),
        _status_row(1, D2),
        _status_row(2, D0),
        _status_row(2, D1),
    ]
    if suspended_missing:
        status_rows.append(_status_row(2, D2, suspended=True))
    _insert(engine, status, status_rows)


def test_scan_end_to_end_rules_and_coverage(db) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, _demo = db
    _seed_market(db)
    result = run_quality_scan(
        engine,
        datasets=list(DEFAULT_DATASETS),
        window_end=DAY,
        lookback_days=3,
        codes=SCOPE,
        reconcile_codes=(),
        hub=None,
    )
    by_key = {(item.dataset, item.check_id): item for item in result.results}

    completeness = by_key[("cn_equity.daily_bar", "completeness")]
    assert completeness.status == STATUS_FAILED
    assert completeness.violations == 1
    assert completeness.metrics["ratio"] == pytest.approx(5 / 6, rel=1e-6)
    assert "trade_date=2026-09-25" in completeness.samples[0]

    freshness = by_key[("cn_equity.daily_bar", "freshness")]
    assert freshness.status == STATUS_PASSED and freshness.metrics["lag_days"] == 0

    assert by_key[("cn_equity.daily_bar", "rule:range#3")].violations == 1
    assert by_key[("cn_equity.daily_bar", "rule:expression#4")].violations == 1
    assert by_key[("cn_equity.daily_bar", "rule:jump#5")].violations == 1
    assert by_key[("cn_equity.daily_bar", "reconcile:raw.tushare_daily#6")].status == STATUS_SKIPPED
    assert by_key[("cn_equity.daily_bar", "reconcile:ref.entity#7")].status == STATUS_PASSED

    # 区间数据集（在市覆盖）与引用数据集（静态 universe）完整性
    lifecycle = by_key[("cn_equity.listing_lifecycle", "completeness")]
    assert lifecycle.status == STATUS_PASSED and lifecycle.violations == 0
    calendar = by_key[("ref.trade_calendar", "completeness")]
    assert calendar.status == STATUS_PASSED

    # 未配置跨源样本 → 两项 skipped
    cross = [item for item in result.results if item.family == "cross_source"]
    assert len(cross) == 2 and all(item.status == STATUS_SKIPPED for item in cross)
    assert result.counts["failed"] >= 1


def test_scan_suspension_aware_completeness(db) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, _demo = db
    _seed_market(db, suspended_missing=True)
    result = run_quality_scan(
        engine,
        datasets=list(DEFAULT_DATASETS),
        window_end=DAY,
        lookback_days=3,
        codes=SCOPE,
        reconcile_codes=(),
        hub=None,
    )
    by_key = {(item.dataset, item.check_id): item for item in result.results}
    assert by_key[("cn_equity.daily_bar", "completeness")].status == STATUS_PASSED
    assert by_key[("cn_equity.daily_status", "completeness")].status == STATUS_PASSED


def test_scan_unresolved_scope_is_skipped(db) -> None:  # type: ignore[no-untyped-def]
    """codes 未匹配任何实体：完整性报「范围未解析」，不退化为全市场期望。"""
    engine, _metadata, _demo = db
    _seed_market(db)
    result = run_quality_scan(
        engine,
        datasets=["cn_equity.daily_bar"],
        window_end=DAY,
        lookback_days=3,
        codes=("999999.SZ",),
        reconcile_codes=(),
        hub=None,
    )
    completeness = next(item for item in result.results if item.check_id == "completeness")
    assert completeness.status == STATUS_SKIPPED
    assert "未解析" in completeness.message


def test_store_prefers_latest_run_per_day(db) -> None:  # type: ignore[no-untyped-def]
    """同日多次运行：报告以最新一次运行为准（重跑不重复计入）。"""
    engine, _metadata, _demo = db

    def _result(message: str, status: str) -> CheckResult:
        return CheckResult(
            dataset="cn_equity.daily_bar",
            check_id="freshness",
            family="freshness",
            severity="warn",
            status=status,
            window_start=D0,
            window_end=D2,
            message=message,
        )

    write_results(engine, run_id=1, results=[_result("旧运行", STATUS_FAILED)])
    write_results(engine, run_id=2, results=[_result("新运行", STATUS_PASSED)])
    total, items = fetch_results(engine, day=D2)
    assert total == 1 and items[0]["message"] == "新运行"
    summary = fetch_summary(engine, day=D2)
    assert summary["datasets"][0]["passed"] == 1
    assert summary["datasets"][0]["failed"] == 0


@pytest.fixture(scope="module")
def _passing_hub():  # type: ignore[no-untyped-def]
    class FakeHub:
        def get_bars(self, codes, *, start, end, adjust=None, source=None, **kwargs):  # type: ignore[no-untyped-def]
            base = 1700.0 if str(source) == "tushare" else 1700.005
            return pd.DataFrame({"code": [CODE_A, CODE_A], "date": [D1, D2], "close": [base, base]})

        def get_adjust_factors(self, codes, *, start, end, source=None, **kwargs):  # type: ignore[no-untyped-def]
            values = [1.0, 1.0]
            return pd.DataFrame({"code": [CODE_A, CODE_A], "date": [D1, D2], "adj_factor": values})

    return FakeHub()


def test_cross_source_checks_pass_and_fail(db, _passing_hub) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, _demo = db
    _seed_market(db)

    passed = run_quality_scan(
        engine,
        datasets=[],
        window_end=DAY,
        lookback_days=3,
        reconcile_codes=(CODE_A,),
        hub=_passing_hub,
    )
    statuses = {item.check_id: item.status for item in passed.results}
    assert statuses["cross_source:close"] == STATUS_PASSED
    assert statuses["cross_source:factor"] == STATUS_PASSED
    assert passed.results[0].metrics["overlap_rows"] == 2

    class DriftingHub:
        def get_bars(self, codes, *, start, end, adjust=None, source=None, **kwargs):  # type: ignore[no-untyped-def]
            base = 1700.0 if str(source) == "tushare" else 1700.5
            return pd.DataFrame({"code": [CODE_A, CODE_A], "date": [D1, D2], "close": [base, base]})

        def get_adjust_factors(self, codes, *, start, end, source=None, **kwargs):  # type: ignore[no-untyped-def]
            values = [1.0, 1.0] if str(source) == "tushare" else [0.5, 1.0]
            return pd.DataFrame({"code": [CODE_A, CODE_A], "date": [D1, D2], "adj_factor": values})

    failed = run_quality_scan(
        engine,
        datasets=[],
        window_end=DAY,
        lookback_days=3,
        reconcile_codes=(CODE_A,),
        hub=DriftingHub(),
    )
    statuses = {item.check_id: item.status for item in failed.results}
    assert statuses["cross_source:close"] == STATUS_FAILED
    assert statuses["cross_source:factor"] == STATUS_FAILED
    close = next(item for item in failed.results if item.check_id == "cross_source:close")
    assert close.samples and close.metrics["max_abs_diff"] > close.metrics["tolerance"]

    class NanHub:
        """对照源含缺失值：NaN 不得静默通过（计为违规）。"""

        def get_bars(self, codes, *, start, end, adjust=None, source=None, **kwargs):  # type: ignore[no-untyped-def]
            close = 1700.0 if str(source) == "tushare" else float("nan")
            return pd.DataFrame(
                {"code": [CODE_A, CODE_A], "date": [D1, D2], "close": [close, close]}
            )

        def get_adjust_factors(self, codes, *, start, end, source=None, **kwargs):  # type: ignore[no-untyped-def]
            values = [1.0, 1.0] if str(source) == "tushare" else [float("nan")] * 2
            return pd.DataFrame({"code": [CODE_A, CODE_A], "date": [D1, D2], "adj_factor": values})

    invalid = run_quality_scan(
        engine,
        datasets=[],
        window_end=DAY,
        lookback_days=3,
        reconcile_codes=(CODE_A,),
        hub=NanHub(),
    )
    statuses = {item.check_id: item.status for item in invalid.results}
    assert statuses["cross_source:close"] == STATUS_FAILED
    assert statuses["cross_source:factor"] == STATUS_FAILED
    close = next(item for item in invalid.results if item.check_id == "cross_source:close")
    assert close.metrics["invalid_rows"] == 2
    assert close.metrics["max_abs_diff"] is None


def test_cross_source_factor_window_passthrough() -> None:
    """复权因子对账使用独立（更长）窗口：起点参数透传给取数。"""
    calls: list[tuple[str, str]] = []

    class RecordingHub:
        def get_bars(self, codes, *, start, end, adjust=None, source=None, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(("bars", start))
            return pd.DataFrame({"code": [CODE_A], "date": [D2], "close": [1700.0]})

        def get_adjust_factors(self, codes, *, start, end, source=None, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(("factor", start))
            return pd.DataFrame({"code": [CODE_A], "date": [D2], "adj_factor": [1.0]})

    results = cross_source_checks(
        RecordingHub(),
        codes=(CODE_A,),
        window_start=D1,
        window_end=D2,
        factor_window_start=date(2025, 9, 25),
    )
    assert len(results) == 2
    assert {start for kind, start in calls if kind == "bars"} == {D1.isoformat()}
    assert {start for kind, start in calls if kind == "factor"} == {"2025-09-25"}


# ---------------------------------------------------------------- 运行时 / 控制面 / API
def test_quality_task_end_to_end(db) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, _demo = db
    _seed_market(db)
    registry = TaskRegistry()
    spec = register_quality_task(
        registry,
        engine,
        datasets=list(DEFAULT_DATASETS),
        lookback_days=3,
        codes=SCOPE,
        reconcile_codes=(),
        schedule=None,
    )
    assert spec.window_provider(datetime(2026, 9, 25, 12, 0)) == [(DAY, DAY)]
    # 16:30 CST（08:30 UTC）前：最近已收盘交易日 = 前一日
    assert spec.window_provider(datetime(2026, 9, 25, 3, 0)) == [(D1, D1)]
    repository = SqlMetaRepository(engine)
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://")),
        engine=engine,
        repository=repository,
        registry=registry,
    )
    app.sync_metadata()
    assert app.submit(registry.intent(spec, window_start=DAY, window_end=DAY)) == "created"
    assert app.run_pending() == 1
    run = repository.list_runs()[0]
    assert run.status == "succeeded" and run.rows_written > 0

    total, items = fetch_results(engine, day=DAY)
    assert total == run.rows_written
    assert {item["dataset"] for item in items} == set(DEFAULT_DATASETS)
    summary = fetch_summary(engine, day=DAY)
    bars = next(item for item in summary["datasets"] if item["dataset"] == "cn_equity.daily_bar")
    assert bars["failed"] >= 1 and bars["coverage_ratio"] is not None
    # 同日重复触发：job_key 幂等
    assert app.submit(registry.intent(spec, window_start=DAY, window_end=DAY)) == "duplicate"


def test_control_trigger_allows_quality(db) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, _demo = db
    registry = TaskRegistry()
    register_quality_task(registry, engine, lookback_days=3)
    repository = SqlMetaRepository(engine)
    app = RuntimeApp(
        RuntimeConfig(storage=StorageConfig(write_dsn="sqlite://")),
        engine=engine,
        repository=repository,
        registry=registry,
    )
    app.sync_metadata()
    client = ControlClient(
        engine,
        specs=load_all(),
        meta=repository,
        algorithms=InMemoryAlgorithmStore(),
    )
    run = client.trigger(QUALITY_JOB)
    assert run.created and run.job_id == QUALITY_JOB and run.kind == "quality"


def test_api_quality_summary_and_results(db) -> None:  # type: ignore[no-untyped-def]
    engine, _metadata, _demo = db
    results = [
        CheckResult(
            dataset="cn_equity.daily_bar",
            check_id="completeness",
            family="completeness",
            severity="error",
            status=STATUS_FAILED,
            window_start=D0,
            window_end=D2,
            rows_checked=6,
            violations=1,
            samples=("entity_id=2, trade_date=2026-09-25",),
            metrics={"ratio": 0.8333, "missing": 1},
            message="覆盖率 83.33%（期望 6，缺失 1）",
        ),
        CheckResult(
            dataset="cn_equity.daily_bar",
            check_id="freshness",
            family="freshness",
            severity="warn",
            status=STATUS_PASSED,
            window_start=D0,
            window_end=D2,
            metrics={"lag_days": 0},
            message="最新数据 2026-09-25，期望 2026-09-25（滞后 0 个交易日）",
        ),
        CheckResult(
            dataset="cn_equity.daily_bar",
            check_id="rule:jump#5",
            family="rule",
            severity="warn",
            status=STATUS_FAILED,
            window_start=D0,
            window_end=D2,
            violations=2,
            samples=("entity_id=1, trade_date=2026-09-24",),
            message="违规 2 条（max_ratio=5.0）",
        ),
    ]
    write_results(engine, run_id=7, results=results)
    context = ApiContext(
        config=StorageConfig(write_dsn="sqlite://"),
        writer_engine=engine,
        read_engine=engine,
        meta=InMemoryMetaRepository(),
        algorithms=InMemoryAlgorithmStore(),
        registry=None,  # type: ignore[arg-type]
        specs=load_all(),
    )
    client = TestClient(create_app(context, web_dist=None))

    summary = client.get("/v1/quality/summary", params={"date": D2.isoformat()})
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["day"] == D2.isoformat()
    item = payload["datasets"][0]
    assert item["dataset"] == "cn_equity.daily_bar"
    # 告警 = 失败/异常且 warn 级（passed 的 warn 级检查不计入）
    assert item["failed"] == 2 and item["warnings"] == 1
    assert item["coverage_ratio"] == pytest.approx(0.8333)
    assert item["freshness_lag_days"] == 0

    detail = client.get(
        "/v1/quality/results",
        params={"date": D2.isoformat(), "dataset": "cn_equity.daily_bar"},
    )
    assert detail.status_code == 200
    page = detail.json()
    assert page["total"] == 3 and len(page["items"]) == 3
    assert page["items"][0]["samples"] == ["entity_id=2, trade_date=2026-09-25"]
    # 无效过滤值 → 422
    assert client.get("/v1/quality/results", params={"status": "bad"}).status_code == 422


def test_cross_source_common_anchor_and_dropped_rows(db) -> None:  # type: ignore[no-untyped-def]
    """单边晚发布：按共同末值归一化（无假失败）；单边缺失计入 dropped_rows。"""
    engine, _metadata, _demo = db
    _seed_market(db)

    class AnchorHub:
        def get_bars(self, codes, *, start, end, adjust=None, source=None, **kwargs):  # type: ignore[no-untyped-def]
            return pd.DataFrame(
                {"code": [CODE_A, CODE_A], "date": [D1, D2], "close": [1700.0, 1700.0]}
            )

        def get_adjust_factors(self, codes, *, start, end, source=None, **kwargs):  # type: ignore[no-untyped-def]
            if str(source) == "tushare":
                return pd.DataFrame(
                    {"code": [CODE_A, CODE_A], "date": [D1, D2], "adj_factor": [1.0, 2.0]}
                )
            return pd.DataFrame({"code": [CODE_A], "date": [D1], "adj_factor": [1.0]})

    anchored = run_quality_scan(
        engine,
        datasets=[],
        window_end=DAY,
        lookback_days=3,
        reconcile_codes=(CODE_A,),
        hub=AnchorHub(),
    )
    factor = next(item for item in anchored.results if item.check_id == "cross_source:factor")
    assert factor.status == STATUS_PASSED
    assert factor.metrics["rows_a"] == 2 and factor.metrics["rows_b"] == 1
    assert factor.metrics["dropped_rows"] == 1


def test_scan_end_clamps_to_last_closed() -> None:
    """扫描终点不晚于最近已收盘交易日（盘中触发不误判当日数据）。"""
    from fin_data_platform.quality.tasks import _scan_end

    class FakeCalendar:
        def last_closed(self, now: datetime) -> date:
            return date(2026, 9, 24)

    moment = datetime(2026, 9, 25, 3, 0)
    assert _scan_end(FakeCalendar(), date(2026, 9, 25), moment) == date(2026, 9, 24)
    assert _scan_end(FakeCalendar(), date(2026, 9, 24), moment) == date(2026, 9, 24)


def test_scan_isolates_check_errors(db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """单条检查异常记 error 结果，不中断整轮扫描。"""
    engine, _metadata, _demo = db
    _seed_market(db)
    from fin_data_platform.quality import runner as runner_mod

    original = runner_mod._execute_rule

    def flaky(connection, planned, **kwargs):  # type: ignore[no-untyped-def]
        if planned.check_id == "rule:unique#1":
            raise RuntimeError("boom")
        return original(connection, planned, **kwargs)

    monkeypatch.setattr(runner_mod, "_execute_rule", flaky)
    result = run_quality_scan(
        engine,
        datasets=["cn_equity.daily_bar"],
        window_end=DAY,
        lookback_days=3,
        codes=SCOPE,
        reconcile_codes=(),
        hub=None,
    )
    errors = [item for item in result.results if item.status == STATUS_ERROR]
    assert errors and errors[0].check_id == "rule:unique#1"
    assert "boom" in errors[0].message
    assert any(item.status == STATUS_PASSED for item in result.results)


def test_rule_checks_use_latest_version(db) -> None:  # type: ignore[no-untyped-def]
    """值检查按业务键最新版本执行：被修正的历史值不再计违规（与读层一致）。"""
    engine, _metadata, demo = db
    table = demo.tables["cn_equity.demo"]
    _insert(
        engine,
        table,
        [
            _demo_row(1, 1, D0, close=-1.0, volume=1.0, status="listed", version=1),
            _demo_row(2, 1, D0, close=5.0, volume=1.0, status="listed", version=2),
        ],
    )
    spec = _spec([{"rule": "expression", "expr": "close >= 0", "severity": "error"}])
    plan = plan_rules(spec, table, metadata=demo, window=(D0, D0))[0]
    assert _violations(engine, plan) == 0


def test_universe_scd2_dedupe(db) -> None:  # type: ignore[no-untyped-def]
    """SCD2 期望集合按键去重：同键多版本不重复计入期望。"""
    engine, metadata, _demo = db
    _seed_market(db)
    _insert(
        engine,
        metadata.tables["ref.entity"],
        [
            {
                "entity_id": 1,
                "entity_type": "equity",
                "market": "cn",
                "code": CODE_A,
                "name": "贵州茅台",
                "valid_from": date(2001, 8, 27),
                "knowledge_time": NOW,
                "version": 2,
            }
        ],
    )
    result = run_quality_scan(
        engine,
        datasets=["cn_equity.listing_lifecycle"],
        window_end=DAY,
        lookback_days=3,
        codes=SCOPE,
        reconcile_codes=(),
        hub=None,
    )
    completeness = next(item for item in result.results if item.check_id == "completeness")
    # ref.entity 为静态期望集合（全部身份）：3 实体 × 3 交易日 = 9；
    # 未按键去重时 entity 1 的两个版本会重复计入（= 12）
    assert completeness.metrics["expected_pairs"] == 9


def test_universe_interval_winner_semantics(db) -> None:  # type: ignore[no-untyped-def]
    """区间期望集合取获胜区间：未闭合的旧区间不造成幻影在市。"""
    engine, metadata, _demo = db
    _seed_market(db)
    lifecycle = metadata.tables["cn_equity.listing_lifecycle"]
    # entity 3：存在未闭合的上市区间（version 2），但其 start_date 早于退市终态区间
    _insert(
        engine,
        lifecycle,
        [
            {
                "entity_id": 3,
                "status": "listed",
                "start_date": date(1991, 4, 3),
                "end_date": None,
                "knowledge_time": NOW,
                "version": 2,
                "provider": "tushare",
            }
        ],
    )
    result = run_quality_scan(
        engine,
        datasets=["cn_equity.daily_bar"],
        window_end=DAY,
        lookback_days=3,
        codes=SCOPE,
        reconcile_codes=(),
        hub=None,
    )
    completeness = next(item for item in result.results if item.check_id == "completeness")
    # 获胜区间 = 退市终态（start_date 更大）→ entity 3 不计入在市；期望 = 2 标的 × 3 日
    assert completeness.metrics["expected_pairs"] == 6
