"""访问面异常（结构化：code / detail / hint，供 SDK 与 REST 同构映射）。"""

from __future__ import annotations


class AccessError(Exception):
    """访问面异常基类。"""

    code = "access_error"

    def __init__(self, detail: str, *, hint: str = "") -> None:
        self.detail = detail
        self.hint = hint
        super().__init__(detail)

    def __str__(self) -> str:
        return f"{self.detail}（{self.hint}）" if self.hint else self.detail


class UnknownDataset(AccessError):
    code = "invalid_dataset"


class UnknownField(AccessError):
    code = "invalid_field"


class UnsupportedAdjust(AccessError):
    """数据集未声明的复权口径（不静默返回原始值）。"""

    code = "unsupported_adjust"


class UnsupportedPitClass(AccessError):
    """数据集 pit_class 暂不支持规范化读取。"""

    code = "unsupported_pit_class"
