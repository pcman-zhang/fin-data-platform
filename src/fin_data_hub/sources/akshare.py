"""AkShare 数据源适配器（免费公开源，默认应保守限流）。

- 代码：由 :class:`~fin_data_hub.mapping.AkShareMapper` 转为 6 位裸代码；
- bars 路由：股票 ``stock_zh_a_hist`` / ETF ``fund_etf_hist_em`` /
  LOF ``fund_lof_hist_em`` / 指数 ``index_zh_a_hist``；
- 快照：全市场 spot 接口（股票 ``stock_zh_a_spot_em`` / ETF ``fund_etf_spot_em`` /
  LOF ``fund_lof_spot_em``），一次调用覆盖全市场、按请求代码本地过滤；指数快照暂不支持；
- 单位统一：``volume`` 股（AkShare 手 ×100）、``amount`` 元；
- AkShare 各接口为单标的形式，本适配器逐 code 调用，调用次数控制见后续
  capability/合并任务。
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd

from fin_data_hub.codes import SecCode, SecType
from fin_data_hub.enums import Capability, Source
from fin_data_hub.errors import SourceError, UnsupportedCapability
from fin_data_hub.mapping import get_mapper
from fin_data_hub.ratelimit import default_rate_limiter_set
from fin_data_hub.sources.base import BaseAdapter
from fin_data_hub.specs import load_spec, normalize


def _date_param(value: str) -> str:
    text = str(value).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"日期格式应为 YYYYMMDD 或 YYYY-MM-DD: {value!r}")
    return text


#: 快照端点路由（指数无稳定的全市场 spot 接口，暂不支持）
_SNAPSHOT_ROUTES: dict[SecType, str] = {
    SecType.STOCK: "stock_zh_a_spot_em",
    SecType.ETF: "fund_etf_spot_em",
    SecType.LOF: "fund_lof_spot_em",
}

#: 基金 spot 与股票 spot 的列名差异（统一到 spec 的股票列名）
_FUND_SNAPSHOT_RENAMES = {"开盘价": "今开", "最高价": "最高", "最低价": "最低"}


def _cn_today() -> Any:
    """快照日期：Asia/Shanghai 当日（naive 零点，与库内 date 列口径一致）。"""
    return pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)


class AkShareAdapter(BaseAdapter):
    source = Source.AKSHARE
    capabilities = frozenset(
        {
            Capability.BARS,
            Capability.FUND_NAV,
            Capability.SNAPSHOT,
            Capability.TRADE_CALENDAR,
        }
    )

    def __init__(self, *, ak_module: Any | None = None) -> None:
        self._ak = ak_module if ak_module is not None else _default_module()
        self._mapper = get_mapper(self.source)
        self._rate_limits = default_rate_limiter_set(self.source)
        self._spec = load_spec(self.source)

    # ------------------------------------------------------------------ 行情
    def fetch_bars(
        self,
        codes: list[SecCode],
        *,
        start: str,
        end: str,
        freq: str,
        adjust: str | None,
        fields: tuple[str, ...] | None,
    ) -> pd.DataFrame:
        if freq not in ("1d", "d", "D"):
            raise UnsupportedCapability(f"AkShare 适配器暂不支持 freq={freq!r}（仅 1d）")
        if adjust not in (None, "qfq", "hfq"):
            raise ValueError(f"adjust 仅支持 None/qfq/hfq: {adjust!r}")

        frames = [
            self._fetch_bars_one(code, start=start, end=end, adjust=adjust)
            for code in codes
        ]
        if not frames:
            return _empty_bars()
        return pd.concat(frames, ignore_index=True)

    def _fetch_bars_one(
        self, code: SecCode, *, start: str, end: str, adjust: str | None
    ) -> pd.DataFrame:
        symbol = self._mapper.to_source(code)
        common = {
            "symbol": symbol,
            "period": "daily",
            "start_date": _date_param(start),
            "end_date": _date_param(end),
        }
        if code.sec_type is SecType.STOCK:
            raw = self._call("stock_zh_a_hist", **common, adjust=adjust or "")
        elif code.sec_type is SecType.ETF:
            raw = self._call("fund_etf_hist_em", **common, adjust=adjust or "")
        elif code.sec_type is SecType.LOF:
            raw = self._call("fund_lof_hist_em", **common, adjust=adjust or "")
        elif code.sec_type is SecType.INDEX:
            if adjust:
                raise UnsupportedCapability("AkShare 指数 K 线不支持复权")
            raw = self._call("index_zh_a_hist", **common)
        else:
            raise UnsupportedCapability(f"AkShare 不支持 {code.sec_type} 的 K 线")

        if raw.empty:
            return _empty_bars()
        frame = normalize(
            raw,
            self._spec.responses["bars"],
            source=self.source,
            code=code.canonical,
        )
        return frame.sort_values("date").reset_index(drop=True)

    # ---------------------------------------------------------------- 快照
    def fetch_snapshot(
        self,
        codes: list[SecCode],
        *,
        fields: tuple[str, ...] | None,
    ) -> pd.DataFrame:
        """快照：按资产类型调用全市场 spot 接口并本地过滤（fields 由门面裁剪）。"""
        if not codes:
            return _empty_snapshot()
        groups: dict[SecType, list[SecCode]] = {}
        for code in codes:
            if code.sec_type not in _SNAPSHOT_ROUTES:
                raise UnsupportedCapability(
                    f"AkShare 快照暂不支持 {code.sec_type}（{code.canonical}）；"
                    "支持股票 / ETF / LOF"
                )
            groups.setdefault(code.sec_type, []).append(code)
        frames = [
            self._fetch_snapshot_group(sec_type, group)
            for sec_type, group in groups.items()
        ]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return _empty_snapshot()
        return pd.concat(frames, ignore_index=True).sort_values("code").reset_index(
            drop=True
        )

    def _fetch_snapshot_group(
        self, sec_type: SecType, codes: list[SecCode]
    ) -> pd.DataFrame:
        route = _SNAPSHOT_ROUTES[sec_type]
        raw = self._call(route)
        if raw.empty:
            return _empty_snapshot()
        if sec_type is not SecType.STOCK:  # 基金 spot 列名差异 → 统一
            raw = raw.rename(columns=_FUND_SNAPSHOT_RENAMES)
        symbol_column = next(
            (name for name in ("代码", "symbol") if name in raw.columns), None
        )
        if symbol_column is None:
            raise SourceError(
                f"AkShare 快照响应缺少代码列（{route}）：{list(raw.columns)}"
            )
        wanted = {self._mapper.to_source(code): code.canonical for code in codes}
        matched = raw[raw[symbol_column].astype(str).isin(wanted)]
        if matched.empty:
            return _empty_snapshot()
        frame = normalize(
            matched,
            self._spec.responses["snapshot"],
            source=self.source,
        )
        # 裸 6 位代码无法反推 venue：由请求代码映射回 canonical（预过滤保证全覆盖）
        frame.insert(0, "code", matched[symbol_column].astype(str).map(wanted))
        frame.insert(1, "date", _cn_today())
        return frame

    # -------------------------------------------------------------- 基金净值
    def fetch_fund_nav(
        self,
        codes: list[SecCode],
        *,
        start: str | None,
        end: str | None,
    ) -> pd.DataFrame:
        frames = []
        for code in codes:
            if code.sec_type is not SecType.FUND:
                raise UnsupportedCapability(
                    f"AkShare 净值仅支持场外基金（.OF），收到 {code.canonical}"
                )
            frames.append(self._fetch_nav_one(code, start=start, end=end))
        if not frames:
            return _empty_nav()
        return pd.concat(frames, ignore_index=True)

    def _fetch_nav_one(
        self, code: SecCode, *, start: str | None, end: str | None
    ) -> pd.DataFrame:
        symbol = self._mapper.to_source(code)
        raw = self._call(
            "fund_open_fund_info_em",
            symbol=symbol,
            indicator="单位净值走势",
            period="成立来",
        )
        if raw.empty:
            return _empty_nav()
        nav = normalize(
            raw,
            self._spec.responses["fund_nav"],
            source=self.source,
            code=code.canonical,
        ).sort_values("date")
        if start:
            nav = nav[nav["date"] >= pd.to_datetime(_date_param(start))]
        if end:
            nav = nav[nav["date"] <= pd.to_datetime(_date_param(end))]
        return nav.reset_index(drop=True)

    # ---------------------------------------------------------------- 日历
    def fetch_trade_calendar(self, *, start: str, end: str) -> pd.DataFrame:
        raw = self._call("tool_trade_date_hist_sina")
        if raw.empty or "trade_date" not in raw.columns:
            raise SourceError("AkShare 交易日历响应缺少 trade_date")
        open_dates = set(pd.to_datetime(raw["trade_date"]).dt.normalize())
        days = pd.date_range(
            pd.to_datetime(_date_param(start)),
            pd.to_datetime(_date_param(end)),
            freq="D",
        )
        return pd.DataFrame(
            {"date": days, "is_open": [day in open_dates for day in days]}
        )

    # ------------------------------------------------------------------ 内部
    def _call(self, name: str, **kwargs: Any) -> pd.DataFrame:
        fn = getattr(self._ak, name, None)
        if fn is None:
            raise SourceError(f"AkShare 缺少接口 {name!r}")
        self._acquire(name)
        start = time.monotonic()
        try:
            result = fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一映射源端异常
            raise SourceError(f"AkShare {name} 调用失败: {exc}") from exc
        finally:
            self._record(name, latency_ms=(time.monotonic() - start) * 1000)
        if result is None:
            return pd.DataFrame()
        return pd.DataFrame(result)


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [],
            "date": pd.Series([], dtype="datetime64[ns]"),
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
            "amount": [],
        }
    )


def _empty_nav() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [],
            "date": pd.Series([], dtype="datetime64[ns]"),
            "unit_nav": [],
            "accum_nav": [],
            "daily_return": [],
        }
    )


def _empty_snapshot() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [],
            "date": pd.Series([], dtype="datetime64[ns]"),
            "last": [],
            "open": [],
            "high": [],
            "low": [],
            "prev_close": [],
            "volume": [],
            "amount": [],
        }
    )


def _default_module() -> Any:
    try:
        import akshare
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支
        raise SourceError(
            "未安装 akshare 包：请安装 fin-data-hub[akshare]"
        ) from exc
    return akshare
