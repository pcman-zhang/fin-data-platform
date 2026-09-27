"""时序查询面异常（结构化 code / detail / hint；SDK 与 REST 同构映射）。"""

from __future__ import annotations


class PanelError(Exception):
    """时序查询异常基类。"""

    code = "panel_error"

    def __init__(self, detail: str, *, hint: str = "") -> None:
        self.detail = detail
        self.hint = hint
        super().__init__(detail)

    def __str__(self) -> str:
        return f"{self.detail}（{self.hint}）" if self.hint else self.detail


class UnsupportedFrequency(PanelError):
    """频率不受支持（v1：1d / 1w / 1mo / 1q / 1y）。"""

    code = "unsupported_frequency"


class InvalidFill(PanelError):
    """缺口策略非法（v1：none / ffill）。"""

    code = "invalid_fill"


class InvalidArgument(PanelError):
    """其它参数非法（日历取值 / 窗口算子 / 输出形态 / asof join 方向等）。"""

    code = "invalid_argument"
