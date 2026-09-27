import pandas as pd
import pytest

from fin_data_hub import Capability, Source
from fin_data_hub.errors import ResponseParseError
from fin_data_hub.schemas import (
    ADJUST_FACTOR_COLUMNS,
    ADJUSTMENT_EVENT_COLUMNS,
    BALANCE_SHEET_COLUMNS,
    BARS_COLUMNS,
    CALENDAR_COLUMNS,
    FINANCIAL_INDICATOR_COLUMNS,
    INDEX_WEIGHT_COLUMNS,
    IPO_COLUMNS,
    NAMECHANGE_COLUMNS,
    NAV_COLUMNS,
    SNAPSHOT_COLUMNS,
    ST_COLUMNS,
    SUSPENSION_COLUMNS,
)
from fin_data_hub.specs import load_all_specs, load_spec, normalize, validate_specs

COVERAGE: dict[Source, set[Capability]] = {
    Source.TUSHARE: {
        Capability.BARS,
        Capability.FUND_NAV,
        Capability.TRADE_CALENDAR,
        Capability.ADJUST_FACTORS,
        Capability.INDEX_WEIGHTS,
        Capability.FINANCIALS,
        Capability.MARKET_EVENTS,
    },
    Source.AKSHARE: {
        Capability.BARS,
        Capability.FUND_NAV,
        Capability.SNAPSHOT,
        Capability.TRADE_CALENDAR,
    },
    Source.WIND: {Capability.BARS, Capability.SNAPSHOT},
    Source.FUYAO: {
        Capability.BARS,
        Capability.SNAPSHOT,
        Capability.ADJUSTMENT_EVENTS,
        Capability.TRADE_CALENDAR,
    },
    Source.BAOSTOCK: {
        Capability.BARS,
        Capability.TRADE_CALENDAR,
        Capability.ADJUST_FACTORS,
    },
}

#: capability → spec response 块别名（一个 capability 对多个 kind 时）
BLOCK_ALIASES: dict[Capability, tuple[str, ...]] = {
    Capability.FINANCIALS: ("balance_sheet", "financial_indicator"),
    Capability.MARKET_EVENTS: ("ipo", "suspension", "st", "namechange"),
}

#: endpoint → canonical schema 列（spec 目标列一致性校验）
SCHEMA_COLUMNS: dict[str, tuple[str, ...]] = {
    "bars": BARS_COLUMNS,
    "snapshot": SNAPSHOT_COLUMNS,
    "fund_nav": NAV_COLUMNS,
    "trade_calendar": CALENDAR_COLUMNS,
    "adjust_factors": ADJUST_FACTOR_COLUMNS,
    "index_weights": INDEX_WEIGHT_COLUMNS,
    "balance_sheet": BALANCE_SHEET_COLUMNS,
    "financial_indicator": FINANCIAL_INDICATOR_COLUMNS,
    "ipo": IPO_COLUMNS,
    "suspension": SUSPENSION_COLUMNS,
    "st": ST_COLUMNS,
    "namechange": NAMECHANGE_COLUMNS,
    "adjustment_events": ADJUSTMENT_EVENT_COLUMNS,
}


def test_specs_are_structurally_valid() -> None:
    assert validate_specs() == []


def test_spec_targets_are_canonical_columns() -> None:
    for source, spec in load_all_specs().items():
        for endpoint, response in spec.responses.items():
            expected = set(SCHEMA_COLUMNS[endpoint])
            extra = set(response.fields) - expected
            assert not extra, (
                f"{source}.{endpoint} 映射列不在 canonical schema: {sorted(extra)}"
            )


def test_spec_coverage() -> None:
    specs = load_all_specs()
    for source, endpoints in COVERAGE.items():
        assert source in specs, f"缺少 {source} spec"
        for endpoint in endpoints:
            blocks = BLOCK_ALIASES.get(endpoint, (endpoint.value,))
            for block in blocks:
                assert block in specs[source].responses, (
                    f"{source} 缺少 {endpoint} 映射"
                )


def test_normalize_tushare_bars_units_and_dates() -> None:
    spec = load_spec(Source.TUSHARE).responses["bars"]
    raw = pd.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "trade_date": ["20260105"],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.3],
            "vol": [1000.0],
            "amount": [1030.0],
        }
    )
    frame = normalize(raw, spec, source=Source.TUSHARE)
    assert frame["code"].tolist() == ["600000.SH"]
    assert str(frame["date"].dtype) == "datetime64[ns]"
    assert frame["volume"].tolist() == [100_000.0]  # 手 → 股
    assert frame["amount"].tolist() == [1_030_000.0]  # 千元 → 元


def test_normalize_fuyao_snapshot_date_ms() -> None:
    spec = load_spec(Source.FUYAO).responses["snapshot"]
    ms = 1757558400000
    raw = pd.DataFrame(
        {
            "thscode": ["600519.SH"],
            "date_ms": [ms],
            "last_price": [1277.8],
            "open_price": [1252.08],
            "high_price": [1282.0],
            "low_price": [1250.21],
            "prev_price": [1256.0],
            "volume": [3_098_875],
            "turnover": [3_937_375_200.0],
        }
    )
    frame = normalize(raw, spec, source=Source.FUYAO)
    # 快照日期来自响应信封 timestamp（adapter 级处理），spec 不映射 date
    assert "date" not in frame.columns
    assert frame["last"].tolist() == [1277.8]
    assert frame["code"].tolist() == ["600519.SH"]


def test_normalize_missing_required_raises() -> None:
    spec = load_spec(Source.TUSHARE).responses["bars"]
    raw = pd.DataFrame({"ts_code": ["600000.SH"]})
    with pytest.raises(ResponseParseError, match="缺少字段"):
        normalize(raw, spec, source=Source.TUSHARE)


def test_normalize_missing_mapped_field_raises() -> None:
    from fin_data_hub.specs import FieldSpec, ResponseSpec

    spec = ResponseSpec(
        required=("a",), fields={"code": FieldSpec(source="missing", type="code")}
    )
    raw = pd.DataFrame({"a": [1]})
    with pytest.raises(ResponseParseError, match="缺少映射源字段"):
        normalize(raw, spec, source=Source.FUYAO)


def test_normalize_optional_field_and_code_override() -> None:
    spec = load_spec(Source.AKSHARE).responses["fund_nav"]
    raw = pd.DataFrame({"净值日期": ["2026-01-05"], "单位净值": [1.234]})
    frame = normalize(raw, spec, source=Source.AKSHARE, code="000001.OF")
    assert frame["code"].tolist() == ["000001.OF"]
    assert frame["accum_nav"].isna().all()  # optional 缺失 → 置空
    assert "daily_return" not in frame.columns or frame["daily_return"].isna().all()


def test_normalize_akshare_bars_golden() -> None:
    spec = load_spec(Source.AKSHARE).responses["bars"]
    raw = pd.DataFrame(
        {
            "日期": ["2026-01-05", "2026-01-06"],
            "开盘": [10.0, 10.5],
            "收盘": [10.5, 10.8],
            "最高": [10.6, 10.9],
            "最低": [9.9, 10.4],
            "成交量": [1000.0, 2000.0],
            "成交额": [10500.0, 21600.0],
        }
    )
    frame = normalize(raw, spec, source=Source.AKSHARE, code="600000.SH")
    assert frame["code"].tolist() == ["600000.SH", "600000.SH"]
    assert frame["date"].tolist() == [
        pd.Timestamp("2026-01-05"),
        pd.Timestamp("2026-01-06"),
    ]
    assert frame["volume"].tolist() == [100_000.0, 200_000.0]  # 手 → 股


def test_normalize_fuyao_bars_golden() -> None:
    spec = load_spec(Source.FUYAO).responses["bars"]
    ms = 1767571200000  # 2026-01-05 00:00 Asia/Shanghai
    raw = pd.DataFrame(
        {
            "date_ms": [ms],
            "open_price": [10.0],
            "high_price": [10.6],
            "low_price": [9.9],
            "close_price": [10.5],
            "volume": [1_000_000.0],
            "turnover": [10_500_000.0],
        }
    )
    frame = normalize(raw, spec, source=Source.FUYAO, code="600000.SH")
    assert frame["date"].tolist() == [pd.Timestamp("2026-01-05")]  # date_ms → 沪市日期
    assert frame["close"].tolist() == [10.5]


def test_normalize_baostock_bars_and_factors_golden() -> None:
    spec = load_spec(Source.BAOSTOCK)
    bars_raw = pd.DataFrame(
        {
            "date": ["2026-09-01"],
            "open": ["1295.0"],
            "high": ["1307.99"],
            "low": ["1286.10"],
            "close": ["1299.56"],
            "volume": ["3266402"],
            "amount": ["4242441000"],
        }
    )
    bars = normalize(bars_raw, spec.responses["bars"], source=Source.BAOSTOCK, code="600000.SH")
    assert bars["close"].tolist() == [1299.56]
    assert str(bars["date"].dtype) == "datetime64[ns]"

    factors_raw = pd.DataFrame(
        {
            "dividOperateDate": ["2024-07-18"],
            "backAdjustFactor": ["12.388310"],
            "foreAdjustFactor": ["0.967359"],
        }
    )
    factors = normalize(
        factors_raw, spec.responses["adjust_factors"], source=Source.BAOSTOCK
    )
    assert factors["adj_factor"].tolist() == [12.388310]
    assert factors["date"].tolist() == [pd.Timestamp("2024-07-18")]


def test_normalize_tushare_calendar_bool_golden() -> None:
    spec = load_spec(Source.TUSHARE).responses["trade_calendar"]
    raw = pd.DataFrame(
        {"cal_date": ["20260105", "20260106"], "is_open": ["1", "0"]}
    )
    frame = normalize(raw, spec, source=Source.TUSHARE)
    assert frame["is_open"].tolist() == [True, False]
