"""端点能力元数据（驱动请求分块与合并）。

表键：``(Source, capability)``；capability 与 :class:`~fin_data_hub.sources.base.BaseAdapter`
的 ``CAP_*`` 常量一致，另含适配器级能力（如 ``"edb"``）。

约定：
- ``max_codes_per_call=None`` 表示单次调用可容纳任意多代码（由适配器内部处理）；
- ``max_indicators_per_call=None`` 表示不限，``1`` 表示一次仅一个指标；
- ``cost_class`` 为通用成本分级（``free`` / ``metered`` / ``premium``），
  不包含任何厂商具体价格。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fin_data_hub.codes import SecCode
from fin_data_hub.enums import Capability, Source

CostClass = Literal["free", "metered", "premium"]


@dataclass(frozen=True, slots=True)
class EndpointCapability:
    max_codes_per_call: int | None = None
    max_indicators_per_call: int | None = None
    supports_multi_symbol: bool = True
    supports_history: bool = True
    cost_class: CostClass = "free"


_DEFAULT = EndpointCapability()

CAPABILITIES: dict[tuple[Source, Capability], EndpointCapability] = {
    # Tushare：ts_code 支持逗号批量；限额随积分档变化，由限流器/预算配置控制
    (Source.TUSHARE, Capability.BARS): EndpointCapability(
        max_codes_per_call=None, cost_class="free"
    ),
    (Source.TUSHARE, Capability.FUND_NAV): EndpointCapability(
        max_codes_per_call=None, cost_class="free"
    ),
    (Source.TUSHARE, Capability.ADJUST_FACTORS): EndpointCapability(
        max_codes_per_call=None, cost_class="free"
    ),
    (Source.TUSHARE, Capability.SECURITY_INFO): EndpointCapability(
        max_codes_per_call=None, cost_class="free"
    ),
    (Source.TUSHARE, Capability.INDEX_WEIGHTS): EndpointCapability(
        max_codes_per_call=1, supports_multi_symbol=False, cost_class="free"
    ),
    (Source.TUSHARE, Capability.FINANCIALS): EndpointCapability(
        max_codes_per_call=None, cost_class="free"
    ),
    (Source.TUSHARE, Capability.MARKET_EVENTS): EndpointCapability(
        max_codes_per_call=None, cost_class="free"
    ),
    # AkShare：行情/净值为单标的形式；快照为全市场 spot 接口（一次调用覆盖全市场，
    # 按请求代码本地过滤，故不限代码数）
    (Source.AKSHARE, Capability.BARS): EndpointCapability(
        max_codes_per_call=1, supports_multi_symbol=False, cost_class="free"
    ),
    (Source.AKSHARE, Capability.FUND_NAV): EndpointCapability(
        max_codes_per_call=1, supports_multi_symbol=False, cost_class="free"
    ),
    (Source.AKSHARE, Capability.SNAPSHOT): EndpointCapability(
        max_codes_per_call=None, supports_multi_symbol=True, cost_class="free"
    ),
    # iFinD：NL 工具普遍支持多标的/多指标聚合（已抽验 stock/fund/edb）；
    # max_codes_per_call=50 是请求体积的安全上限，非接口限制
    (Source.IFIND, Capability.BARS): EndpointCapability(
        max_codes_per_call=50, supports_multi_symbol=True, cost_class="metered"
    ),
    (Source.IFIND, Capability.FUND_NAV): EndpointCapability(
        max_codes_per_call=50, supports_multi_symbol=True, cost_class="metered"
    ),
    (Source.IFIND, Capability.EDB): EndpointCapability(
        max_indicators_per_call=None, supports_multi_symbol=True, cost_class="metered"
    ),
    # Fuyao：K 线单标的且窗口 ≤10 年；快照支持 thscodes 批量（50 为安全上限）
    (Source.FUYAO, Capability.BARS): EndpointCapability(
        max_codes_per_call=1, supports_multi_symbol=False, cost_class="free"
    ),
    (Source.FUYAO, Capability.SNAPSHOT): EndpointCapability(
        max_codes_per_call=50, cost_class="free"
    ),
    # BaoStock：K 线单代码（sh./sz. 前缀，映射层转换）；复权因子同样单代码
    (Source.BAOSTOCK, Capability.BARS): EndpointCapability(
        max_codes_per_call=1, supports_multi_symbol=False, cost_class="free"
    ),
    (Source.BAOSTOCK, Capability.ADJUST_FACTORS): EndpointCapability(
        max_codes_per_call=1, supports_multi_symbol=False, cost_class="free"
    ),
    # Wind：K 线单代码；快照单次 ≤50；EDB 精确代码可批量
    (Source.WIND, Capability.BARS): EndpointCapability(
        max_codes_per_call=1, supports_multi_symbol=False, cost_class="premium"
    ),
    (Source.WIND, Capability.SNAPSHOT): EndpointCapability(
        max_codes_per_call=50, cost_class="premium"
    ),
    (Source.WIND, Capability.EDB): EndpointCapability(
        max_indicators_per_call=None, cost_class="premium"
    ),
}


def get_capability(
    source: Source | str, capability: Capability | str
) -> EndpointCapability:
    """返回能力元数据；未登记的组合返回默认值（不限代码数）。"""
    try:
        key = (Source(source), Capability(capability))
    except ValueError:
        return _DEFAULT
    return CAPABILITIES.get(key, _DEFAULT)


def split_codes(
    source: Source | str,
    capability: Capability | str,
    codes: list[SecCode],
) -> list[list[SecCode]]:
    """按能力上限把代码列表切成多个调用批次（保持原顺序）。"""
    limit = get_capability(source, capability).max_codes_per_call
    if limit is None or len(codes) <= limit:
        return [list(codes)]
    return [codes[index : index + limit] for index in range(0, len(codes), limit)]
