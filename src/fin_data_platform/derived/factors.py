"""因子实现（doc-11 §4；doc-21「因子库」形态）。

约定：

- 输入经**访问面**读取（默认口径=字典 ``adjust.default``；原始值需显式 ``@raw``）；
- 返回值必须包含数据集业务键 + ``output`` 列（引擎校验）；
- docstring 必须包含 ``Formula`` 与 ``PIT``（CI 校验）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fin_data_platform.derived.registry import register
from fin_data_platform.derived.smoothing import Wilder

if TYPE_CHECKING:
    import pyarrow as pa

#: 20 日收盘价均线（默认后复权口径；窗口内须有 20 个非空收盘价才输出）
_MA20_SQL = """
SELECT entity_id, trade_date, ma20
FROM (
    SELECT entity_id,
           trade_date,
           AVG(close) OVER (
               PARTITION BY entity_id
               ORDER BY trade_date
               ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
           ) AS ma20,
           COUNT(close) OVER (
               PARTITION BY entity_id
               ORDER BY trade_date
               ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
           ) AS _n
    FROM daily
) AS windowed
WHERE _n = 20
ORDER BY entity_id, trade_date
"""


@register(algorithm_id="ma20", version=1, owner="derived-engine")
def ma20(inputs: Mapping[str, pa.Table], *, as_of: datetime) -> pa.Table:
    """20 日收盘价均线（ma20）。

    Formula:
        ``ma20_t = mean(close_{t-19..t})``；``close`` 取访问面规范化值（声明为**显式后复权**
        ``close@hfq = raw × f``；如需前复权改声明为 ``@qfq``）。窗口内不足 20 个非空收盘价时
        **不输出该行**（不做部分窗口）。

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤（防前视）；``as_of`` 用于
        审计标注，本函数不重新读数据。
    """
    import duckdb

    daily = inputs["cn_equity.daily_bar.close@hfq"]
    connection = duckdb.connect()
    try:
        connection.register("daily", daily)
        return connection.execute(_MA20_SQL).to_arrow_table()
    finally:
        connection.close()


#: ADX 计算周期（v1 固定 14；参数化属算法升级）
_ADX_PERIOD = 14

_ADX_BASE_SQL = """
SELECT entity_id, trade_date,
       GREATEST(high - low, ABS(high - prev_close), ABS(low - prev_close)) AS tr,
       CASE
           WHEN high - prev_high > prev_low - low AND high - prev_high > 0
           THEN high - prev_high ELSE 0
       END AS plus_dm,
       CASE
           WHEN prev_low - low > high - prev_high AND prev_low - low > 0
           THEN prev_low - low ELSE 0
       END AS minus_dm
FROM (
    SELECT h.entity_id, h.trade_date, h.high, l.low, c.close,
           COALESCE(LAG(c.close) OVER w, c.close) AS prev_close,
           LAG(h.high) OVER w AS prev_high,
           LAG(l.low) OVER w AS prev_low
    FROM high AS h
    JOIN low AS l ON l.entity_id = h.entity_id AND l.trade_date = h.trade_date
    JOIN close AS c ON c.entity_id = h.entity_id AND c.trade_date = h.trade_date
    WINDOW w AS (PARTITION BY h.entity_id ORDER BY h.trade_date)
) AS base
ORDER BY entity_id, trade_date
"""


@dataclass(slots=True)
class _AdxState:
    """单实体 ADX 累加：DI 由三路 Wilder 平滑合成，DX 再作一次 Wilder 平滑。"""

    period: int = _ADX_PERIOD
    _tr: Wilder = field(init=False)
    _plus: Wilder = field(init=False)
    _minus: Wilder = field(init=False)
    _dx_seed: list[float] = field(default_factory=list, init=False)
    _adx: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._tr = Wilder(self.period)
        self._plus = Wilder(self.period)
        self._minus = Wilder(self.period)

    def push(self, tr: float, plus_dm: float, minus_dm: float) -> float | None:
        tr_s = self._tr.push(tr)
        plus_s = self._plus.push(plus_dm)
        minus_s = self._minus.push(minus_dm)
        if tr_s is None or plus_s is None or minus_s is None:
            return None
        if tr_s <= 0:  # 无波动区间：DX 记 0（避免除零）
            dx = 0.0
        else:
            plus_di = 100.0 * plus_s / tr_s
            minus_di = 100.0 * minus_s / tr_s
            total = plus_di + minus_di
            dx = 0.0 if total <= 0 else 100.0 * abs(plus_di - minus_di) / total
        if self._adx is None:
            self._dx_seed.append(dx)
            if len(self._dx_seed) < self.period:
                return None
            self._adx = sum(self._dx_seed) / self.period
            return self._adx
        self._adx += (dx - self._adx) / self.period
        return self._adx


@register(algorithm_id="adx", version=1, owner="derived-engine")
def adx(inputs: Mapping[str, pa.Table], *, as_of: datetime) -> pa.Table:
    """14 日平均趋向指数（ADX，Wilder）。

    Formula:
        ``TR = max(high-low, |high-prev_close|, |low-prev_close|)``；
        ``+DM``/``-DM`` 取上行/下行动量较大且为正的一侧（否则 0）；
        ``TR/+DM/-DM`` 各自 **Wilder 平滑**（首值 = 前 14 期均值，其后
        ``s_t = s_{t-1} + (x_t - s_{t-1}) / 14``）；``+DI = 100·sm(+DM)/sm(TR)``、
        ``-DI = 100·sm(-DM)/sm(TR)``；``DX = 100·|+DI - -DI| / (+DI + -DI)``；
        ``ADX`` = DX 的 Wilder 平滑（首值 = 前 14 个 DX 均值）。``sm(TR) = 0``
        （无波动）时 DX 记 0。平滑由共享原语 ``derived.smoothing.Wilder`` 提供（单一实现，
        不登记为因子）。预热不足 ``2·14 - 1`` 根 bar 不输出该实体行；无数据行（停牌等）
        的区分由访问面承担（TASK-3.31）；
        输入为**显式后复权**（``high/low/close@hfq``，同乘因子，DM/TR 比例不变）。

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤（防前视）；``as_of`` 仅审计标注，
        本函数不重新读数据。
    """
    import duckdb
    import pyarrow as pa

    high = inputs["cn_equity.daily_bar.high@hfq"]
    low = inputs["cn_equity.daily_bar.low@hfq"]
    close = inputs["cn_equity.daily_bar.close@hfq"]
    connection = duckdb.connect()
    try:
        connection.register("high", high)
        connection.register("low", low)
        connection.register("close", close)
        base = connection.execute(_ADX_BASE_SQL).to_arrow_table()
    finally:
        connection.close()

    entity_type = base.schema.field("entity_id").type
    date_type = base.schema.field("trade_date").type
    states: dict[int, _AdxState] = {}
    out_entity: list[int] = []
    out_date: list[Any] = []
    out_adx: list[float] = []
    for row in base.to_pylist():
        state = states.setdefault(row["entity_id"], _AdxState())
        value = state.push(row["tr"], row["plus_dm"], row["minus_dm"])
        if value is not None:
            out_entity.append(row["entity_id"])
            out_date.append(row["trade_date"])
            out_adx.append(value)
    return pa.table(
        {
            "entity_id": pa.array(out_entity, type=entity_type),
            "trade_date": pa.array(out_date, type=date_type),
            "adx": pa.array(out_adx, type=pa.float64()),
        }
    )
