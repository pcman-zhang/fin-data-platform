import pandas as pd
import pytest

from fin_data_hub import SecCode
from fin_data_hub.errors import SourceError, UnsupportedCapability
from fin_data_hub.sources.akshare import AkShareAdapter


def _hist_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": ["2026-01-05", "2026-01-06"],
            "开盘": [10.0, 10.2],
            "收盘": [10.3, 10.1],
            "最高": [10.5, 10.4],
            "最低": [9.9, 10.0],
            "成交量": [1000, 1200],
            "成交额": [1_030_000.0, 1_210_000.0],
        }
    )


class FakeAkModule:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_hist = False
        self.empty_hist = False

    def _record(self, name: str, kwargs: dict) -> pd.DataFrame:
        self.calls.append((name, kwargs))
        if self.fail_hist:
            raise RuntimeError("akshare exploded")
        return pd.DataFrame() if self.empty_hist else _hist_frame()

    def stock_zh_a_hist(self, **kwargs):
        return self._record("stock_zh_a_hist", kwargs)

    def fund_etf_hist_em(self, **kwargs):
        return self._record("fund_etf_hist_em", kwargs)

    def fund_lof_hist_em(self, **kwargs):
        return self._record("fund_lof_hist_em", kwargs)

    def index_zh_a_hist(self, **kwargs):
        return self._record("index_zh_a_hist", kwargs)

    def fund_open_fund_info_em(self, **kwargs):
        self.calls.append(("fund_open_fund_info_em", kwargs))
        return pd.DataFrame(
            {
                "净值日期": ["2026-01-05", "2026-01-06", "2026-01-07"],
                "单位净值": [1.0, 1.1, 1.21],
                "日增长率": [0.0, 10.0, 10.0],
            }
        )

    def tool_trade_date_hist_sina(self, **kwargs):
        self.calls.append(("tool_trade_date_hist_sina", kwargs))
        return pd.DataFrame(
            {"trade_date": ["2026-01-05", "2026-01-06", "2026-01-08"]}
        )


def make_adapter() -> tuple[AkShareAdapter, FakeAkModule]:
    fake = FakeAkModule()
    return AkShareAdapter(ak_module=fake), fake


def test_stock_bars_mapping_and_units() -> None:
    adapter, fake = make_adapter()
    df = adapter.fetch_bars(
        [SecCode.parse("600000.SH")],
        start="2026-01-05",
        end="2026-01-06",
        freq="1d",
        adjust=None,
        fields=None,
    )
    assert list(df.columns) == [
        "code",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    # 手 → 股
    assert df["volume"].tolist() == [100_000, 120_000]
    # 成交额已是元
    assert df["amount"].tolist() == [1_030_000.0, 1_210_000.0]
    assert df["code"].tolist() == ["600000.SH", "600000.SH"]
    assert str(df["date"].dtype) == "datetime64[ns]"
    name, kwargs = fake.calls[0]
    assert name == "stock_zh_a_hist"
    assert kwargs["symbol"] == "600000"
    assert kwargs["adjust"] == ""
    assert kwargs["start_date"] == "20260105"


def test_etf_lof_index_routing() -> None:
    cases = [
        ("510300.SH", "fund_etf_hist_em"),
        ("166009.SZ", "fund_lof_hist_em"),
        ("000300.SH", "index_zh_a_hist"),
    ]
    for code_text, endpoint in cases:
        adapter, fake = make_adapter()
        adapter.fetch_bars(
            [SecCode.parse(code_text)],
            start="20260105",
            end="20260106",
            freq="1d",
            adjust=None,
            fields=None,
        )
        assert fake.calls[0][0] == endpoint
        assert fake.calls[0][1]["symbol"] == code_text.split(".")[0]


def test_adjust_passed_through() -> None:
    adapter, fake = make_adapter()
    adapter.fetch_bars(
        [SecCode.parse("510300.SH")],
        start="20260105",
        end="20260106",
        freq="1d",
        adjust="qfq",
        fields=None,
    )
    assert fake.calls[0][1]["adjust"] == "qfq"


def test_index_adjust_rejected() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(UnsupportedCapability):
        adapter.fetch_bars(
            [SecCode.parse("000300.SH")],
            start="20260105",
            end="20260106",
            freq="1d",
            adjust="qfq",
            fields=None,
        )


def test_fund_bars_rejected() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(UnsupportedCapability):
        adapter.fetch_bars(
            [SecCode.parse("000001.OF")],
            start="20260105",
            end="20260106",
            freq="1d",
            adjust=None,
            fields=None,
        )


def test_fund_nav_mapping_and_filter() -> None:
    adapter, fake = make_adapter()
    df = adapter.fetch_fund_nav(
        [SecCode.parse("000001.OF")], start="2026-01-06", end=None
    )
    assert df["code"].tolist() == ["000001.OF", "000001.OF"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-06", "2026-01-07"]
    assert df["unit_nav"].tolist() == [1.1, 1.21]
    assert df["daily_return"].tolist() == [10.0, 10.0]
    assert df["accum_nav"].isna().all()
    name, kwargs = fake.calls[0]
    assert name == "fund_open_fund_info_em"
    assert kwargs["symbol"] == "000001"


def test_fund_nav_rejects_non_fund() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(UnsupportedCapability):
        adapter.fetch_fund_nav([SecCode.parse("600000.SH")], start=None, end=None)


def test_trade_calendar() -> None:
    adapter, _ = make_adapter()
    df = adapter.fetch_trade_calendar(start="2026-01-05", end="2026-01-08")
    assert df["is_open"].tolist() == [True, True, False, True]


def test_api_error_wrapped() -> None:
    adapter, fake = make_adapter()
    fake.fail_hist = True
    with pytest.raises(SourceError, match="stock_zh_a_hist"):
        adapter.fetch_bars(
            [SecCode.parse("600000.SH")],
            start="20260105",
            end="20260106",
            freq="1d",
            adjust=None,
            fields=None,
        )


def test_empty_result_keeps_schema() -> None:
    adapter, fake = make_adapter()
    fake.empty_hist = True
    df = adapter.fetch_bars(
        [SecCode.parse("600000.SH")],
        start="20260105",
        end="20260106",
        freq="1d",
        adjust=None,
        fields=None,
    )
    assert df.empty
    assert list(df.columns) == [
        "code",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]


def test_invalid_date_rejected() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(ValueError):
        adapter.fetch_bars(
            [SecCode.parse("600000.SH")],
            start="2026/01/05",
            end="20260106",
            freq="1d",
            adjust=None,
            fields=None,
        )


def test_capabilities() -> None:
    adapter, _ = make_adapter()
    assert adapter.capabilities == frozenset(
        {"bars", "fund_nav", "snapshot", "trade_calendar"}
    )


# ------------------------------------------------------------------ 快照（TASK-3.34）
class FakeAkSnapshotModule:
    """快照桩：全市场 spot 帧（含未知代码）+ 调用记录。"""

    def __init__(self, *, empty: bool = False, missing_volume: bool = False) -> None:
        self.calls: list[str] = []
        self._empty = empty
        self._missing_volume = missing_volume

    def stock_zh_a_spot_em(self) -> pd.DataFrame:
        self.calls.append("stock_zh_a_spot_em")
        if self._empty:
            return pd.DataFrame()
        frame = pd.DataFrame(
            {
                "代码": ["600519", "000001", "999999"],  # 999999：非请求代码
                "最新价": [1500.0, 12.5, 1.0],
                "今开": [1490.0, 12.0, 1.0],
                "最高": [1510.0, 12.8, 1.0],
                "最低": [1480.0, 11.9, 1.0],
                "昨收": [1495.0, 12.2, 1.0],
                "成交量": [1000, 2000, 5],
                "成交额": [1.5e9, 2.5e7, 5.0],
            }
        )
        return frame.drop(columns=["成交量"]) if self._missing_volume else frame

    def fund_etf_spot_em(self) -> pd.DataFrame:
        self.calls.append("fund_etf_spot_em")
        if self._empty:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "代码": ["510300"],
                "最新价": [4.1],
                "开盘价": [4.0],
                "最高价": [4.2],
                "最低价": [3.9],
                "昨收": [4.05],
                "成交量": [500],
                "成交额": [2.05e6],
            }
        )


def test_snapshot_maps_filters_and_converts_units() -> None:
    fake = FakeAkSnapshotModule()
    adapter = AkShareAdapter(ak_module=fake)
    frame = adapter.fetch_snapshot(
        [SecCode.parse("600519.SH"), SecCode.parse("510300.SH")], fields=None
    )
    assert list(frame["code"]) == ["510300.SH", "600519.SH"]  # 排序且仅请求代码
    assert list(frame["date"].dt.date.unique()) == [pd.Timestamp.now(
        tz="Asia/Shanghai"
    ).date()]
    stock = frame[frame["code"] == "600519.SH"].iloc[0]
    assert stock["last"] == 1500.0 and stock["prev_close"] == 1495.0
    assert stock["volume"] == 100000.0  # 手 → 股（×100）
    assert stock["amount"] == 1.5e9
    etf = frame[frame["code"] == "510300.SH"].iloc[0]
    assert etf["open"] == 4.0 and etf["high"] == 4.2 and etf["volume"] == 50000.0
    assert fake.calls == ["stock_zh_a_spot_em", "fund_etf_spot_em"]  # 每端点一次


def test_snapshot_rejects_unsupported_sec_type() -> None:
    adapter = AkShareAdapter(ak_module=FakeAkSnapshotModule())
    with pytest.raises(UnsupportedCapability, match="暂不支持"):
        adapter.fetch_snapshot([SecCode.parse("000300.SH")], fields=None)


def test_snapshot_empty_and_missing_columns() -> None:
    adapter = AkShareAdapter(ak_module=FakeAkSnapshotModule(empty=True))
    empty = adapter.fetch_snapshot([SecCode.parse("600519.SH")], fields=None)
    assert empty.empty and list(empty.columns) == [
        "code",
        "date",
        "last",
        "open",
        "high",
        "low",
        "prev_close",
        "volume",
        "amount",
    ]

    broken = AkShareAdapter(ak_module=FakeAkSnapshotModule(missing_volume=True))
    from fin_data_hub.errors import ResponseParseError

    with pytest.raises(ResponseParseError, match="缺少字段"):
        broken.fetch_snapshot([SecCode.parse("600519.SH")], fields=None)


def test_get_snapshot_via_hub() -> None:
    from fin_data_hub import FinDataHub, HubConfig
    from fin_data_hub.sources import SourceRegistry

    hub = FinDataHub(
        HubConfig(), registry=SourceRegistry([AkShareAdapter(ak_module=FakeAkSnapshotModule())])
    )
    frame = hub.get_snapshot(["600519.SH"], source="akshare")
    assert len(frame) == 1
    assert frame.iloc[0]["code"] == "600519.SH"
    assert frame.iloc[0]["currency"] == "CNY"
    assert frame.attrs["source"] == "akshare"
    # 能力表：快照为全市场接口，不限单次代码数
    from fin_data_hub.capabilities import get_capability
    from fin_data_hub.enums import Source

    assert get_capability(Source.AKSHARE, "snapshot").max_codes_per_call is None
