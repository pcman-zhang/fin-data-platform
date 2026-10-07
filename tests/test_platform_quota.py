"""TASK-3.19：跨进程限流与成本预算聚合测试。

覆盖：配置解析、共享限流一致性（多客户端/多线程不超配）、超时语义、
fail-open 回退与缓存恢复接管、预算回调聚合、Hub 装配缝、``/v1/usage``。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from fin_data_hub.enums import Source
from fin_data_hub.errors import RateLimitTimeout
from fin_data_hub.ratelimit import RateLimitConfig, RateLimiter
from fin_data_hub.usage import BudgetConfig, UsageLedger
from fin_data_platform.api.app import create_app
from fin_data_platform.api.deps import ApiContext
from fin_data_platform.cache.backend import CacheStats, InMemoryCache
from fin_data_platform.cache.layered import LayeredCache
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.ingestion.bootstrap import build_hub
from fin_data_platform.quota import (
    QuotaSettings,
    SharedRateLimiter,
    load_quota_settings,
    make_shared_budget,
    read_usage,
    usage_key,
)
from fin_data_platform.quota.config import (
    parse_budget_calls,
    parse_budget_cost,
    parse_rate_limits,
)
from fin_data_platform.quota.shared import RATE_KEY
from fin_data_platform.registry.reader import RegistryReader
from fin_data_platform.runtime.repository import SqlMetaRepository
from fin_data_platform.storage.config import StorageConfig


def _today() -> str:
    """当前 UTC 日期（每次调用求值，避免跨日 flaky）。"""
    return datetime.now(UTC).strftime("%Y-%m-%d")


class FlakyBackend:
    """可控故障后端：验证 fail-open 回退与缓存恢复接管。"""

    name = "flaky"

    def __init__(self) -> None:
        self.fail = False
        self.incrby_calls = 0
        self._inner = InMemoryCache()

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("backend down")

    def get(self, key: str) -> bytes | None:
        self._check()
        return self._inner.get(key)

    def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        self._check()
        self._inner.set(key, value, ttl=ttl)

    def delete(self, key: str) -> None:
        self._check()
        self._inner.delete(key)

    def incr(self, key: str, *, ttl: float | None = None) -> int:
        self._check()
        return self._inner.incr(key, ttl=ttl)

    def incrby(self, key: str, amount: int, *, ttl: float | None = None) -> int:
        self.incrby_calls += 1
        self._check()
        return self._inner.incrby(key, amount, ttl=ttl)

    def acquire_lock(self, key: str, token: str, *, ttl: float) -> bool:
        self._check()
        return self._inner.acquire_lock(key, token, ttl=ttl)

    def release_lock(self, key: str, token: str) -> None:
        self._check()
        self._inner.release_lock(key, token)

    def stats(self) -> CacheStats:
        return self._inner.stats()

    def close(self) -> None:
        return None


class FakeClock:
    """确定性时钟：``sleep`` 推进虚拟时间。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ------------------------------------------------------------------ 配置解析
def test_parse_rate_limits() -> None:
    limits = parse_rate_limits("tushare=1:2:30;baostock=0.5")
    assert limits["tushare"] == RateLimitConfig(rate=1.0, burst=2.0, timeout=30.0)
    assert limits["baostock"] == RateLimitConfig(rate=0.5, burst=None, timeout=None)
    assert parse_budget_calls("tushare=1000;baostock=500") == {
        "tushare": 1000,
        "baostock": 500,
    }
    assert parse_budget_cost("tushare=5.5") == {"tushare": 5.5}


@pytest.mark.parametrize(
    "raw,parser",
    [
        ("bad-entry", parse_rate_limits),
        ("tushare=0", parse_rate_limits),
        ("tushare=abc", parse_rate_limits),
        ("tushare=1:2:3:4", parse_rate_limits),
        ("tushare=0", parse_budget_calls),
        ("tushare=abc", parse_budget_calls),
        ("tushare=-1", parse_budget_cost),
        ("tushare=nan", parse_budget_cost),
        ("tushare=inf", parse_budget_cost),
        ("tushare=nan", parse_rate_limits),
        ("tushare=1:inf", parse_rate_limits),
    ],
)
def test_parse_quota_errors(raw: str, parser: Any) -> None:
    with pytest.raises(ValueError):
        parser(raw)


def test_load_quota_settings() -> None:
    env: Mapping[str, str] = {
        "FDP_RATE_LIMITS": "tushare=1:2",
        "FDP_BUDGET_CALLS": "tushare=1000",
        "FDP_BUDGET_COST": "tushare=5",
        "FDP_BUDGET_WARN_RATIO": "0.9",
        "FDP_SYNC_SOURCE": "tushare",
    }
    settings = load_quota_settings(env)
    assert dict(settings.rate_limits)["tushare"] == RateLimitConfig(rate=1.0, burst=2.0)
    assert dict(settings.budget.calls_per_day) == {"tushare": 1000}
    assert dict(settings.budget.cost_per_day) == {"tushare": 5.0}
    assert settings.budget.warn_ratio == 0.9
    assert settings.sources == ("tushare",)
    with pytest.raises(ValueError):
        load_quota_settings({"FDP_BUDGET_WARN_RATIO": "2"})
    with pytest.raises(ValueError):
        load_quota_settings({"FDP_RATE_LIMITS": "not-a-source=1"})


# ------------------------------------------------------------------ 共享限流
def test_shared_limiter_capacity_across_clients() -> None:
    cache = LayeredCache(InMemoryCache())
    config = RateLimitConfig(rate=5.0, burst=2.0)
    clients = [
        SharedRateLimiter(cache, source="tushare", config=config) for _ in range(8)
    ]
    results = [client.try_acquire() for client in clients]
    assert sum(results) == 2  # 多客户端共享同一窗口容量
    # 计数覆盖全部尝试（含被拒），保证跨进程总量可观测
    assert cache.read_counter(RATE_KEY.format(source="tushare")) == 8


def test_shared_limiter_concurrent_threads() -> None:
    cache = LayeredCache(InMemoryCache())
    limiter = SharedRateLimiter(
        cache, source="tushare", config=RateLimitConfig(rate=10.0, burst=5.0)
    )
    barrier = threading.Barrier(20)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        ok = limiter.try_acquire()
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(results) == 5
    assert cache.read_counter(RATE_KEY.format(source="tushare")) == 20


def test_shared_limiter_timeout() -> None:
    clock = FakeClock()
    cache = LayeredCache(InMemoryCache())
    limiter = SharedRateLimiter(
        cache,
        source="tushare",
        config=RateLimitConfig(rate=1.0, burst=1.0),
        clock=clock,
        sleep=clock.sleep,
    )
    limiter.acquire()
    with pytest.raises(RateLimitTimeout):
        limiter.acquire(timeout=0.05)
    assert limiter.tokens == 0.0


def test_shared_limiter_fail_open_and_recovery() -> None:
    backend = FlakyBackend()
    cache = LayeredCache(backend)
    backend.fail = True
    limiter = SharedRateLimiter(
        cache,
        source="tushare",
        config=RateLimitConfig(rate=5.0, burst=2.0),
        cooldown=0.0,  # 关闭冷却：验证缓存恢复立即接管
    )
    # 缓存不可用：回退进程内令牌桶，采集不被阻塞
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(RateLimitTimeout):
        limiter.acquire(timeout=0.01)
    # 缓存恢复：共享计数自动接管（新计数从 0 起）
    backend.fail = False
    limiter.acquire(timeout=1.0)
    assert cache.read_counter(RATE_KEY.format(source="tushare")) == 1


def test_layered_shared_counter_prefers_l2() -> None:
    l1, l2 = InMemoryCache(), InMemoryCache()
    cache = LayeredCache(l1, l2)
    assert cache.try_incrby("k", 2, ttl=60) == 2
    assert cache.read_counter("k") == 2
    assert l2.get("k") == b"2"
    assert l1.get("k") is None  # 共享计数以 L2 为权威


# ------------------------------------------------------------------ 预算聚合
def test_budget_callbacks_aggregate() -> None:
    cache = LayeredCache(InMemoryCache())
    base = BudgetConfig(
        calls_per_day={"tushare": 10},
        cost_per_day={"tushare": 100.0},
        cost_table={"tushare": 0.5},
        warn_ratio=0.8,
    )
    ledger = UsageLedger(make_shared_budget(cache, base))
    for _ in range(8):
        ledger.record("tushare", "daily", calls=1, latency_ms=1.0)

    usage = read_usage(
        cache, settings=QuotaSettings(budget=base, sources=("tushare",)), day=_today()
    )
    row = next(item for item in usage["sources"] if item["source"] == "tushare")
    assert usage["shared"] is True
    assert row["calls"] == 8
    assert row["cost"] == pytest.approx(4.0)
    assert row["calls_limit"] == 10
    assert row["calls_ratio"] == pytest.approx(0.8)
    # 派生告警（共享计数口径）与本地触发计数（审计）分离
    assert row["alerts"] == {"warn": True, "exceeded": False}
    assert row["fired"] == {"warn": 1, "exceeded": 0}

    # 继续累计到超限：派生告警升级，触发计数各进程独立累计
    for _ in range(2):
        ledger.record("tushare", "daily", calls=1, latency_ms=1.0)
    usage = read_usage(
        cache, settings=QuotaSettings(budget=base, sources=("tushare",)), day=_today()
    )
    row = next(item for item in usage["sources"] if item["source"] == "tushare")
    assert row["calls"] == 10
    assert row["alerts"] == {"warn": False, "exceeded": True}
    assert row["fired"] == {"warn": 1, "exceeded": 1}


def test_make_shared_budget_chains_base_callbacks() -> None:
    """共享回调必须链式调用基础回调（不吞掉调用方注入）。"""
    cache = LayeredCache(InMemoryCache())
    seen: list[str] = []
    base = BudgetConfig(
        calls_per_day={"tushare": 10},
        cost_table={"tushare": 1.0},
        warn_ratio=0.8,
        on_record=lambda record: seen.append("record"),
        on_alert=lambda alert: seen.append("alert"),
    )
    ledger = UsageLedger(make_shared_budget(cache, base))
    for _ in range(8):
        ledger.record("tushare", "daily", calls=1)
    assert seen.count("record") == 8
    assert seen.count("alert") == 1


def test_read_usage_fail_open() -> None:
    backend = FlakyBackend()
    cache = LayeredCache(backend)
    backend.fail = True
    usage = read_usage(cache, settings=QuotaSettings(), day=_today())
    assert usage["shared"] is False
    assert usage["sources"]  # 结构完整（用量按 0）
    assert all(row["calls"] == 0 for row in usage["sources"])

    assert read_usage(None, settings=QuotaSettings(), day=_today())["shared"] is False


# ------------------------------------------------------------------ Hub 装配
def test_build_hub_wires_shared_quota() -> None:
    cache = LayeredCache(InMemoryCache())
    env = {
        "TUSHARE_TOKEN": "test-token",
        "FDP_RATE_LIMITS": "tushare=1:1",
        "FDP_BUDGET_CALLS": "tushare=10",
    }
    hub = build_hub(env, cache=cache)
    limiter_set = hub._limiter_for(Source.TUSHARE)  # noqa: SLF001 - 装配断言
    assert isinstance(limiter_set.default, SharedRateLimiter)
    limiter_set.acquire("daily", timeout=1.0)
    assert cache.read_counter(RATE_KEY.format(source="tushare")) == 1
    assert hub.usage.budget.on_record is not None
    hub.usage.record("tushare", "daily", calls=1)
    assert cache.read_counter(usage_key(_today(), "tushare", "calls")) == 1


def test_build_hub_without_cache_uses_local_limiter() -> None:
    hub = build_hub({"TUSHARE_TOKEN": "test-token"})
    limiter_set = hub._limiter_for(Source.TUSHARE)  # noqa: SLF001 - 装配断言
    assert isinstance(limiter_set.default, RateLimiter)
    assert not isinstance(limiter_set.default, SharedRateLimiter)


# ------------------------------------------------------------------ API 端点
def _api_context(
    *, cache: LayeredCache | None, settings: QuotaSettings
) -> ApiContext:
    engine = create_engine("sqlite://")
    return ApiContext(
        config=StorageConfig(write_dsn="sqlite://"),
        writer_engine=engine,
        read_engine=engine,
        meta=SqlMetaRepository(engine),
        algorithms=InMemoryAlgorithmStore(),
        registry=RegistryReader(engine),
        specs=load_all(),
        factors=None,
        cache=cache,
        quota=settings,
    )


def test_usage_api_endpoint() -> None:
    cache = LayeredCache(InMemoryCache())
    cache.try_incrby(usage_key(_today(), "tushare", "calls"), 9, ttl=60)
    cache.try_incrby(usage_key(_today(), "tushare", "cost_micro"), 1_500_000, ttl=60)
    cache.try_incrby(usage_key(_today(), "tushare", "alerts.warn"), 1, ttl=60)
    settings = QuotaSettings(
        budget=BudgetConfig(calls_per_day={"tushare": 10}),
        rate_limits={"tushare": RateLimitConfig(rate=2.0, burst=2.0)},
        sources=("tushare",),
    )
    client = TestClient(create_app(_api_context(cache=cache, settings=settings), web_dist=None))
    response = client.get("/v1/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["shared"] is True
    row = next(item for item in body["sources"] if item["source"] == "tushare")
    assert row["calls"] == 9
    assert row["cost"] == pytest.approx(1.5)
    assert row["calls_limit"] == 10
    assert row["calls_ratio"] == pytest.approx(0.9)
    assert row["alerts"] == {"warn": True, "exceeded": False}
    assert row["fired"] == {"warn": 1, "exceeded": 0}
    assert row["rate_limit"] == {"rate": 2.0, "burst": 2.0}


def test_usage_api_without_cache() -> None:
    client = TestClient(
        create_app(_api_context(cache=None, settings=QuotaSettings()), web_dist=None)
    )
    body = client.get("/v1/usage").json()
    assert body["shared"] is False
    assert all(row["calls"] == 0 for row in body["sources"])


# ------------------------------------------------------------------ 窗口语义
def test_shared_limiter_window_scales_with_burst() -> None:
    """窗口缺省 capacity/rate：持续速率 ≈ rate（burst 大则窗口长）。"""
    cache = LayeredCache(InMemoryCache())
    slow = SharedRateLimiter(
        cache, source="baostock", config=RateLimitConfig(rate=0.5)
    )
    assert slow.window == pytest.approx(2.0)  # capacity=1 → 0.5/s
    bursty = SharedRateLimiter(
        cache, source="tushare", config=RateLimitConfig(rate=1.0, burst=10.0)
    )
    assert bursty.window == pytest.approx(10.0)  # 10 突发 / 10s → 1/s


def test_shared_limiter_window_advance() -> None:
    """窗口推进（TTL 过期）后可重新获取；cache 与 limiter 共用同一时钟。"""
    clock = FakeClock()
    cache = LayeredCache(InMemoryCache(clock=clock))
    limiter = SharedRateLimiter(
        cache,
        source="tushare",
        config=RateLimitConfig(rate=2.0, burst=2.0),
        clock=clock,
        sleep=clock.sleep,
        cooldown=0.0,
    )
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(RateLimitTimeout):
        limiter.acquire(timeout=0.05)
    clock.now += limiter.window + 0.001  # 窗口过期 → 计数归零
    limiter.acquire()
    assert cache.read_counter(RATE_KEY.format(source="tushare")) == 1


def test_shared_limiter_cooldown_short_circuits() -> None:
    """故障后短冷却：不再逐调用访问缓存；冷却结束自动重试共享路径。"""
    backend = FlakyBackend()
    backend.fail = True
    cache = LayeredCache(backend)
    clock = FakeClock()
    limiter = SharedRateLimiter(
        cache,
        source="tushare",
        config=RateLimitConfig(rate=10.0, burst=2.0),
        cooldown=5.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert limiter.try_acquire() is True  # 首次故障 → 回退本地（消耗 1/2）
    assert backend.incrby_calls == 1
    backend.fail = False
    clock.now += 1.0  # 冷却期内：即使缓存已恢复也不访问
    assert limiter.try_acquire() is True
    assert backend.incrby_calls == 1
    clock.now += 10.0  # 冷却结束 → 自动重试共享计数
    assert limiter.try_acquire() is True
    assert backend.incrby_calls == 2
    assert cache.read_counter(RATE_KEY.format(source="tushare")) == 1


def test_shared_limiter_try_acquire_over_capacity() -> None:
    """请求超容量：不消耗、不污染共享计数（与进程内令牌桶一致）。"""
    cache = LayeredCache(InMemoryCache())
    limiter = SharedRateLimiter(
        cache, source="tushare", config=RateLimitConfig(rate=2.0, burst=2.0)
    )
    assert limiter.try_acquire(5.0) is False
    assert cache.read_counter(RATE_KEY.format(source="tushare")) == 0


def test_layered_counter_l2_failure_does_not_touch_l1() -> None:
    """L2 故障时共享计数返回 None，且不得落到 L1（L1 非共享）。"""
    l1 = InMemoryCache()
    l2 = FlakyBackend()
    cache = LayeredCache(l1, l2)
    l2.fail = True
    assert cache.try_incrby("fdh:quota:rate:x", 1, ttl=60) is None
    assert cache.read_counter("fdh:quota:rate:x") is None
    assert l1.get("fdh:quota:rate:x") is None
