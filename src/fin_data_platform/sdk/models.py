"""SDK 结果模型与契约（doc-21 §3 / doc-2 §6.17）：Pydantic v2，直连与 REST 共用。

- :class:`ResultMeta` / :class:`FactorResultMeta`：结果元数据（source/as_of/quality 语义）；
- :class:`ControlRunInfo`：控制面意图运行信息；
- :class:`ErrorModel`：与 REST 问题体（RFC 9457）同构；
- :func:`export_json_schema`：导出 JSON Schema（契约文档 / 跨语言客户端生成）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ResultMeta(_Base):
    """访问面 / 消费面读取的元数据（两模式同构）。"""

    dataset: str
    as_of: datetime | None = None
    version_mode: str | None = None
    policy: str | None = None
    fallback: str | None = None
    adjust: str | None = None
    semantic_version: int
    data_generation: str | None = None
    row_count: int
    warnings: list[str] = []
    # 访问面 / 对齐扩展
    aligned: bool | None = None
    calendar_dataset: str | None = None
    status_dataset: str | None = None
    trading_days: int | None = None
    factor_dataset: str | None = None
    adjusted_fields: list[str] = []
    # 消费面扩展
    next_cursor: str | None = None


class FactorResultMeta(_Base):
    """因子读取元数据（审计三件套 + 物化状态）。"""

    dataset: str
    output: str
    algorithm_id: str
    algorithm_version: int | None = None
    as_of: datetime
    materialized: bool
    data_generation: str | None = None
    computed_at: datetime | None = None
    upstream_fingerprint: str | None = None
    row_count: int
    warnings: list[str] = []


class ControlRunInfo(_Base):
    """控制面意图运行信息（ensure / materialize / trigger）。"""

    run_id: int
    job_id: str
    dataset: str
    kind: str
    status: str
    window_start: date | None = None
    window_end: date | None = None
    created: bool = True
    note: str | None = None


class ErrorModel(_Base):
    """与 REST 问题体（RFC 9457）同构的错误模型。"""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None
    hint: str | None = None


@dataclass(slots=True)
class SdkResult:
    """读取结果：``frame``（pandas）/ ``table``（Arrow，直连）/ ``meta``（Pydantic）。"""

    frame: pd.DataFrame
    meta: ResultMeta | FactorResultMeta
    table: Any | None = None


@dataclass(slots=True)
class SdkRun:
    """控制面意图句柄（``wait`` 轮询到终态；超时抛 ``timeout``）。"""

    info: ControlRunInfo
    _wait: Callable[[float | None], ControlRunInfo] = field(repr=False)

    def wait(self, timeout: float | None = None) -> ControlRunInfo:
        self.info = self._wait(timeout)
        return self.info


#: 契约模型（导出 JSON Schema / 文档用）
CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "ResultMeta": ResultMeta,
    "FactorResultMeta": FactorResultMeta,
    "ControlRunInfo": ControlRunInfo,
    "ErrorModel": ErrorModel,
}


def export_json_schema() -> dict[str, Any]:
    """导出契约模型 JSON Schema（跨语言客户端 / 文档生成用）。"""
    return {name: model.model_json_schema() for name, model in CONTRACT_MODELS.items()}
