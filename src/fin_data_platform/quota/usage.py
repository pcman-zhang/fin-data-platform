"""预算与成本聚合：回调写入共享计数 + 按源聚合查询。

- 写入：``BudgetConfig.on_record`` → 调用数 / 成本（微单位整数）共享计数；
  ``on_alert`` → 告警触发计数（日志由 Hub 台账统一输出）；
- 读取：:func:`read_usage` 按源聚合当日用量（多进程共享口径）：
  ``alerts`` 由共享计数与预算**派生**（多进程一致）；``fired`` 为各进程本地
  告警触发次数合计（审计口径）；缓存不可用时 ``shared=False``（fail-open）。

计数为可重建的运行时状态（权威数据在 PostgreSQL），TTL 保留 3 天。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fin_data_hub.enums import Source
from fin_data_hub.ratelimit import RateLimitConfig, default_rate_limit_config
from fin_data_hub.usage import BudgetAlert, BudgetConfig, UsageRecord
from fin_data_platform.cache.layered import LayeredCache
from fin_data_platform.quota.config import QuotaSettings

logger = logging.getLogger("fin_data_platform.quota")

#: 用量计数键模板
USAGE_KEY = "fdh:quota:usage:{day}:{source}:{metric}"

#: 计数保留时长（秒）：3 天（覆盖跨日查询窗口）
USAGE_TTL = 3 * 24 * 3600.0

_METRIC_CALLS = "calls"
_METRIC_COST = "cost_micro"
_ALERT_WARN = "alerts.warn"
_ALERT_EXCEEDED = "alerts.exceeded"

#: 成本换算：微单位（1e-6）整数计数，避免浮点累计误差
_COST_SCALE = 1_000_000


def usage_key(day: str, source: str, metric: str) -> str:
    """用量计数键（按日 / 源 / 指标）。"""
    return USAGE_KEY.format(day=day, source=source, metric=metric)


def make_shared_budget(cache: LayeredCache, base: BudgetConfig) -> BudgetConfig:
    """在基础预算配置上注入共享计数回调（保留并链式调用基础回调）。"""
    base_on_record = base.on_record
    base_on_alert = base.on_alert

    def on_record(record: UsageRecord) -> None:
        cache.try_incrby(
            usage_key(record.day, record.source, _METRIC_CALLS),
            max(1, int(record.calls)),
            ttl=USAGE_TTL,
        )
        micro = int(round(record.est_cost * _COST_SCALE))
        if micro > 0:
            cache.try_incrby(
                usage_key(record.day, record.source, _METRIC_COST),
                micro,
                ttl=USAGE_TTL,
            )
        if base_on_record is not None:
            try:
                base_on_record(record)
            except Exception:  # noqa: BLE001 - 回调异常不影响主流程
                logger.exception("共享预算 on_record 基础回调执行失败")

    def on_alert(alert: BudgetAlert) -> None:
        # 告警日志由 Hub 台账统一输出；此处仅做跨进程触发计数（审计）
        cache.try_incrby(
            usage_key(alert.day, alert.source, f"alerts.{alert.level}"),
            1,
            ttl=USAGE_TTL,
        )
        if base_on_alert is not None:
            try:
                base_on_alert(alert)
            except Exception:  # noqa: BLE001 - 回调异常不影响主流程
                logger.exception("共享预算 on_alert 基础回调执行失败")

    return BudgetConfig(
        calls_per_day=base.calls_per_day,
        cost_per_day=base.cost_per_day,
        cost_table=base.cost_table,
        warn_ratio=base.warn_ratio,
        on_record=on_record,
        on_alert=on_alert,
    )


def read_usage(
    cache: LayeredCache | None,
    *,
    settings: QuotaSettings,
    day: str | None = None,
) -> dict[str, Any]:
    """按源聚合当日用量（共享缓存口径）。

    枚举范围为 Hub 已知源 ∪ 配置涉及的源；``alerts`` 由共享计数与预算派生
    （多进程一致），``fired`` 为本地告警触发次数合计；缓存未配置或任一计数
    读取失败则 ``shared=False``（fail-open，字段按 0 返回）。
    """
    effective_day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    sources = sorted({source.value for source in Source} | set(settings.sources))
    warn_ratio = settings.budget.warn_ratio
    shared = True
    rows: list[dict[str, Any]] = []
    for source in sources:
        if cache is None:
            calls = cost_micro = warn = exceeded = None
        else:
            calls = cache.read_counter(usage_key(effective_day, source, _METRIC_CALLS))
            cost_micro = cache.read_counter(
                usage_key(effective_day, source, _METRIC_COST)
            )
            warn = cache.read_counter(usage_key(effective_day, source, _ALERT_WARN))
            exceeded = cache.read_counter(
                usage_key(effective_day, source, _ALERT_EXCEEDED)
            )
        if None in (calls, cost_micro, warn, exceeded):
            shared = False
        calls_value = calls or 0
        cost_value = (cost_micro or 0) / _COST_SCALE
        calls_limit = settings.budget.calls_per_day.get(source)
        cost_limit = settings.budget.cost_per_day.get(source)
        alert_warn = alert_exceeded = False
        for value, limit in ((calls_value, calls_limit), (cost_value, cost_limit)):
            if not limit:
                continue
            if value >= limit:
                alert_exceeded = True
            elif value >= limit * warn_ratio:
                alert_warn = True
        rate_config: RateLimitConfig | None = settings.rate_limits.get(source)
        if rate_config is None:
            try:
                rate_config = default_rate_limit_config(source)
            except ValueError:
                rate_config = None
        rows.append(
            {
                "source": source,
                "calls": calls_value,
                "cost": round(cost_value, 6),
                "calls_limit": calls_limit,
                "cost_limit": cost_limit,
                "calls_ratio": (
                    round(calls_value / calls_limit, 4) if calls_limit else None
                ),
                "cost_ratio": (
                    round(cost_value / cost_limit, 4) if cost_limit else None
                ),
                "alerts": {"warn": alert_warn, "exceeded": alert_exceeded},
                "fired": {"warn": warn or 0, "exceeded": exceeded or 0},
                "rate_limit": (
                    {"rate": rate_config.rate, "burst": rate_config.burst}
                    if rate_config is not None
                    else None
                ),
            }
        )
    return {
        "day": effective_day,
        "shared": shared,
        "warn_ratio": warn_ratio,
        "sources": rows,
    }
