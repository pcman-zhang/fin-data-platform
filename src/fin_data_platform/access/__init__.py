"""访问面（Access）：Canonical 的统一规范化读取。

- :func:`read`：PIT（``as_of``）+ 口径组合（复权，缺省取字典声明）；
- :func:`read_sql`：渲染读取 SQL（``literal=True`` 供读模型内联；与 ``read`` 同一实现）；
- 异常：:class:`AccessError` 及子类（code / hint 结构化，SDK 与 REST 同构映射）。
"""

from __future__ import annotations

from fin_data_platform.access.errors import (
    AccessError,
    UnknownDataset,
    UnknownField,
    UnsupportedAdjust,
    UnsupportedPitClass,
)
from fin_data_platform.access.reader import (
    ADJUST_MODES,
    SUPPORTED_PIT_CLASSES,
    ReadMeta,
    ReadResult,
    dataset_asof_sql,
    normalize_as_of,
    read,
    read_sql,
)

__all__ = [
    "ADJUST_MODES",
    "SUPPORTED_PIT_CLASSES",
    "AccessError",
    "ReadMeta",
    "ReadResult",
    "UnknownDataset",
    "UnknownField",
    "UnsupportedAdjust",
    "UnsupportedPitClass",
    "dataset_asof_sql",
    "normalize_as_of",
    "read",
    "read_sql",
]
