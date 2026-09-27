"""源侧读取（源层）：直连 Hub 取数 × 平台落库参照数据的规范化合成。

入口 :func:`read_bars_with_status`：交易日 × 标的的行情 + 状态（三态：
``ok`` / ``suspended`` / ``missing``），非交易日无行；SQL 不 JOIN。
"""

from __future__ import annotations

from fin_data_platform.source.read import (
    BAR_FIELDS,
    DATASET,
    DEFAULT_EXCHANGE,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_SUSPENDED,
    SourceReadError,
    SourceReadMeta,
    SourceReadResult,
    UnknownEntity,
    read_bars_with_status,
)

__all__ = [
    "BAR_FIELDS",
    "DATASET",
    "DEFAULT_EXCHANGE",
    "STATUS_MISSING",
    "STATUS_OK",
    "STATUS_SUSPENDED",
    "SourceReadError",
    "SourceReadMeta",
    "SourceReadResult",
    "UnknownEntity",
    "read_bars_with_status",
]
