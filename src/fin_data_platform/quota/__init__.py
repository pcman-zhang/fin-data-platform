"""平台侧配额：跨进程共享限流与成本预算聚合。

本包把 Hub 的进程内限流/计量升级为平台级共享口径（基于
:class:`~fin_data_platform.cache.LayeredCache` 的共享计数）：

- :mod:`fin_data_platform.quota.config`：环境变量解析（限流 / 预算）；
- :mod:`fin_data_platform.quota.shared`：固定窗口共享限流器（fail-open）；
- :mod:`fin_data_platform.quota.usage`：预算回调 → 共享计数；按源聚合查询。

共享计数属可重建的运行时状态，权威数据仍在 PostgreSQL；缓存不可用时
一律 fail-open（回退进程内限额，采集不被阻塞）。
"""

from fin_data_platform.quota.config import QuotaSettings, load_quota_settings
from fin_data_platform.quota.shared import SharedRateLimiter, shared_limiter_factory
from fin_data_platform.quota.usage import (
    make_shared_budget,
    read_usage,
    usage_key,
)

__all__ = [
    "QuotaSettings",
    "SharedRateLimiter",
    "shared_limiter_factory",
    "load_quota_settings",
    "make_shared_budget",
    "read_usage",
    "usage_key",
]
