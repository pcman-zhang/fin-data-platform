"""每源限流（令牌桶）与退避重试辅助。

限流与配额是两件事：本模块负责 QPS 限流（防封禁）；配额/成本计数见后续任务。
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from fin_data_hub.enums import Source
from fin_data_hub.errors import NetworkError, RateLimitError, RateLimitTimeout

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """限流配置。"""

    rate: float
    burst: float | None = None
    timeout: float | None = None


class RateLimiter:
    """线程安全令牌桶：``rate`` 为每秒补充令牌数（QPS）。"""

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        *,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate 必须为正数")
        self.rate = float(rate)
        self.capacity = float(burst) if burst is not None else max(1.0, float(rate))
        if self.capacity <= 0:
            raise ValueError("burst 必须为正数")
        self._tokens = self.capacity
        self._updated = time_fn()
        self._time_fn = time_fn
        self._cond = threading.Condition(threading.Lock())

    def acquire(self, tokens: float = 1.0, *, timeout: float | None = None) -> None:
        """获取令牌；超时抛 :class:`RateLimitTimeout`。"""
        if tokens > self.capacity:
            raise ValueError(f"请求令牌数 {tokens} 超过桶容量 {self.capacity}")
        deadline = None if timeout is None else self._time_fn() + timeout
        with self._cond:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
                if deadline is not None:
                    remaining = deadline - self._time_fn()
                    if remaining <= 0:
                        raise RateLimitTimeout(f"等待限流令牌超时（{timeout}s）")
                    wait = min(wait, remaining)
                self._cond.wait(max(wait, 1e-6))

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """非阻塞获取；无可用令牌立即返回 False。"""
        with self._cond:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def tokens(self) -> float:
        with self._cond:
            self._refill()
            return self._tokens

    def _refill(self) -> None:
        now = self._time_fn()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now


class RateLimiterSet:
    """源级默认限流 + endpoint 级覆盖（如不同接口限额不同）。"""

    def __init__(
        self,
        default: RateLimiter,
        endpoints: Mapping[str, RateLimiter] | None = None,
        *,
        timeout: float | None = None,
    ) -> None:
        self.default = default
        self.timeout = timeout
        self._endpoints = dict(endpoints or {})

    def acquire(
        self,
        endpoint: str | None = None,
        tokens: float = 1.0,
        *,
        timeout: float | None = None,
    ) -> None:
        effective = timeout if timeout is not None else self.timeout
        self.get(endpoint).acquire(tokens, timeout=effective)

    def get(self, endpoint: str | None = None) -> RateLimiter:
        if endpoint is not None and endpoint in self._endpoints:
            return self._endpoints[endpoint]
        return self.default


#: 各源默认限流（保守起步，可按账号/积分档通过 HubConfig.rate_limits 覆盖）。
DEFAULT_RATE_LIMITS: dict[Source, RateLimitConfig] = {
    Source.TUSHARE: RateLimitConfig(rate=2.0, burst=2.0, timeout=30.0),
    Source.AKSHARE: RateLimitConfig(rate=1.0, burst=1.0, timeout=30.0),
    Source.WIND: RateLimitConfig(rate=1.0, burst=1.0, timeout=30.0),
    Source.IFIND: RateLimitConfig(rate=2.0, burst=2.0, timeout=30.0),
    Source.FUYAO: RateLimitConfig(rate=2.0, burst=2.0, timeout=30.0),
    Source.BAOSTOCK: RateLimitConfig(rate=2.0, burst=2.0, timeout=30.0),
}


def default_rate_limit_config(source: Source | str) -> RateLimitConfig:
    """某源的默认限流配置（未登记源按 ``rate=1.0`` 保守处理）。"""
    return DEFAULT_RATE_LIMITS.get(Source(source), RateLimitConfig(rate=1.0))


def default_rate_limiter_set(source: Source | str) -> RateLimiterSet:
    """按默认表构建某源的限流器集合。"""
    config = default_rate_limit_config(source)
    return RateLimiterSet(
        RateLimiter(config.rate, config.burst), timeout=config.timeout
    )


def compute_backoff(
    attempt: int,
    *,
    base: float = 2.0,
    max_delay: float = 60.0,
    jitter: float = 0.5,
    rand: Callable[[], float] = random.random,
) -> float:
    """指数退避 + 抖动（``attempt`` 从 1 开始）。"""
    delay = min(max_delay, base * (2 ** max(0, attempt - 1)))
    factor = 1 + jitter * (2 * rand() - 1)
    return max(0.0, delay * factor)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    retry_on: tuple[type[BaseException], ...] = (RateLimitError, NetworkError),
    sleep_fn: Callable[[float], None] = time.sleep,
    base: float = 2.0,
    max_delay: float = 60.0,
    jitter: float = 0.5,
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> T:
    """带退避重试地执行 ``fn``；重试耗尽后抛出最后一次异常。"""
    if attempts < 1:
        raise ValueError("attempts 必须 >= 1")
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            delay = compute_backoff(
                attempt, base=base, max_delay=max_delay, jitter=jitter, rand=rand
            )
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep_fn(delay)
    assert last_error is not None
    raise last_error
