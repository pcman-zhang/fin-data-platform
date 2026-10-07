"""跨进程共享限流：固定窗口计数 + fail-open。

算法：每个源一个共享计数键（``fdh:quota:rate:{source}``），窗口内 ``INCRBY``
原子自增；计数不超过容量则放行，超限则短睡眠轮询等待窗口推进，超时抛
:class:`~fin_data_hub.errors.RateLimitTimeout`。窗口 TTL 由自增补设（见
``RedisCache._ensure_ttl``，崩溃/断连后可自愈）。

窗口长度缺省 ``capacity / rate``：窗口内持续速率 ≈ ``rate``（``burst == rate``
时为 1s；``burst`` 更大则窗口更长、允许突发但平滑到平均速率）。被拒尝试也
计入窗口计数（保证放行数 ≤ 容量），故计数读数含重试放大，仅用于容量控制。

fail-open：共享缓存不可用（``try_incrby`` 返回 ``None``）时回退进程内令牌桶，
并按 ``cooldown`` 短路后续共享尝试（避免黑洞故障逐调用叠加超时）；冷却期结束
后自动重试共享路径（无粘滞状态，恢复即接管）。
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Any

from fin_data_hub.errors import RateLimitTimeout
from fin_data_hub.ratelimit import RateLimitConfig, RateLimiter
from fin_data_platform.cache.layered import LayeredCache

logger = logging.getLogger("fin_data_platform.quota")

#: 共享限流键模板（窗口计数）
RATE_KEY = "fdh:quota:rate:{source}"

#: 等待窗口推进的轮询间隔（秒）
_POLL_INTERVAL = 0.01


class SharedRateLimiter:
    """基于共享缓存固定窗口计数的限流器（多进程按源共享总量）。

    与 :class:`fin_data_hub.ratelimit.RateLimiter` 接口兼容（``acquire`` /
    ``try_acquire`` / ``tokens``），由 ``RateLimiterSet`` 包装使用。
    """

    def __init__(
        self,
        cache: LayeredCache,
        *,
        source: str,
        config: RateLimitConfig,
        window: float | None = None,
        cooldown: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.rate <= 0:
            raise ValueError("rate 必须为正数")
        self.source = source
        self.rate = float(config.rate)
        self.capacity = (
            float(config.burst)
            if config.burst is not None
            else max(1.0, float(config.rate))
        )
        if self.capacity <= 0:
            raise ValueError("burst 必须为正数")
        self.window = (
            float(window) if window is not None else self.capacity / self.rate
        )
        if self.window <= 0:
            raise ValueError("window 必须为正数")
        self._cache = cache
        self._clock = clock
        self._sleep = sleep
        self._cooldown = max(0.0, float(cooldown))
        self._retry_at = 0.0
        self._key = RATE_KEY.format(source=source)
        self._fallback = RateLimiter(config.rate, config.burst)

    def acquire(self, tokens: float = 1.0, *, timeout: float | None = None) -> None:
        """获取令牌；超时抛 ``RateLimitTimeout``；缓存不可用回退进程内限流。"""
        if tokens > self.capacity:
            raise ValueError(f"请求令牌数 {tokens} 超过桶容量 {self.capacity}")
        amount = max(1, math.ceil(tokens))
        if amount > self.capacity:
            raise ValueError(f"请求令牌数 {tokens} 超过桶容量 {self.capacity}")
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            count = self._try_count(amount)
            if count is None:
                # fail-open：共享计数不可用 → 进程内令牌桶（采集不阻塞）
                self._fallback.acquire(tokens, timeout=self._remaining(deadline, timeout))
                return
            if count <= self.capacity:
                return
            remaining = self._remaining(deadline, timeout)
            if remaining is not None and remaining <= 0:
                raise RateLimitTimeout(f"等待限流令牌超时（{timeout}s）")
            self._sleep(_POLL_INTERVAL)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """非阻塞获取；无可用令牌或缓存不可用（回退进程内）时按结果返回。"""
        amount = max(1, math.ceil(tokens))
        if amount > self.capacity:
            # 与进程内令牌桶一致：请求超容量不消耗、不计数
            return False
        count = self._try_count(amount)
        if count is None:
            return self._fallback.try_acquire(tokens)
        return count <= self.capacity

    @property
    def tokens(self) -> float:
        """窗口剩余容量（近似）；缓存不可用/冷却期回退进程内桶余量。"""
        if self._clock() < self._retry_at:
            return self._fallback.tokens
        count = self._cache.read_counter(self._key)
        if count is None:
            return self._fallback.tokens
        return max(0.0, self.capacity - count)

    # ------------------------------------------------------------------ 内部
    def _try_count(self, amount: int) -> int | None:
        """共享窗口计数；冷却期内或缓存故障返回 ``None``（调用方 fail-open）。"""
        if self._clock() < self._retry_at:
            return None
        count = self._cache.try_incrby(self._key, amount, ttl=self.window)
        if count is None:
            self._retry_at = self._clock() + self._cooldown
        return count

    def _remaining(self, deadline: float | None, timeout: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - self._clock())


def shared_limiter_factory(
    cache: LayeredCache,
) -> Callable[[str, RateLimitConfig], Any]:
    """构造共享限流器工厂（``HubConfig.limiter_factory`` 接口）。

    返回类型为 ``Any``：Hub 侧按鸭子类型使用（``acquire`` / ``try_acquire`` /
    ``tokens``），不要求继承进程内 ``RateLimiter``。
    """

    def factory(source: str, config: RateLimitConfig) -> Any:
        return SharedRateLimiter(cache, source=source, config=config)

    return factory
