"""因子图/投影读取异常（结构化 code；SDK/REST 同构映射，见 doc-21）。"""

from __future__ import annotations


class FactorError(Exception):
    """因子读取/物化异常基类。"""

    code = "factor_error"

    def __init__(self, detail: str, *, hint: str = "") -> None:
        self.detail = detail
        self.hint = hint
        super().__init__(detail)

    def __str__(self) -> str:
        return f"{self.detail}（{self.hint}）" if self.hint else self.detail


class FactorNotMaterialized(FactorError):
    """上游因子投影不存在（latest 单份未物化）。"""

    code = "factor_not_materialized"


class AsOfNotAligned(FactorError):
    """请求时点早于投影知识锚（computed_at），单份投影无法回答。"""

    code = "as_of_not_aligned"


class UpstreamStale(FactorError):
    """上游算法已升级而下游投影未重算（指纹不一致）。"""

    code = "upstream_stale"
