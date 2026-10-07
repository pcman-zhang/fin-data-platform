"""SDK 统一异常（doc-21 §4）：``code / detail / hint / request_id``；直连与 REST 同构。

各层（access / derived / query / panel / control）的异常都带结构化 ``code`` / ``hint``，
SDK 只做统一出口与 REST 问题体（RFC 9457）解析，不改变语义。
"""

from __future__ import annotations

from typing import Any


class FinDataError(Exception):
    """SDK 异常基类（所有层错误的统一出口）。"""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        hint: str = "",
        request_id: str | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.hint = hint
        self.request_id = request_id
        super().__init__(detail)

    def __str__(self) -> str:
        return f"{self.detail}（{self.hint}）" if self.hint else self.detail


def from_backend_error(exc: Exception, *, request_id: str | None = None) -> FinDataError:
    """把各层结构化异常映射为 :class:`FinDataError`（保持 code / hint）。"""
    if isinstance(exc, FinDataError):
        return exc
    code = getattr(exc, "code", None) or type(exc).__name__.lower()
    if code == "intent_error":
        # 控制面「请求语义不支持」的基类码：与 REST 侧映射同构
        code = "invalid_request"
    detail = getattr(exc, "detail", None) or str(exc)
    hint = getattr(exc, "hint", "") or ""
    return FinDataError(
        str(code), str(detail), hint=str(hint), request_id=request_id
    )


def from_problem(
    payload: dict[str, Any], *, status: int, request_id: str | None = None
) -> FinDataError:
    """RFC 9457 问题体（REST 模式）→ :class:`FinDataError`。"""
    code = str(payload.get("title") or payload.get("code") or f"http_{status}")
    detail = str(payload.get("detail") or payload.get("title") or f"HTTP {status}")
    hint = str(payload.get("hint") or "")
    return FinDataError(
        code,
        detail,
        hint=hint,
        request_id=request_id or payload.get("request_id"),
    )
