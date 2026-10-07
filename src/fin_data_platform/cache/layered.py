"""分层缓存：L1（进程内）→ L2（Redis），fail-open + 跨进程 single-flight。

语义（doc-10 §3.4 缓存非权威）：

- **fail-open**：任何后端异常按 miss 处理并计数，调用方直查权威层；
- **single-flight**：进程内锁（线程）+ L2 锁（跨进程）；等待超时回退自算；
- **代际失效**：``invalidate_domain`` 推进域代际（键含代际 → 旧键自然失效）；
- **PIT 键**：``build_key`` 内置代际与 PIT 维度校验。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from fin_data_platform.cache.backend import CacheBackend, CacheStats
from fin_data_platform.cache.keys import (
    bump_generation_of,
    cache_key,
    generation_of,
    lock_key,
    set_generation_of,
)
from fin_data_platform.cache.serialize import CacheFormatError, decode, encode

logger = logging.getLogger("fin_data_platform.cache")

DEFAULT_TTL = 6 * 3600.0


class LayeredCache:
    """L1/L2 分层缓存（可只配 L1）。"""

    def __init__(
        self,
        l1: CacheBackend,
        l2: CacheBackend | None = None,
        *,
        default_ttl: float = DEFAULT_TTL,
        ttl_resolver: Callable[[str], float] | None = None,
        lock_ttl: float = 30.0,
        lock_wait: float = 10.0,
        poll_interval: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._l1 = l1
        self._l2 = l2
        self._default_ttl = default_ttl
        self._ttl_resolver = ttl_resolver
        self._lock_ttl = lock_ttl
        self._lock_wait = lock_wait
        self._poll_interval = poll_interval
        self._clock = clock
        self._locks: weakref.WeakValueDictionary[str, threading.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._locks_guard = threading.Lock()
        self._lock_waits = 0
        self._lock_timeouts = 0
        self._errors = 0
        self._format_errors = 0

    # -------------------------------------------------------------- 键与代际
    @property
    def l1(self) -> CacheBackend:
        return self._l1

    @property
    def l2(self) -> CacheBackend | None:
        return self._l2

    def _generation_backend(self) -> CacheBackend:
        return self._l2 if self._l2 is not None else self._l1

    def generation(self, domain: str) -> int:
        """读取域代际：``max(L1, L2)``（任一后端失败按 0；宁可多 miss 不读旧值）。"""
        values: list[int] = []
        for backend in (self._l1, self._l2):
            if backend is None:
                continue
            try:
                values.append(generation_of(backend, domain))
            except Exception as exc:  # noqa: BLE001 - fail-open
                self._note_error(f"generation[{backend.name}]", exc)
        return max(values) if values else 0

    def bump_generation(self, domain: str) -> int:
        """推进域代际（同步完成后的按域失效；单调，L2 恢复后自动补齐）。"""
        local = bump_generation_of(self._l1, domain)
        l2 = self._l2
        if l2 is None:
            return local
        remote: int | None = None
        try:
            remote = bump_generation_of(l2, domain)
        except Exception as exc:  # noqa: BLE001 - fail-open（本地已推进）
            self._note_error("bump_generation[l2]", exc)
        if remote is None:
            return local
        if local > remote:
            # L2 落后（曾不可用）→ 补齐到本地值，保证代际单调
            try:
                set_generation_of(l2, domain, local)
            except Exception as exc:  # noqa: BLE001 - fail-open
                self._note_error("sync_generation[l2]", exc)
            return local
        if remote > local:
            try:
                set_generation_of(self._l1, domain, remote)
            except Exception as exc:  # noqa: BLE001 - fail-open
                self._note_error("sync_generation[l1]", exc)
        return max(local, remote)

    def ttl_for(self, domain: str | None) -> float:
        """生效 TTL：``domain`` 解析器 > 默认。"""
        if domain and self._ttl_resolver is not None:
            try:
                return self._ttl_resolver(domain)
            except Exception as exc:  # noqa: BLE001 - fail-open（退回默认 TTL）
                self._note_error("ttl_resolver", exc)
        return self._default_ttl

    def invalidate_domain(self, domain: str) -> int:
        """按域失效：推进代际（旧键含旧代际，自然失效）。"""
        logger.info("缓存域失效: domain=%s", domain)
        return self.bump_generation(domain)

    def build_key(
        self,
        domain: str,
        panel: str,
        *,
        as_of: date | datetime | str | None = None,
        knowledge_time: date | datetime | str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> str:
        """构造 PIT 安全缓存键（含当前域代际）。"""
        return cache_key(
            domain,
            panel,
            generation=self.generation(domain),
            as_of=as_of,
            knowledge_time=knowledge_time,
            params=params,
        )

    # -------------------------------------------------------------- 读写
    def get_or_load(
        self,
        key: str,
        loader: Callable[[], Any],
        *,
        ttl: float | None = None,
        domain: str | None = None,
    ) -> Any:
        """命中返回缓存值；否则 single-flight 执行 ``loader`` 并回填。

        ``ttl`` 未指定时按 ``domain`` 解析（``FDP_CACHE_TTL_<DOMAIN>``），
        无解析器则用默认 TTL。
        """
        effective_ttl = self.ttl_for(domain) if ttl is None else ttl
        hit, value = self._lookup(key)
        if hit:
            return value

        with self._process_lock(key):
            hit, value = self._lookup(key)
            if hit:
                return value
            acquired: bool | None = True
            token = uuid.uuid4().hex
            if self._l2 is not None:
                acquired = self._acquire(lock_key(key), token)
            if acquired is False:
                self._lock_waits += 1
                deadline = self._clock() + self._lock_wait
                while self._clock() < deadline:
                    time.sleep(self._poll_interval)
                    hit, value = self._lookup(key)
                    if hit:
                        return value
                self._lock_timeouts += 1
            try:
                value = loader()
                self._store(key, value, effective_ttl)
                return value
            finally:
                if acquired is True and self._l2 is not None:
                    self._release(lock_key(key), token)

    def invalidate(self, key: str) -> None:
        """删除单个键（L1 + L2；代际失效优先）。"""
        self._l1.delete(key)
        if self._l2 is not None:
            try:
                self._l2.delete(key)
            except Exception as exc:  # noqa: BLE001 - fail-open
                self._note_error("invalidate", exc)

    # -------------------------------------------------------------- 共享计数
    def try_incr(self, key: str, *, ttl: float | None = None) -> int | None:
        """共享计数自增 1（L2 优先；无 L2 退化为 L1；异常 → ``None``）。"""
        return self.try_incrby(key, 1, ttl=ttl)

    def try_incrby(self, key: str, amount: int, *, ttl: float | None = None) -> int | None:
        """共享计数自增 ``amount``；失败返回 ``None``（调用方 fail-open）。

        计数以 L2 为权威（跨进程）；无 L2 时退化为进程内计数（单进程语义）。
        """
        backend = self._l2 if self._l2 is not None else self._l1
        try:
            return int(backend.incrby(key, amount, ttl=ttl))
        except Exception as exc:  # noqa: BLE001 - fail-open
            self._note_error(f"incrby[{backend.name}]", exc)
            return None

    def read_counter(self, key: str) -> int | None:
        """读取共享计数（键不存在视为 0）；失败返回 ``None``（调用方 fail-open）。"""
        backend = self._l2 if self._l2 is not None else self._l1
        try:
            raw = backend.get(key)
        except Exception as exc:  # noqa: BLE001 - fail-open
            self._note_error(f"get[{backend.name}]", exc)
            return None
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # -------------------------------------------------------------- 统计
    def stats(self) -> dict[str, Any]:
        layered = CacheStats(
            errors=self._errors,
            lock_waits=self._lock_waits,
            lock_timeouts=self._lock_timeouts,
        )
        result: dict[str, Any] = {
            "layers": {},
            "layered": {
                "errors": layered.errors,
                "lock_waits": layered.lock_waits,
                "lock_timeouts": layered.lock_timeouts,
                "format_errors": self._format_errors,
                "l1+hits": self._l1.stats().hits,
                "l2": self._l2.name if self._l2 is not None else None,
            },
        }
        total = CacheStats()
        for name, backend in (("l1", self._l1), ("l2", self._l2)):
            if backend is None:
                continue
            stats = backend.stats()
            result["layers"][name] = {
                "backend": backend.name,
                "hits": stats.hits,
                "misses": stats.misses,
                "sets": stats.sets,
                "evictions": stats.evictions,
                "errors": stats.errors,
                "bytes_written": stats.bytes_written,
                "memory_bytes": stats.memory_bytes,
                **stats.extra,
            }
            total = total.merged(stats)
        result["total"] = {
            "hits": total.hits,
            "misses": total.misses,
            "sets": total.sets,
            "evictions": total.evictions,
            "bytes_written": total.bytes_written,
            "memory_bytes": total.memory_bytes,
        }
        return result

    def close(self) -> None:
        for backend in (self._l1, self._l2):
            if backend is None:
                continue
            try:
                backend.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不抛
                self._note_error("close", exc)

    # -------------------------------------------------------------- 内部
    def _lookup(self, key: str) -> tuple[bool, Any]:
        for backend in (self._l1, self._l2):
            if backend is None:
                continue
            try:
                raw = backend.get(key)
            except Exception as exc:  # noqa: BLE001 - fail-open
                self._note_error(f"get[{backend.name}]", exc)
                continue
            if raw is None:
                continue
            try:
                return True, decode(raw)
            except (CacheFormatError, ValueError) as exc:
                self._format_errors += 1
                self._note_error(f"decode[{backend.name}]", exc)
                continue
        return False, None

    def _store(self, key: str, value: Any, ttl: float) -> None:
        try:
            payload = encode(value)
        except Exception as exc:  # noqa: BLE001 - 序列化失败不缓存
            self._note_error("encode", exc)
            return
        try:
            self._l1.set(key, payload, ttl=ttl)
        except Exception as exc:  # noqa: BLE001 - fail-open
            self._note_error("set[l1]", exc)
        if self._l2 is not None:
            try:
                self._l2.set(key, payload, ttl=ttl)
            except Exception as exc:  # noqa: BLE001 - fail-open
                self._note_error("set[l2]", exc)

    def _process_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _acquire(self, key: str, token: str) -> bool | None:
        """尝试获取 L2 锁；``None`` 表示后端异常（调用方应直接自算）。"""
        assert self._l2 is not None
        try:
            return self._l2.acquire_lock(key, token, ttl=self._lock_ttl)
        except Exception as exc:  # noqa: BLE001 - fail-open
            self._note_error("acquire_lock", exc)
            return None

    def _release(self, key: str, token: str) -> None:
        assert self._l2 is not None
        try:
            self._l2.release_lock(key, token)
        except Exception as exc:  # noqa: BLE001 - fail-open
            self._note_error("release_lock", exc)

    def _note_error(self, op: str, exc: BaseException) -> None:
        self._errors += 1
        logger.warning("缓存 fail-open（%s）: %s: %s", op, type(exc).__name__, exc)
