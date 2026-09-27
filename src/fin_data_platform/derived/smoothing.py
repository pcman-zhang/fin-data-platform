"""平滑原语（doc-11 §4；TASK-3.30）：Wilder 平滑的**单一实现**。

定位：平滑是**计算步骤**，不是数据产品——不登记为因子、不物化、不进入依赖图；
各因子实现共用本原语，避免种子/边界口径分叉（"复权逻辑不出现第二份"的同构要求）。

约定：

- Wilder 平滑与 EMA（α = 1/n）**递推同构**，差别仅在**种子**：本原语首值取前
  ``period`` 期均值，其后 ``s_t = s_{t-1} + (x_t - s_{t-1}) / period``；
- 预热不足 ``period`` 期返回 ``None``（严格不输出，不做部分窗口近似）；
- 缺失值语义（停牌/状态/真缺失=NaN 的区分）由访问面承担（见 TASK-3.31）；
  本原语接收的数据为访问面规范化结果。
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["Wilder", "wilder_smooth"]


class Wilder:
    """Wilder 平滑累加器（有状态；适合逐行流式组合，如 DX → ADX）。"""

    __slots__ = ("period", "_seed", "value")

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError(f"period 必须 >= 1：{period}")
        self.period = period
        self._seed: list[float] = []
        self.value: float | None = None

    def push(self, x: float) -> float | None:
        """推入一期观测；预热不足返回 ``None``，其后返回当前平滑值。"""
        if self.value is None:
            self._seed.append(float(x))
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            return self.value
        self.value += (float(x) - self.value) / self.period
        return self.value


def wilder_smooth(values: Sequence[float], period: int) -> list[float | None]:
    """对整段序列做 Wilder 平滑；预热期以 ``None`` 占位（长度与输入一致）。"""
    state = Wilder(period)
    return [state.push(value) for value in values]
