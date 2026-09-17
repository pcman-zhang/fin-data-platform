"""访问面（TASK-3.24）：规范化读取 = PIT + 复权口径组合。

覆盖：缺省口径（字典 default）/ 显式 raw·hfq / as-of 知识锚（含重述与未来知识）
/ 非可复权字段透传 / 不支持口径报错 / 内联 SQL 与按需读取同源 / 字典 CI 校验。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.access import (
    UnsupportedAdjust,
    read,
    read_sql,
)
from fin_data_platform.derived.inputs import parse_ref, read_inputs
from fin_data_platform.dictionary import load_all, validate_directory
from fin_data_platform.storage.schema import build_metadata

AS_OF = datetime(2026, 9, 15, 12, 0)
DATASET = "cn_equity.daily_bar"
FACTOR = "cn_equity.adj_factor"


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
    metadata, _specs = build_metadata()
    metadata.create_all(engine)
    return engine, metadata


def _seed(engine, metadata, dataset: str, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    table = metadata.tables[dataset]
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)


def _known(day: int) -> datetime:
    return datetime(2026, 9, day, 12, 0)


def _bar(entity: int, day: int, close: float, known: int, version: int = 1) -> dict[str, Any]:
    return {
        "entity_id": entity,
        "trade_date": date(2026, 9, day),
        "close": close,
        "volume": 100.0 * day,
        "knowledge_time": _known(known),
        "ingest_time": _known(known),
        "provider": "tushare",
        "version": version,
    }


def _factor(entity: int, day: int, value: float, known: int, version: int = 1) -> dict[str, Any]:
    return {
        "entity_id": entity,
        "trade_date": date(2026, 9, day),
        "adj_factor": value,
        "knowledge_time": _known(known),
        "ingest_time": _known(known),
        "provider": "tushare",
        "version": version,
    }


def _seed_simple(engine, metadata) -> None:  # type: ignore[no-untyped-def]
    _seed(
        engine,
        metadata,
        DATASET,
        [
            _bar(1, 10, 10.0, 10),
            _bar(1, 11, 11.0, 11),
            # 未来知识（as_of=09-15 不可见）
            _bar(1, 12, 99.0, 20),
        ],
    )
    _seed(
        engine,
        metadata,
        FACTOR,
        [
            _factor(1, 10, 1.0, 10),
            _factor(1, 11, 2.0, 11),
            _factor(1, 11, 4.0, 20),  # 未来知识：不得参与锚点
        ],
    )


def _values(result, field: str = "close") -> list[float]:  # type: ignore[no-untyped-def]
    return [row[field] for row in result.table.to_pylist()]


# ------------------------------------------------------------------ 口径组合
def test_default_adjust_comes_from_dictionary(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_simple(db, metadata)
    result = read(db, DATASET, ["close", "volume"], as_of=AS_OF)

    assert result.meta.adjust == "qfq"
    assert result.meta.factor_dataset == FACTOR
    assert result.meta.row_count == 2
    # qfq = raw × f / anchor（anchor = as_of 可见的最新因子 2.0）
    assert _values(result) == [pytest.approx(5.0), pytest.approx(11.0)]
    # 非可复权字段透传
    assert [row["volume"] for row in result.table.to_pylist()] == [
        pytest.approx(1000.0),
        pytest.approx(1100.0),
    ]


def test_explicit_raw_and_hfq(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_simple(db, metadata)

    raw = read(db, DATASET, ["close"], as_of=AS_OF, adjust="raw")
    assert raw.meta.adjust == "raw" and raw.meta.factor_dataset is None
    assert _values(raw) == [pytest.approx(10.0), pytest.approx(11.0)]

    hfq = read(db, DATASET, ["close"], as_of=AS_OF, adjust="hfq")
    assert hfq.meta.adjust == "hfq"
    assert _values(hfq) == [pytest.approx(10.0), pytest.approx(22.0)]


def test_as_of_guard_applies_to_raw_and_anchor(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_simple(db, metadata)

    # 09-21 时未来行可见：09-11 的因子被重述为 4.0、锚点=4.0；
    # 09-12 无对应因子 → 调整值为 NULL（不静默回退为原始价）
    late = read(db, DATASET, ["close"], as_of=datetime(2026, 9, 21, 12, 0))
    values = _values(late)
    assert values[0] == pytest.approx(2.5)
    assert values[1] == pytest.approx(11.0)
    assert values[2] is None

    # 更早的知识时点：仅 09-10 一行可见，锚点=1.0
    early = read(db, DATASET, ["close"], as_of=datetime(2026, 9, 10, 13, 0))
    assert _values(early) == [pytest.approx(10.0)]


def test_restatement_version_dedup(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed(
        db,
        metadata,
        DATASET,
        [
            _bar(1, 10, 10.0, 10, version=1),
            _bar(1, 10, 12.0, 12, version=2),
        ],
    )
    _seed(db, metadata, FACTOR, [_factor(1, 10, 1.0, 10)])

    early = read(db, DATASET, ["close"], as_of=datetime(2026, 9, 11, 12, 0))
    assert _values(early) == [pytest.approx(10.0)]
    late = read(db, DATASET, ["close"], as_of=AS_OF)
    assert _values(late) == [pytest.approx(12.0)]


# ------------------------------------------------------------------ 异常
def test_unsupported_adjust_raises(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_simple(db, metadata)

    # 未声明 adjust 的数据集（cn_fund.nav 无复权）
    with pytest.raises(UnsupportedAdjust, match="未声明口径"):
        read(db, "cn_fund.nav", as_of=AS_OF, adjust="qfq")
    # 未声明的口径取值
    with pytest.raises(UnsupportedAdjust, match="口径非法"):
        read(db, DATASET, ["close"], as_of=AS_OF, adjust="dvd")
    # 未声明 adjust 的数据集：raw 仍可用
    assert read(db, DATASET, ["close"], as_of=AS_OF, adjust="raw").meta.adjust == "raw"


def test_unknown_dataset_and_field(engine) -> None:  # type: ignore[no-untyped-def]
    from fin_data_platform.access import UnknownDataset, UnknownField

    db, _metadata = engine
    with pytest.raises(UnknownDataset, match="数据集不存在"):
        read(db, "no.such_dataset", as_of=AS_OF)
    with pytest.raises(UnknownField, match="字段不存在"):
        read(db, DATASET, ["nope"], as_of=AS_OF)


# ------------------------------------------------------------------ 内联同源
def test_read_sql_matches_read(engine) -> None:  # type: ignore[no-untyped-def]
    import pandas as pd

    db, metadata = engine
    _seed_simple(db, metadata)
    specs = load_all()
    spec = specs[DATASET]

    sql, params, mode = read_sql(
        spec, ["close", "volume"], as_of=AS_OF, adjust="qfq", specs=specs, literal=True
    )
    assert mode == "qfq" and params == {}
    with db.connect() as connection:
        inline = pd.read_sql(text(sql), connection)
    direct = read(db, DATASET, ["close", "volume"], as_of=AS_OF).table.to_pandas()
    pd.testing.assert_frame_equal(inline.reset_index(drop=True), direct.reset_index(drop=True))


# ------------------------------------------------------------------ 派生输入
def test_inputs_suffix_parsing_and_default_adjust(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    _seed_simple(db, metadata)
    specs = load_all()

    assert parse_ref("cn_equity.daily_bar.close", specs) == (DATASET, "close", None)
    assert parse_ref("cn_equity.daily_bar.close@raw", specs) == (DATASET, "close", "raw")
    with pytest.raises(ValueError, match="口径非法"):
        parse_ref("cn_equity.daily_bar.close@dvd", specs)
    with pytest.raises(ValueError, match="数据集不存在"):
        parse_ref("no.such.close", specs)
    with pytest.raises(ValueError, match="字段不存在"):
        parse_ref("cn_equity.daily_bar.nope", specs)

    tables = read_inputs(
        db,
        ["cn_equity.daily_bar.close", "cn_equity.daily_bar.close@raw"],
        as_of=AS_OF,
        specs=specs,
    )
    assert list(tables) == ["cn_equity.daily_bar.close", "cn_equity.daily_bar.close@raw"]
    adjusted = tables["cn_equity.daily_bar.close"].column("close").to_pylist()
    raw = tables["cn_equity.daily_bar.close@raw"].column("close").to_pylist()
    assert adjusted == [pytest.approx(5.0), pytest.approx(11.0)]
    assert raw == [pytest.approx(10.0), pytest.approx(11.0)]


# ------------------------------------------------------------------ 字典 CI
def _write_dataset(root: Path, dataset: str, data: dict[str, Any]) -> None:
    path = root.joinpath(*dataset.split(".")).with_suffix(".yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def _base_spec(dataset: str = "cn_equity.demo") -> dict[str, Any]:
    return {
        "dataset": dataset,
        "semantic_version": 1,
        "domain": dataset.split(".")[0],
        "description": "demo",
        "pit_class": "market",
        "business_key": ["entity_id", "trade_date"],
        "physical_key": ["entity_id", "trade_date", "knowledge_time", "version"],
        "grain": "标的 × 交易日",
        "update_sla": {
            "frequency": "daily",
            "earliest_available": "T+0 18:00",
            "latest_available": "T+0 22:00",
            "tolerance": "2h",
        },
        "sources": [{"provider": "tushare", "endpoint": "daily"}],
        "coverage": {
            "universe": "A 股",
            "universe_source": "ref.entity",
            "history_start": "1990-12-19",
            "expected_dates": {"calendar": "ref.trade_calendar", "frequency": "daily"},
        },
        "storage": {
            "canonical_table": dataset,
            "read_model": f"mart.{dataset.split('.')[-1]}_v1",
            "read_model_impl": "view",
            "partition_strategy": "event_time",
            "partition_interval": "1 month",
            "retention": "all",
            "compression": {
                "after": "7 days",
                "segment_by": "entity_id",
                "order_by": "trade_date, knowledge_time, version",
            },
        },
        "quality": [],
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
                "name": "close",
                "type": "float64",
                "nullable": True,
                "description": "收盘",
                "pit_role": "none",
            },
            {
                "name": "adj_factor",
                "type": "float64",
                "nullable": True,
                "description": "因子",
                "pit_role": "none",
            },
            {
                "name": "knowledge_time",
                "type": "timestamp_tz",
                "nullable": False,
                "description": "知识时间",
                "pit_role": "knowledge_time",
            },
            {
                "name": "version",
                "type": "int64",
                "nullable": False,
                "description": "版本",
                "pit_role": "none",
            },
        ],
    }


def _adjust(**overrides: Any) -> dict[str, Any]:
    adjust = {
        "modes": ["qfq", "hfq"],
        "factor_dataset": "cn_equity.factor",
        "factor_field": "adj_factor",
        "fields": ["close"],
        "default": "qfq",
    }
    adjust.update(overrides)
    return adjust


def test_dictionary_ci_validates_adjust_block(tmp_path: Path) -> None:
    _write_dataset(tmp_path, "cn_equity.factor", _base_spec("cn_equity.factor"))
    _write_dataset(
        tmp_path,
        "cn_equity.demo",
        {**_base_spec(), "adjust": _adjust(modes=["qfq"], default="hfq")},
    )
    errors = validate_directory(tmp_path)
    assert any("adjust.default=hfq" in error for error in errors)

    _write_dataset(
        tmp_path,
        "cn_equity.demo",
        {**_base_spec(), "adjust": _adjust(factor_dataset="cn_equity.missing")},
    )
    errors = validate_directory(tmp_path)
    assert any("adjust.factor_dataset 不存在" in error for error in errors)

    _write_dataset(
        tmp_path,
        "cn_equity.demo",
        {**_base_spec(), "adjust": _adjust(factor_field="nope")},
    )
    errors = validate_directory(tmp_path)
    assert any("adjust.factor_field 不存在" in error for error in errors)

    _write_dataset(tmp_path, "cn_equity.demo", {**_base_spec(), "adjust": _adjust(fields=["nope"])})
    errors = validate_directory(tmp_path)
    assert any("adjust.fields 不存在" in error for error in errors)

    # 合法声明：无错误
    _write_dataset(tmp_path, "cn_equity.demo", {**_base_spec(), "adjust": _adjust()})
    assert validate_directory(tmp_path) == []


def test_dictionary_ci_validates_input_adjust_suffix(tmp_path: Path) -> None:
    _write_dataset(tmp_path, "cn_equity.factor", _base_spec("cn_equity.factor"))
    demo = {
        **_base_spec(),
        "adjust": _adjust(),
        "derived": [
            {
                "output": "ma",
                "algorithm_id": "ma_v1",
                "implementation": "tests.fake.ma",
                "owner": "derived-engine",
                "inputs": ["cn_equity.demo.close@dvd"],
                "description": "demo 因子",
            }
        ],
    }
    _write_dataset(tmp_path, "cn_equity.demo", demo)
    errors = validate_directory(tmp_path)
    assert any("输入口径非法" in error for error in errors)

    demo["derived"][0]["inputs"] = ["cn_equity.factor.adj_factor@qfq"]  # 因子数据集未声明
    _write_dataset(tmp_path, "cn_equity.demo", demo)
    errors = validate_directory(tmp_path)
    assert any("未声明口径 qfq" in error for error in errors)


# ------------------------------------------------------------------ 复审修复
def test_read_business_key_field_not_duplicated(engine) -> None:  # type: ignore[no-untyped-def]
    """请求业务键字段不得产生重复列（复审修复）。"""
    db, metadata = engine
    _seed_simple(db, metadata)
    result = read(db, DATASET, ["entity_id", "close"], as_of=AS_OF)
    assert result.table.column_names == ["entity_id", "trade_date", "close"]

    tables = read_inputs(
        db,
        ["cn_equity.daily_bar.entity_id"],
        as_of=AS_OF,
        specs=load_all(),
    )
    assert tables["cn_equity.daily_bar.entity_id"].column_names == [
        "entity_id",
        "trade_date",
    ]


def test_adjust_none_alias_and_meta_adjusted_fields(engine) -> None:  # type: ignore[no-untyped-def]
    """``none`` 为 ``raw`` 别名；ReadMeta 如实报告实际被复权的字段。"""
    db, metadata = engine
    _seed_simple(db, metadata)

    alias = read(db, DATASET, ["close"], as_of=AS_OF, adjust="none")
    assert alias.meta.adjust == "raw"
    assert _values(alias) == [pytest.approx(10.0), pytest.approx(11.0)]

    # 请求口径为 qfq 但字段不可复权：数值透传，元数据如实标注
    passthrough = read(db, DATASET, ["volume"], as_of=AS_OF)
    assert passthrough.meta.adjust == "qfq"
    assert passthrough.meta.adjusted_fields == ()
    assert passthrough.meta.factor_dataset is None
    assert [row["volume"] for row in passthrough.table.to_pylist()] == [
        pytest.approx(1000.0),
        pytest.approx(1100.0),
    ]

    mixed = read(db, DATASET, ["close", "volume"], as_of=AS_OF)
    assert mixed.meta.adjusted_fields == ("close",)


def test_literal_sql_with_empty_entities(engine) -> None:  # type: ignore[no-untyped-def]
    """literal=True + 空实体列表 → ``1 = 0``（与绑定路径的 0 行语义一致）。"""
    db, metadata = engine
    _seed_simple(db, metadata)
    sql, params, _mode = read_sql(
        load_all()[DATASET],
        ["close"],
        as_of=AS_OF,
        adjust="raw",
        entity_ids=[],
        literal=True,
    )
    assert "1 = 0" in sql and params == {}
    with db.connect() as connection:
        rows = connection.execute(text(sql)).fetchall()
    assert rows == []


def test_input_ref_non_adjustable_field_raises() -> None:
    specs = load_all()
    with pytest.raises(ValueError, match="不可复权"):
        parse_ref("cn_equity.daily_bar.volume@qfq", specs)
    # raw 不受限制
    assert parse_ref("cn_equity.daily_bar.volume@raw", specs) == (
        DATASET,
        "volume",
        "raw",
    )


def test_dictionary_ci_rejects_non_adjustable_input_field(tmp_path: Path) -> None:
    _write_dataset(tmp_path, "cn_equity.factor", _base_spec("cn_equity.factor"))
    demo = {
        **_base_spec(),
        "adjust": _adjust(),
        "derived": [
            {
                "output": "ma",
                "algorithm_id": "ma_v1",
                "implementation": "tests.fake.ma",
                "owner": "derived-engine",
                "inputs": ["cn_equity.demo.close@qfq", "cn_equity.demo.adj_factor@qfq"],
                "description": "demo 因子",
            }
        ],
    }
    _write_dataset(tmp_path, "cn_equity.demo", demo)
    errors = validate_directory(tmp_path)
    assert any("派生输入字段不可复权" in error for error in errors)


def test_dictionary_ci_rejects_factor_without_entity_key(tmp_path: Path) -> None:
    factor = _base_spec("cn_equity.factor")
    factor["business_key"] = ["trade_date"]
    factor["physical_key"] = ["trade_date", "knowledge_time", "version"]
    _write_dataset(tmp_path, "cn_equity.factor", factor)
    _write_dataset(tmp_path, "cn_equity.demo", {**_base_spec(), "adjust": _adjust()})
    errors = validate_directory(tmp_path)
    assert any("缺少非事件时间业务键" in error for error in errors)
