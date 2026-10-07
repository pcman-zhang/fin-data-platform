"""平台缓存（TASK-3.9）单测：PIT 键 / 分层 / 代际失效 / 防击穿 / fail-open / 序列化。

Redis 后端以 fakeredis 验证机械语义；真 Redis 的锁与 INFO 见
``tests/test_integration_cache.py``（``-m integration``）。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime

import fakeredis
import pandas as pd
import pytest

from fin_data_platform.cache import (
    CACHE_FORMAT_VERSION,
    CacheStats,
    InMemoryCache,
    LayeredCache,
    RedisCache,
    bump_generation_of,
    cache_from_env,
    cache_key,
    decode,
    encode,
    generation_of,
    params_hash,
    pit_token,
    ttl_for_domain,
)
from fin_data_platform.cache.serialize import CacheFormatError
from fin_data_platform.storage.readers import cached_frame


def _fake_redis_cache(**kwargs) -> RedisCache:
    client = fakeredis.FakeRedis(decode_responses=False)
    return RedisCache("redis://fake/0", client=client, **kwargs)


def _layered(**kwargs) -> tuple[LayeredCache, InMemoryCache]:
    l1 = InMemoryCache()
    cache = LayeredCache(l1, _fake_redis_cache(), **kwargs)
    return cache, l1


# ------------------------------------------------------------------ 键与 PIT
def test_pit_token_requires_exactly_one_dimension() -> None:
    assert pit_token(as_of=date(2026, 9, 14), knowledge_time=None) == "asof:2026-09-14"
    assert (
        pit_token(as_of=None, knowledge_time=datetime(2026, 9, 14, 8, 30))
        == "kt:2026-09-14T08:30:00"
    )
    with pytest.raises(ValueError, match="必须携带 PIT"):
        pit_token(as_of=None, knowledge_time=None)
    with pytest.raises(ValueError, match="互斥"):
        pit_token(as_of=date(2026, 9, 14), knowledge_time=date(2026, 9, 14))


def test_cache_key_is_pit_safe_and_param_sensitive() -> None:
    key = cache_key(
        "cn_equity", "daily_bar", generation=3, as_of=date(2026, 9, 14), params={"b": 2, "a": 1}
    )
    assert key.startswith("fdh:cn_equity:daily_bar:g3:asof:2026-09-14:")
    # 参数排序无关；不同参数 → 不同键
    assert key == cache_key(
        "cn_equity", "daily_bar", generation=3, as_of=date(2026, 9, 14), params={"a": 1, "b": 2}
    )
    assert key != cache_key(
        "cn_equity", "daily_bar", generation=3, as_of=date(2026, 9, 14), params={"a": 2, "b": 2}
    )
    # 时间轴不同 → 键不同（防前视/陈旧混用）
    assert key != cache_key(
        "cn_equity", "daily_bar", generation=3, knowledge_time=date(2026, 9, 14)
    )
    # 代际不同 → 键不同
    assert key != cache_key("cn_equity", "daily_bar", generation=4, as_of=date(2026, 9, 14))


def test_params_hash_stable() -> None:
    assert params_hash({"a": 1}) == params_hash({"a": 1})
    assert params_hash(None) == params_hash({})
    assert len(params_hash({"a": 1})) == 16


def test_generation_roundtrip_in_memory() -> None:
    backend = InMemoryCache()
    assert generation_of(backend, "cn_equity") == 0
    assert bump_generation_of(backend, "cn_equity") == 1
    assert bump_generation_of(backend, "cn_equity") == 2
    assert generation_of(backend, "cn_equity") == 2


# ------------------------------------------------------------------ 内存后端
def test_inmemory_ttl_and_lru_eviction() -> None:
    now = {"t": 1000.0}
    backend = InMemoryCache(max_entries=2, clock=lambda: now["t"])
    backend.set("a", b"1", ttl=10)
    backend.set("b", b"2", ttl=10)
    assert backend.get("a") == b"1"
    now["t"] += 11
    assert backend.get("a") is None  # 过期
    backend.set("c", b"3", ttl=None)
    backend.set("d", b"4", ttl=None)  # 触发 LRU 淘汰
    assert backend.stats().evictions >= 1


def test_inmemory_lock_token_semantics() -> None:
    backend = InMemoryCache()
    assert backend.acquire_lock("lock", "t1", ttl=5) is True
    assert backend.acquire_lock("lock", "t2", ttl=5) is False
    backend.release_lock("lock", "t2")  # token 不匹配 → 不释放
    assert backend.acquire_lock("lock", "t3", ttl=5) is False
    backend.release_lock("lock", "t1")
    assert backend.acquire_lock("lock", "t4", ttl=5) is True


# ------------------------------------------------------------------ 序列化
def test_serialize_roundtrip_dataframe() -> None:
    frame = pd.DataFrame(
        {
            "entity_id": [10001, 10002],
            "trade_date": pd.to_datetime(["2026-09-11", "2026-09-12"]),
            "close": [1500.5, 1510.25],
        }
    )
    decoded = decode(encode(frame))
    pd.testing.assert_frame_equal(decoded, frame, check_dtype=False)


def test_serialize_roundtrip_small_objects() -> None:
    for value in ({"a": 1, "b": [1, 2]}, ["x", 1.5, None], "text", 42):
        assert decode(encode(value)) == value


def test_serialize_rejects_version_mismatch() -> None:
    payload = (
        json.dumps({"v": CACHE_FORMAT_VERSION + 1, "type": "json"}).encode()
        + b"\n"
        + b"null"
    )
    with pytest.raises(CacheFormatError, match="版本不兼容"):
        decode(payload)
    with pytest.raises(CacheFormatError):
        decode(b"not-a-cache-value")


# ------------------------------------------------------------------ 分层
def test_layered_hits_l1_then_l2() -> None:
    cache, l1 = _layered()
    key = cache.build_key("cn_equity", "daily_bar", as_of=date(2026, 9, 14))
    calls = {"n": 0}

    def loader() -> dict:
        calls["n"] += 1
        return {"rows": calls["n"]}

    assert cache.get_or_load(key, loader) == {"rows": 1}
    assert cache.get_or_load(key, loader) == {"rows": 1}  # L1 命中
    assert calls["n"] == 1

    l1.clear()  # L1 清空 → L2 命中
    assert cache.get_or_load(key, loader) == {"rows": 1}
    assert calls["n"] == 1
    stats = cache.stats()
    assert stats["layers"]["l2"]["hits"] >= 1


def test_layered_invalidate_domain_changes_key() -> None:
    cache, _ = _layered()
    key = cache.build_key("cn_equity", "daily_bar", as_of=date(2026, 9, 14))
    cache.get_or_load(key, lambda: "v1")
    assert cache.get_or_load(key, lambda: "v2") == "v1"

    cache.invalidate_domain("cn_equity")
    new_key = cache.build_key("cn_equity", "daily_bar", as_of=date(2026, 9, 14))
    assert new_key != key
    assert cache.get_or_load(new_key, lambda: "v2") == "v2"


def test_layered_cross_process_single_flight() -> None:
    """两个实例（各自 L1）共享同一 L2：loader 只执行一次。"""
    shared = fakeredis.FakeRedis(decode_responses=False)
    caches = [
        LayeredCache(InMemoryCache(), RedisCache("redis://fake/0", client=shared)),
        LayeredCache(InMemoryCache(), RedisCache("redis://fake/0", client=shared)),
    ]
    key = caches[0].build_key("cn_equity", "daily_bar", as_of=date(2026, 9, 14))
    calls = {"n": 0}
    start = threading.Barrier(2)

    def loader() -> str:
        calls["n"] += 1
        time.sleep(0.2)
        return "value"

    results: list[str] = []

    def worker(cache: LayeredCache) -> None:
        start.wait()
        results.append(cache.get_or_load(key, loader))

    threads = [threading.Thread(target=worker, args=(cache,)) for cache in caches]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["value", "value"]
    assert calls["n"] == 1  # 跨进程防击穿：只有一个执行


class _BrokenBackend:
    name = "broken"

    def get(self, key: str) -> bytes | None:
        raise RuntimeError("boom")

    def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        raise RuntimeError("boom")

    def delete(self, key: str) -> None:
        raise RuntimeError("boom")

    def incr(self, key: str, *, ttl: float | None = None) -> int:
        raise RuntimeError("boom")

    def acquire_lock(self, key: str, token: str, *, ttl: float) -> bool:
        raise RuntimeError("boom")

    def release_lock(self, key: str, token: str) -> None:
        raise RuntimeError("boom")

    def stats(self) -> CacheStats:
        return CacheStats()

    def close(self) -> None:
        raise RuntimeError("boom")


def test_layered_fail_open_on_backend_error() -> None:
    """L2 完全不可用：直查（loader 执行）且不等待锁超时。"""
    cache = LayeredCache(
        InMemoryCache(), _BrokenBackend(), lock_wait=5.0, poll_interval=0.01
    )
    started = time.monotonic()
    value = cache.get_or_load("key", lambda: "direct")
    elapsed = time.monotonic() - started
    assert value == "direct"
    assert elapsed < 1.0  # 未进入等待循环
    assert cache.stats()["layered"]["errors"] >= 3  # get/set/acquire 均 fail-open
    # 代际：L2 读取失败按 0；失效退化为本地推进且立即可见（L2 恢复后补齐）
    assert cache.generation("cn_equity") == 0
    assert cache.bump_generation("cn_equity") == 1
    assert cache.generation("cn_equity") == 1


def test_layered_format_error_treated_as_miss() -> None:
    cache, l1 = _layered()
    key = "fdh:cn_equity:daily_bar:g0:asof:2026-09-14:deadbeef"
    l1.set(key, b"garbage")
    assert cache.get_or_load(key, lambda: 7) == 7
    # 进入锁前与锁内各探测一次 → 计数可能为 2；关键是按 miss 处理而非抛错
    assert cache.stats()["layered"]["format_errors"] >= 1


# ------------------------------------------------------------------ 工厂与读取包装
def test_cache_from_env_disabled_without_redis_url() -> None:
    assert cache_from_env({}) is None
    cache = cache_from_env({"FDP_REDIS_URL": "redis://127.0.0.1:1/0"})
    assert cache is not None
    assert cache.l2 is not None
    cache.close()


def test_ttl_for_domain_override() -> None:
    env = {"FDP_CACHE_TTL": "3600", "FDP_CACHE_TTL_CN_EQUITY": "60"}
    assert ttl_for_domain("cn_equity", env=env) == 60
    assert ttl_for_domain("cn_fund", env=env) == 3600
    with pytest.raises(ValueError, match="必须为正数"):
        ttl_for_domain("cn_equity", env={"FDP_CACHE_TTL_CN_EQUITY": "0"})


def test_cached_frame_uses_cache() -> None:
    cache, _ = _layered()
    key = cache.build_key("cn_equity", "daily_bar", as_of=date(2026, 9, 14))
    calls = {"n": 0}

    def loader() -> pd.DataFrame:
        calls["n"] += 1
        return pd.DataFrame({"close": [1.0, 2.0]})

    first = cached_frame(cache, key, loader)
    second = cached_frame(cache, key, loader)
    assert calls["n"] == 1
    pd.testing.assert_frame_equal(first, second)


# ------------------------------------------------------------------ 评审回归
def test_stats_total_does_not_double_count_l1() -> None:
    cache, _ = _layered()
    key = cache.build_key("cn_equity", "daily_bar", as_of=date(2026, 9, 14))
    cache.get_or_load(key, lambda: "v")
    cache.get_or_load(key, lambda: "v")  # L1 命中
    stats = cache.stats()
    l1 = stats["layers"]["l1"]
    l2 = stats["layers"]["l2"]
    assert stats["total"]["hits"] == l1["hits"] + l2["hits"]
    assert stats["total"]["sets"] == l1["sets"] + l2["sets"]


def test_generation_monotonic_after_l2_recovery() -> None:
    """L2 不可用期间本地推进；L2 恢复后回写并保持单调。"""
    cache = LayeredCache(InMemoryCache(), _BrokenBackend())
    assert cache.bump_generation("cn_equity") == 1  # 仅本地
    good = _fake_redis_cache()
    cache._l2 = good  # 测试：模拟 L2 恢复
    assert cache.generation("cn_equity") == 1
    assert cache.bump_generation("cn_equity") == 2  # 本地 2 > L2 1 → 回写
    assert generation_of(good, "cn_equity") == 2


def test_ttl_resolver_applied_per_domain() -> None:
    l1 = InMemoryCache()
    l2 = _fake_redis_cache()
    cache = LayeredCache(
        l1, l2, default_ttl=3600, ttl_resolver=lambda domain: 60 if domain == "cn_equity" else 3600
    )
    assert cache.ttl_for("cn_equity") == 60
    assert cache.ttl_for("cn_fund") == 3600
    key = cache.build_key("cn_equity", "daily_bar", as_of=date(2026, 9, 14))
    cache.get_or_load(key, lambda: "v", domain="cn_equity")
    ttl = l2._client.ttl(key)
    assert 0 < ttl <= 60


def test_unsupported_type_is_not_cached() -> None:
    """不支持类型：序列化失败按不缓存处理（返回值一致，而非命中字符串化旧值）。"""
    cache, _ = _layered()
    calls = {"n": 0}

    def loader() -> dict:
        calls["n"] += 1
        return {"ts": pd.Timestamp("2026-09-14")}

    assert cache.get_or_load("k", loader) == {"ts": pd.Timestamp("2026-09-14")}
    assert cache.get_or_load("k", loader) == {"ts": pd.Timestamp("2026-09-14")}
    assert calls["n"] == 2  # 未缓存 → 每次直查
    assert cache.stats()["layered"]["errors"] >= 1


def test_arrow_roundtrip_preserves_index() -> None:
    frame = pd.DataFrame(
        {"close": [1.0, 2.0]}, index=pd.to_datetime(["2026-09-11", "2026-09-12"])
    )
    decoded = decode(encode(frame))
    pd.testing.assert_frame_equal(decoded, frame, check_dtype=False)
    assert decoded.index.equals(frame.index)


def test_inmemory_incr_ttl_semantics_match_redis() -> None:
    now = {"t": 1000.0}
    backend = InMemoryCache(clock=lambda: now["t"])
    assert backend.incr("gen", ttl=10) == 1
    now["t"] += 5
    assert backend.incr("gen", ttl=10) == 2  # TTL 不重置
    now["t"] += 6  # 已过期 → 从 0 重算
    assert backend.incr("gen", ttl=10) == 1


class _FlakyExpireClient:
    """首次 pexpire 抛错的 fakeredis 包装（验证 TTL 自愈）。"""

    def __init__(self, inner: fakeredis.FakeRedis) -> None:
        self._inner = inner
        self.fail_expire = True

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def pexpire(self, key: str, milliseconds: int) -> bool:
        if self.fail_expire:
            self.fail_expire = False
            raise ConnectionError("pexpire failed")
        return bool(self._inner.pexpire(key, milliseconds))


def test_redis_incrby_ttl_self_healing() -> None:
    """INCRBY 与 TTL 非原子：补设失败后下次自增必须自愈（毫秒精度）。"""
    inner = fakeredis.FakeRedis(decode_responses=False)
    backend = RedisCache("redis://fake/0", client=_FlakyExpireClient(inner))
    with pytest.raises(ConnectionError):
        backend.incrby("quota", 2, ttl=1.5)  # 自增成功但补设 TTL 失败 → 无 TTL
    assert int(inner.ttl("quota")) == -1
    assert backend.incrby("quota", 2, ttl=1.5) == 4  # 自愈：补设 TTL
    pttl = int(inner.pttl("quota"))
    assert 0 < pttl <= 1500  # 毫秒精度：非整数窗口不被截断到整秒
