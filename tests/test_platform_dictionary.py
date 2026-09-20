"""数据字典（TASK-3.2 / doc-11）加载、校验与 CI 一致性测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fin_data_platform.derived.inputs import parse_ref
from fin_data_platform.dictionary import (
    DEFAULT_ROOT,
    export_schema,
    load_all,
    load_file,
    schema_path,
    validate_directory,
)

#: v0 参考/基础信息类接口（bespoke 映射，无 response spec block）
_REFERENCE_ENDPOINTS = frozenset(
    {
        "stock_basic",
        "fund_basic",
        "etf_basic",
        "index_basic",
        "index_classify",
        "index_member_all",
    }
)


def _write(root: Path, dataset: str, data: dict) -> Path:
    path = root.joinpath(*dataset.split(".")).with_suffix(".yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _base(dataset: str = "cn_equity.demo", **overrides: object) -> dict:
    data: dict = {
        "dataset": dataset,
        "semantic_version": 1,
        "domain": dataset.split(".")[0],
        "description": "demo",
        "pit_class": "market",
        "business_key": ["entity_id", "trade_date"],
        "physical_key": [
            "entity_id",
            "trade_date",
            "knowledge_time",
            "version",
        ],
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
            "universe_source": "ref.entity",
            "history_start": "2020-01-01",
            "expected_dates": {"calendar": "ref.trade_calendar", "frequency": "daily"},
        },
        "storage": {
            "canonical_table": dataset,
            "read_model": "mart.demo_v1",
            "read_model_impl": "view",
            "partition_strategy": "event_time",
            "partition_interval": "1 month",
            "retention": "all",
            "compression": {
                "after": "7 days",
                "segment_by": "entity_id",
                "order_by": "trade_date",
            },
        },
        "quality": [
            {
                "rule": "unique",
                "keys": ["entity_id", "trade_date", "knowledge_time", "version"],
                "severity": "error",
            }
        ],
        "lineage": {"upstream": [], "transform": "raw"},
        "mappings": [
            {"provider": "tushare", "endpoint": "daily", "fields": {"close": "close"}}
        ],
        "fields": [
            {
                "name": "entity_id",
                "type": "int64",
                "nullable": False,
                "description": "d",
                "pit_role": "none",
            },
            {
                "name": "trade_date",
                "type": "date",
                "nullable": False,
                "description": "d",
                "pit_role": "event_time",
            },
            {
                "name": "close",
                "type": "float64",
                "nullable": True,
                "description": "d",
                "pit_role": "none",
            },
            {
                "name": "knowledge_time",
                "type": "timestamp_tz",
                "nullable": False,
                "description": "d",
                "pit_role": "knowledge_time",
            },
            {
                "name": "version",
                "type": "int64",
                "nullable": False,
                "description": "d",
                "pit_role": "none",
            },
        ],
    }
    data.update(overrides)
    return data


def test_first_batch_validates_clean() -> None:
    assert validate_directory() == []


def test_first_batch_entries_and_keys() -> None:
    specs = load_all()
    assert {
        "cn_equity.daily_bar",
        "cn_equity.adj_factor",
        "cn_equity.index_weight",
        "cn_equity.index_member",
        "cn_equity.financials.balance_sheet",
        "cn_equity.market_events.namechange",
        "cn_fund.nav",
    } <= set(specs)
    for dataset, spec in specs.items():
        assert set(spec.business_key) <= set(spec.physical_key), dataset
        assert spec.semantic_version >= 1


def test_derived_dependencies_traceable() -> None:
    """派生输入须可追溯（数据集/字段/口径后缀均合法）；shipped 字典仅登记真正的计算（ma20）。"""
    specs = load_all()
    for dataset, spec in specs.items():
        for entry in spec.derived or []:
            assert entry.algorithm_id, dataset
            for ref in entry.inputs:
                parse_ref(ref, specs)  # 解析失败即引用非法（含口径后缀校验）
    assert "cn_equity.daily_bar" in specs
    # shipped 字典登记真正的计算（ma20/adx）；复权组合不登记（doc-5）
    outputs = {entry.output for entry in (specs["cn_equity.daily_bar"].derived or [])}
    assert outputs == {"ma20", "adx"}


def test_schema_file_matches_models() -> None:
    expected = export_schema()
    on_disk = yaml.safe_load(schema_path().read_text(encoding="utf-8"))
    assert on_disk == expected


def test_expression_and_mapping_are_checked(tmp_path: Path) -> None:
    data = _base(
        quality=[{"rule": "expression", "expr": "close > missing_field"}],
        mappings=[
            {"provider": "tushare", "endpoint": "daily", "fields": {"nope": "x"}}
        ],
    )
    _write(tmp_path, data["dataset"], data)
    errors = validate_directory(tmp_path)
    assert any("expression 引用不存在字段 missing_field" in e for e in errors)
    assert any("未知字段 nope" in e for e in errors)


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda d: d["fields"][0].update(name="wind_code"), "供应商品牌前缀"),
        (lambda d: d["fields"].append({
            "name": "amount",
            "type": "decimal",
            "nullable": True,
            "description": "d",
            "pit_role": "none",
        }), "必须提供 precision/scale"),
        (
            lambda d: d.update(physical_key=["entity_id", "trade_date"]),
            "physical_key 缺少",
        ),
        (
            lambda d: d.update(
                derived=[
                    {
                        "output": "x",
                        "algorithm_id": "x_v1",
                        "implementation": "pkg.mod.fn",
                        "owner": "o",
                        "inputs": ["cn_equity.demo.missing"],
                        "description": "d",
                    }
                ]
            ),
            "derived 输入字段不存在",
        ),
    ],
)
def test_semantic_rule_violations(tmp_path: Path, mutate, fragment: str) -> None:
    data = _base()
    mutate(data)
    _write(tmp_path, data["dataset"], data)
    errors = validate_directory(tmp_path)
    assert any(fragment in error for error in errors), errors


def test_path_dataset_mismatch(tmp_path: Path) -> None:
    data = _base("cn_equity.demo")
    _write(tmp_path, "cn_equity.wrong", data)
    errors = validate_directory(tmp_path)
    assert any("与 dataset 不一致" in error for error in errors)


def test_cycle_detected(tmp_path: Path) -> None:
    first = _base("cn_equity.a")
    second = _base("cn_equity.b")
    first["lineage"] = {"upstream": [{"dataset": "cn_equity.b"}], "transform": "join"}
    second["lineage"] = {"upstream": [{"dataset": "cn_equity.a"}], "transform": "join"}
    _write(tmp_path, "cn_equity.a", first)
    _write(tmp_path, "cn_equity.b", second)
    errors = validate_directory(tmp_path)
    assert any("血缘成环" in error for error in errors)


def test_duplicate_algorithm_id_rejected(tmp_path: Path) -> None:
    derived = [
        {
            "output": "x",
            "algorithm_id": "x_v1",
            "implementation": "pkg.mod.fn",
            "owner": "o",
            "inputs": ["cn_equity.a.close"],
            "description": "d",
        }
    ]
    first = _base("cn_equity.a", derived=derived)
    second = _base("cn_equity.b", derived=derived)
    _write(tmp_path, "cn_equity.a", first)
    _write(tmp_path, "cn_equity.b", second)
    errors = validate_directory(tmp_path)
    assert any("algorithm_id 重复" in error for error in errors)


def test_duplicate_dataset_rejected(tmp_path: Path) -> None:
    data = _base()
    _write(tmp_path, "cn_equity.demo", data)
    _write(tmp_path, "cn_equity.demo_copy", {**data, "dataset": "cn_equity.demo"})
    errors = validate_directory(tmp_path)
    assert any("dataset 重复定义" in error for error in errors)


def test_missing_lineage_fails_schema(tmp_path: Path) -> None:
    data = _base()
    del data["lineage"]
    _write(tmp_path, data["dataset"], data)
    errors = validate_directory(tmp_path)
    assert errors and "加载失败" in errors[0]


def test_freshness_rule_requires_sla_and_tolerance(tmp_path: Path) -> None:
    data = _base(quality=[{"rule": "freshness", "sla": "daily", "tolerance": "2h"}])
    _write(tmp_path, data["dataset"], data)
    assert validate_directory(tmp_path) == []

    broken = _base(quality=[{"rule": "freshness", "sla": "daily"}])
    _write(tmp_path, broken["dataset"], broken)
    errors = validate_directory(tmp_path)
    assert errors and "加载失败" in errors[0]


def test_storage_consistency_rules(tmp_path: Path) -> None:
    bad_table = _base()
    bad_table["storage"]["canonical_table"] = "cn_equity.wrong"
    _write(tmp_path, bad_table["dataset"], bad_table)

    bad_read_model = _base("cn_equity.b")
    bad_read_model["storage"]["read_model"] = "mart.demo_v2"
    _write(tmp_path, "cn_equity.b", bad_read_model)

    bad_partition = _base("cn_equity.c")
    bad_partition["storage"]["partition_strategy"] = "knowledge_time"
    _write(tmp_path, "cn_equity.c", bad_partition)

    errors = validate_directory(tmp_path)
    assert any("canonical_table 应为 cn_equity.demo" in error for error in errors)
    assert any("read_model 版本后缀与 semantic_version" in error for error in errors)
    assert any("partition_strategy 应为 event_time" in error for error in errors)


def test_mapping_endpoint_must_be_declared_in_sources(tmp_path: Path) -> None:
    data = _base(
        mappings=[
            {"provider": "tushare", "endpoint": "not_declared", "fields": {"close": "close"}}
        ]
    )
    _write(tmp_path, data["dataset"], data)
    errors = validate_directory(tmp_path)
    assert any("未在 sources 中声明" in error for error in errors)


def test_errors_are_accumulated_across_files(tmp_path: Path) -> None:
    good = _base("cn_equity.good")
    good["storage"]["canonical_table"] = "cn_equity.wrong"
    _write(tmp_path, "cn_equity.good", good)

    broken = _base("cn_equity.broken")
    del broken["lineage"]
    _write(tmp_path, "cn_equity.broken", broken)

    errors = validate_directory(tmp_path)
    assert any("canonical_table" in error for error in errors)
    assert any("加载失败" in error for error in errors)


def test_catalog_covers_datasets_and_access() -> None:
    from fin_data_platform.dictionary import catalog, catalog_markdown

    rows = {row["dataset"]: row for row in catalog()}
    assert rows["cn_equity.daily_bar"]["access"].startswith("get_bars")
    assert rows["cn_equity.daily_bar"]["entity"] == "entity"
    assert rows["ref.entity"]["entity"] == "entity"
    assert rows["ref.entity_code_history"]["access"] == "内部（Hub mapper）"
    # issuer_id 数据集同样按实体登记（财务改挂发行主体）
    assert rows["cn_equity.financials.balance_sheet"]["entity"] == "entity"
    assert rows["cn_equity.listing_lifecycle"]["entity"] == "entity"
    assert rows["ref.entity_relation"]["access"] == "内部（关系注册）"
    assert rows["ref.entity_external_id"]["access"] == "内部（外部标识注册）"
    assert rows["ref.relation_type_dict"]["access"] == "内部（关系词表）"
    document = catalog_markdown()
    assert "`cn_fund.nav`" in document
    assert "get_financials" in document


def test_real_entry_loadable() -> None:
    spec = load_file(DEFAULT_ROOT / "cn_equity" / "daily_bar.yaml")
    assert spec.storage.partition_strategy == "event_time"
    assert any(field.pit_role == "knowledge_time" for field in spec.fields)


def test_mappings_align_with_hub_adapter_specs() -> None:
    """doc-11 §6-8：字典 mappings（canonical 字段名）必须被适配器 spec 覆盖。"""
    from fin_data_hub.enums import Source
    from fin_data_hub.specs import load_spec

    provider_to_source = {
        "tushare": Source.TUSHARE,
        "baostock": Source.BAOSTOCK,
        "akshare": Source.AKSHARE,
        "wind": Source.WIND,
    }
    canonical_by_source: dict[str, set[str]] = {}
    for provider, source in provider_to_source.items():
        try:
            spec = load_spec(source)
        except FileNotFoundError:
            continue
        canonical_by_source[provider] = {
            canonical
            for response in spec.responses.values()
            for canonical in response.fields
        }

    for dataset, spec in load_all().items():
        for mapping in spec.mappings:
            known = canonical_by_source.get(str(mapping.provider))
            if known is None:
                continue
            # v0 参考/基础信息类接口为 bespoke 映射（无 response spec block）
            if mapping.endpoint in _REFERENCE_ENDPOINTS:
                continue
            for canonical in mapping.fields:
                if canonical == "entity_id" or canonical.endswith("_entity_id"):
                    continue  # 平台内部 ID（由 实体注册表 映射，非源字段）
                assert canonical in known, (
                    f"{dataset}: mapping 字段 {canonical!r} 未在"
                    f" {mapping.provider} 适配器 spec 中定义"
                )
