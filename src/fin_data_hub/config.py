"""配置对象：凭证与运行参数由调用方注入。

规范：
- 密钥字段一律 ``repr=False``，避免进入日志/异常信息；
- 库不读取任何外部全局配置或用户目录。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from fin_data_hub.enums import Source
from fin_data_hub.ratelimit import RateLimitConfig, RateLimiter
from fin_data_hub.routing import RoutingConfig
from fin_data_hub.usage import BudgetConfig


@dataclass(frozen=True, slots=True)
class TushareConfig:
    token: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WindConfig:
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class IfindConfig:
    authorization: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class FuyaoConfig:
    api_key: str | None = field(default=None, repr=False)
    base_url: str = "https://fuyao.aicubes.cn"
    max_attempts: int = 2


@dataclass(frozen=True, slots=True)
class BaostockConfig:
    """BaoStock 无鉴权；``max_attempts`` 控制断线重登录重试次数。"""

    max_attempts: int = 2


@dataclass(frozen=True, slots=True)
class AkShareConfig:
    """AkShare 无鉴权，仅保留占位以便统一配置。"""


@dataclass(frozen=True, slots=True)
class CacheConfig:
    max_bytes: int = 512 * 1024 * 1024
    max_entries: int = 4096
    max_entry_bytes: int = 64 * 1024 * 1024
    ttl: float | None = 6 * 3600
    copy_on_return: bool = False


@dataclass(frozen=True, slots=True)
class HubConfig:
    tushare: TushareConfig | None = None
    wind: WindConfig | None = None
    ifind: IfindConfig | None = None
    akshare: AkShareConfig = field(default_factory=AkShareConfig)
    fuyao: FuyaoConfig | None = None
    baostock: BaostockConfig = field(default_factory=BaostockConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    #: 按源覆盖限流配置（键为 source 值，如 "ifind"）；未覆盖时用默认表
    rate_limits: Mapping[str, RateLimitConfig] = field(default_factory=dict)
    #: 自定义限流器工厂：``(source, 生效配置) -> RateLimiter``；缺省用进程内令牌桶。
    #: 供平台注入跨进程共享限流（如基于 Redis 的固定窗口计数）。
    limiter_factory: Callable[[str, RateLimitConfig], RateLimiter] | None = field(
        default=None, compare=False, repr=False
    )
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    default_source: Source | None = None
