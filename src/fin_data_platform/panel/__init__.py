"""时序查询面（TASK-3.13）：范围序列 / 截面 / 面板 / 版本历史 / asof join。

- :func:`get_series`：多键 × 字段 × 频率；``as_of`` 显式（PIT 严格）；
- :func:`get_cross_section`：单事件日截面；
- :func:`get_panel`：长表 / 宽表（``shape="wide"``）；
- :func:`get_versions`：版本历史（``history``）/ as-first-reported（``vintage``）；
- :func:`asof_join`：跨序列对齐（backward 默认，PIT 安全）。

口径见各函数与 :mod:`fin_data_platform.panel.series` 模块 docstring。
"""

from __future__ import annotations

from fin_data_platform.panel.errors import (
    InvalidArgument,
    InvalidFill,
    PanelError,
    UnsupportedFrequency,
)
from fin_data_platform.panel.join import asof_join
from fin_data_platform.panel.series import (
    AGG_OPS,
    CALENDARS,
    DEFAULT_AGGS,
    FILL_STRATEGIES,
    FREQUENCIES,
    ROLLING_OPS,
    Rolling,
    get_cross_section,
    get_panel,
    get_series,
)
from fin_data_platform.panel.versions import get_versions

__all__ = [
    "AGG_OPS",
    "CALENDARS",
    "DEFAULT_AGGS",
    "FILL_STRATEGIES",
    "FREQUENCIES",
    "ROLLING_OPS",
    "InvalidArgument",
    "InvalidFill",
    "PanelError",
    "Rolling",
    "UnsupportedFrequency",
    "asof_join",
    "get_cross_section",
    "get_panel",
    "get_series",
    "get_versions",
]
