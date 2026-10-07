"""缓存后端抽象与实现（L1 进程内 / L2 Redis）。

- 值统一为字节（序列化见 :mod:`fin_data_platform.cache.serialize`）；
- 代际计数（``incr``）用于按域失效（免 SCAN）；
- 锁（``acquire_lock`` / ``release_lock``）用于跨进程 single-flight；
- 所有后端只做机械操作：**失败语义由 :class:`LayeredCache` 统一按 fail-open 处理**。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

try:  # pragma: no cover - 依赖缺失路径由 factory 负责提示
    import redis as _redis
except ImportError:  # pragma: no cover
    _redis = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class CacheStats:
    """后端统计快照（不可变；``merged`` 用于分层汇总）。"""

    hits: int = 0
    misses: int = 0
    sets: int = 0
    deletes: int = 0
    errors: int = 0
    lock_waits: int = 0
    lock_timeouts: int = 0
    bytes_written: int = 0
    evictions: int = 0
    memory_bytes: int | None = None
    extra: dict[str, float] = field(default_factory=dict)

    def merged(self, *others: CacheStats) -> CacheStats:
        total = self
        for other in others:
            extra = dict(total.extra)
            extra.update(other.extra)
            memory = (
                other.memory_bytes
                if other.memory_bytes is not None
                else total.memory_bytes
            )
            total = replace(
                total,
                hits=total.hits + other.hits,
                misses=total.misses + other.misses,
                sets=total.sets + other.sets,
                deletes=total.deletes + other.deletes,
                errors=total.errors + other.errors,
                lock_waits=total.lock_waits + other.lock_waits,
                lock_timeouts=total.lock_timeouts + other.lock_timeouts,
                bytes_written=total.bytes_written + other.bytes_written,
                evictions=total.evictions + other.evictions,
                memory_bytes=memory,
                extra=extra,
            )
        return total


@runtime_checkable
class CacheBackend(Protocol):
    """缓存后端协议（字节值 + 代际计数 + 互斥锁）。"""

    name: str

    def get(self, key: str) -> bytes | None: ...

    def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def incr(self, key: str, *, ttl: float | None = None) -> int: ...

    def incrby(self, key: str, amount: int, *, ttl: float | None = None) -> int: ...

    def acquire_lock(self, key: str, token: str, *, ttl: float) -> bool: ...

    def release_lock(self, key: str, token: str) -> None: ...

    def stats(self) -> CacheStats: ...

    def close(self) -> None: ...


class NullCache:
    """空实现：永远 miss（用于显式关闭缓存或测试基线）。"""

    name = "null"

    def get(self, key: str) -> bytes | None:
        return None

    def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        return None

    def delete(self, key: str) -> None:
        return None

    def incr(self, key: str, *, ttl: float | None = None) -> int:
        return 0

    def incrby(self, key: str, amount: int, *, ttl: float | None = None) -> int:
        return 0

    def acquire_lock(self, key: str, token: str, *, ttl: float) -> bool:
        return False

    def release_lock(self, key: str, token: str) -> None:
        return None

    def stats(self) -> CacheStats:
        return CacheStats()

    def close(self) -> None:
        return None


class InMemoryCache:
    """进程内缓存：TTL + LRU（条数 / 字节双约束）+ 进程内锁。"""

    name = "memory"

    def __init__(
        self,
        *,
        max_entries: int = 4096,
        max_bytes: int = 256 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[str, tuple[float | None, bytes]] = OrderedDict()
        self._locks: dict[str, tuple[str, float]] = {}
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._clock = clock
        self._bytes = 0
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._deletes = 0
        self._errors = 0
        self._evictions = 0
        self._bytes_written = 0

    # -------------------------------------------------------------- 读写
    def get(self, key: str) -> bytes | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, value = entry
            if expires_at is not None and expires_at <= self._clock():
                del self._entries[key]
                self._bytes -= len(value)
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        expires_at = None if ttl is None else self._clock() + ttl
        with self._lock:
            if key in self._entries:
                self._bytes -= len(self._entries[key][1])
            self._entries[key] = (expires_at, value)
            self._entries.move_to_end(key)
            self._bytes += len(value)
            self._sets += 1
            self._bytes_written += len(value)
            self._evict_locked()

    def delete(self, key: str) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._bytes -= len(entry[1])
                self._deletes += 1

    def incr(self, key: str, *, ttl: float | None = None) -> int:
        """自增 1（语义对齐 Redis INCR：TTL 仅首次设置、跨自增保留；过期视为 0）。"""
        return self.incrby(key, 1, ttl=ttl)

    def incrby(self, key: str, amount: int, *, ttl: float | None = None) -> int:
        """自增 ``amount``（语义对齐 Redis INCRBY）。"""
        with self._lock:
            entry = self._entries.get(key)
            now = self._clock()
            expires_at: float | None = None
            current = 0
            if entry is not None:
                self._bytes -= len(entry[1])
                if entry[0] is not None and entry[0] <= now:
                    del self._entries[key]  # 已过期 → 从 0 重算
                else:
                    current = int(entry[1])
                    expires_at = entry[0]
            value = current + amount
            if current == 0 and ttl is not None:
                expires_at = now + ttl
            payload = str(value).encode("utf-8")
            self._entries[key] = (expires_at, payload)
            self._entries.move_to_end(key)
            self._bytes += len(payload)
            return value

    # -------------------------------------------------------------- 锁
    def acquire_lock(self, key: str, token: str, *, ttl: float) -> bool:
        with self._lock:
            holder = self._locks.get(key)
            now = self._clock()
            if holder is not None and holder[1] > now:
                return False
            self._locks[key] = (token, now + ttl)
            return True

    def release_lock(self, key: str, token: str) -> None:
        with self._lock:
            holder = self._locks.get(key)
            if holder is not None and holder[0] == token:
                del self._locks[key]

    # -------------------------------------------------------------- 统计
    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                sets=self._sets,
                deletes=self._deletes,
                errors=self._errors,
                bytes_written=self._bytes_written,
                evictions=self._evictions,
                memory_bytes=self._bytes,
                extra={"entries": float(len(self._entries))},
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._locks.clear()
            self._bytes = 0

    def close(self) -> None:
        self.clear()

    def _evict_locked(self) -> None:
        while self._entries and (
            len(self._entries) > self._max_entries or self._bytes > self._max_bytes
        ):
            _, (_, value) = self._entries.popitem(last=False)
            self._bytes -= len(value)
            self._evictions += 1


class RedisCache:
    """Redis L2 缓存：TTL、代际计数与跨进程锁。

    仅负责机械操作；连接异常直接抛出，由 :class:`LayeredCache` 按 fail-open 处理。
    """

    name = "redis"

    def __init__(
        self,
        url: str,
        *,
        client: Any | None = None,
        socket_timeout: float = 1.0,
        info_interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if client is None:
            if _redis is None:  # pragma: no cover - 依赖提示
                raise ImportError(
                    "RedisCache 需要 redis 依赖：pip install 'fin-data-platform[cache]'"
                )
            client = _redis.Redis.from_url(
                url, socket_timeout=socket_timeout, decode_responses=False
            )
        self._client = client
        self._info_interval = info_interval
        self._clock = clock
        self._info: dict[str, float] = {}
        self._info_at = 0.0
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._deletes = 0
        self._bytes_written = 0

    # -------------------------------------------------------------- 读写
    def get(self, key: str) -> bytes | None:
        value = self._client.get(key)
        if value is None:
            self._misses += 1
            return None
        self._hits += 1
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)  # pragma: no cover - 客户端返回非字节/字符串时兜底

    def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        if ttl is None:
            self._client.set(key, value)
        else:
            self._client.set(key, value, px=max(1, int(ttl * 1000)))
        self._sets += 1
        self._bytes_written += len(value)

    def delete(self, key: str) -> None:
        self._client.delete(key)
        self._deletes += 1

    def incr(self, key: str, *, ttl: float | None = None) -> int:
        value = int(self._client.incr(key))
        if ttl is not None:
            self._ensure_ttl(key, ttl)
        return value

    def incrby(self, key: str, amount: int, *, ttl: float | None = None) -> int:
        value = int(self._client.incrby(key, int(amount)))
        if ttl is not None:
            self._ensure_ttl(key, ttl)
        return value

    def _ensure_ttl(self, key: str, ttl: float) -> None:
        """补设缺失的 TTL（毫秒精度，与 ``set`` 的 ``px`` 口径一致）。

        INCR/INCRBY 与 EXPIRE 非原子：进程崩溃或断连可能留下无 TTL 的计数键
        （限流键无 TTL 将永久锁死）。每次自增后检查，缺失即补设（幂等自愈）。
        用 ``pexpire`` 保留亚秒窗口（如 ``burst/rate = 1.5s``）的精度。
        """
        if int(self._client.ttl(key)) < 0:  # -1 无过期；-2 键不存在（极端竞态）
            self._client.pexpire(key, max(1, int(ttl * 1000)))

    # -------------------------------------------------------------- 锁
    def acquire_lock(self, key: str, token: str, *, ttl: float) -> bool:
        result = self._client.set(key, token, nx=True, px=max(1, int(ttl * 1000)))
        return bool(result)

    def release_lock(self, key: str, token: str) -> None:
        """释放锁（仅当持有者 token 匹配；WATCH/MULTI 保证原子）。"""
        with self._client.pipeline() as pipe:
            try:
                pipe.watch(key)
                current = pipe.get(key)
                if isinstance(current, bytes):
                    current_text = current.decode("utf-8", "replace")
                else:
                    current_text = str(current) if current is not None else ""
                if current_text == token:
                    pipe.multi()
                    pipe.delete(key)
                    pipe.execute()
            finally:
                pipe.reset()

    # -------------------------------------------------------------- 统计
    def stats(self) -> CacheStats:
        extra = dict(self._info_snapshot())
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            sets=self._sets,
            deletes=self._deletes,
            bytes_written=self._bytes_written,
            memory_bytes=int(extra.pop("used_memory", 0)) or None,
            extra=extra,
        )

    def info(self) -> dict[str, float]:
        """Redis INFO 快照（内存 / 淘汰等；按 ``info_interval`` 节流）。"""
        return dict(self._info_snapshot())

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    def _info_snapshot(self) -> dict[str, float]:
        """INFO 快照（节流；不可用时返回空——指标尽力而为，不影响缓存语义）。"""
        now = self._clock()
        if self._info and now - self._info_at < self._info_interval:
            return self._info
        try:
            raw = self._client.info("memory")
            stats = self._client.info("stats")
        except Exception:  # noqa: BLE001 - 指标失败不影响缓存
            self._info = {}
            self._info_at = now
            return {}
        self._info = {
            "used_memory": float(raw.get("used_memory", 0)),
            "maxmemory": float(raw.get("maxmemory", 0)),
            "evicted_keys": float(stats.get("evicted_keys", 0)),
            "keyspace_hits": float(stats.get("keyspace_hits", 0)),
            "keyspace_misses": float(stats.get("keyspace_misses", 0)),
        }
        self._info_at = now
        return self._info
