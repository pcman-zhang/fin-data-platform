"""Runtime 装配（TASK-3.6 切片 3）：配置驱动的任务注册与运行环境组装。

```python
settings = SyncSettings.from_env()
app = build_sync_runtime(RuntimeConfig.from_env(), settings)  # 未配置时返回空 registry
app.readiness()  # fail fast
app.start()
```
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import date
from typing import Any

from sqlalchemy import Engine

from fin_data_platform.cache import LayeredCache, cache_from_env
from fin_data_platform.ingestion.settings import SyncSettings
from fin_data_platform.ingestion.tasks import (
    register_adj_factor_task,
    register_daily_bar_task,
    register_daily_status_task,
)
from fin_data_platform.quota import (
    load_quota_settings,
    make_shared_budget,
    shared_limiter_factory,
)
from fin_data_platform.runtime.app import RuntimeApp
from fin_data_platform.runtime.calendar import HubTradeCalendar
from fin_data_platform.runtime.config import RuntimeConfig
from fin_data_platform.runtime.registry import TaskRegistry
from fin_data_platform.runtime.repository import SqlMetaRepository
from fin_data_platform.runtime.windows import WatermarkWindowProvider
from fin_data_platform.storage.engine import create_write_engine

logger = logging.getLogger("fin_data_platform.ingestion")


def supports_capability(hub: Any, source: str, capability: str) -> bool:
    """源适配器是否声明指定能力（无 registry 的测试桩默认视为支持）。"""

    from fin_data_hub.capabilities import Capability

    registry = getattr(hub, "registry", None)
    if registry is None:
        return True
    try:
        adapter = registry.get(source)
    except Exception:
        return False
    return Capability(capability) in getattr(adapter, "capabilities", frozenset())


def build_hub(
    env: Mapping[str, str] | None = None, *, cache: LayeredCache | None = None
) -> Any:
    """按环境凭证构建 FinDataHub（缺凭证的源会被跳过）。

    ``cache`` 非空时启用平台级配额：限流升级为跨进程共享固定窗口计数，
    预算回调写入共享计数（多进程口径，``/v1/usage`` 可查）；缓存不可用时
    自动 fail-open 回退进程内限额。
    """
    from fin_data_hub import FinDataHub, HubConfig
    from fin_data_hub.config import TushareConfig

    source_env = env if env is not None else os.environ
    token = source_env.get("TUSHARE_TOKEN") or source_env.get("FIN_DATA_HUB_TUSHARE_TOKEN")
    tushare = TushareConfig(token=token) if token else None
    quota = load_quota_settings(source_env)
    budget = quota.budget
    limiter_factory = None
    if cache is not None:
        budget = make_shared_budget(cache, budget)
        limiter_factory = shared_limiter_factory(cache)
    return FinDataHub.from_config(
        HubConfig(
            tushare=tushare,
            rate_limits=quota.rate_limits,
            budget=budget,
            limiter_factory=limiter_factory,
        )
    )


def build_sync_runtime(
    config: RuntimeConfig,
    settings: SyncSettings | None,
    *,
    hub: Any = None,
    engine: Engine | None = None,
    env: Mapping[str, str] | None = None,
    cache: LayeredCache | None = None,
    registry: TaskRegistry | None = None,
) -> RuntimeApp:
    """组装 Runtime：日线同步任务（按配置的代码清单）+ 日历 + 水位窗口。

    缓存按环境装配（``FDP_REDIS_URL``；未配置则仅进程内或禁用），
    同步成功后按域失效（代际递增）。
    """
    engine = engine or create_write_engine(config.storage)
    repository = SqlMetaRepository(engine)
    registry = registry if registry is not None else TaskRegistry()
    active_cache = cache if cache is not None else cache_from_env(env)
    due_provider = None
    if settings is not None:
        hub = hub or build_hub(env, cache=active_cache)
        factor_enabled = supports_capability(hub, settings.source, "adjust_factors")
        if not factor_enabled:
            logger.warning("数据源 %s 未声明复权因子能力，跳过因子同步任务注册", settings.source)
        status_enabled = supports_capability(hub, settings.source, "market_events")
        if not status_enabled:
            logger.warning("数据源 %s 未声明市场事件能力，跳过每日状态任务注册", settings.source)
        start_dates: dict[str, date] = {}
        for code in settings.codes:
            spec = register_daily_bar_task(
                registry,
                engine,
                hub,
                code=code,
                source=settings.source,
                schedule=settings.schedule,
                cache=active_cache,
            )
            start_dates[spec.job_id] = settings.start
            if factor_enabled:
                factor_spec = register_adj_factor_task(
                    registry,
                    engine,
                    hub,
                    code=code,
                    source=settings.source,
                    schedule=settings.schedule,
                    cache=active_cache,
                )
                start_dates[factor_spec.job_id] = settings.start
            if status_enabled:
                status_spec = register_daily_status_task(
                    registry,
                    engine,
                    hub,
                    code=code,
                    source=settings.source,
                    schedule=settings.schedule,
                    cache=active_cache,
                )
                start_dates[status_spec.job_id] = settings.start
        calendar = HubTradeCalendar(hub, source=settings.source)
        due_provider = WatermarkWindowProvider(repository, calendar, start_dates=start_dates)
    return RuntimeApp(
        config,
        engine=engine,
        repository=repository,
        registry=registry,
        due_provider=due_provider,
    )
