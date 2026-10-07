"""配额配置：环境变量 → 限流 / 预算。

环境变量（均可选；未设置 = 不启用）：

- ``FDP_RATE_LIMITS``：按源限流覆盖，``source=rate[:burst[:timeout]]``，
  条目以 ``;`` 或 ``,`` 分隔，如 ``"tushare=1:2;baostock=0.5"``；
- ``FDP_BUDGET_CALLS``：按源日调用预算，``source=int``，如 ``"tushare=1000"``；
- ``FDP_BUDGET_COST``：按源日成本预算，``source=float``，如 ``"tushare=5.0"``；
- ``FDP_BUDGET_WARN_RATIO``：告警阈值比例（默认 0.8，范围 (0, 1]）。

配置错误在启动/请求时**显式报错**（不静默忽略），避免预算未生效而不自知。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from fin_data_hub.enums import Source
from fin_data_hub.ratelimit import RateLimitConfig
from fin_data_hub.usage import BudgetConfig

_SEPARATOR = re.compile(r"[;,]")


@dataclass(frozen=True, slots=True)
class QuotaSettings:
    """平台配额配置（不含回调；回调由装配层注入）。"""

    rate_limits: Mapping[str, RateLimitConfig] = field(default_factory=dict)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    #: 配置涉及的源（限流 ∪ 预算 ∪ 运行时源），用于聚合查询枚举
    sources: tuple[str, ...] = ()


def _parse_pairs(raw: str, *, name: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in _SEPARATOR.split(raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"{name} 格式错误：{chunk!r}（应为 source=value，条目以 ; 分隔）"
            )
        source, _, value = chunk.partition("=")
        source, value = source.strip(), value.strip()
        if not source or not value:
            raise ValueError(f"{name} 格式错误：{chunk!r}（source/value 不能为空）")
        pairs.append((source, value))
    return pairs


def parse_rate_limits(raw: str) -> dict[str, RateLimitConfig]:
    """解析 ``FDP_RATE_LIMITS``（``source=rate[:burst[:timeout]]``）。"""
    result: dict[str, RateLimitConfig] = {}
    for source, value in _parse_pairs(raw, name="FDP_RATE_LIMITS"):
        parts = value.split(":")
        if len(parts) > 3:
            raise ValueError(
                f"FDP_RATE_LIMITS 格式错误：{source}={value}"
                "（应为 rate[:burst[:timeout]]）"
            )
        try:
            rate = float(parts[0])
            burst = float(parts[1]) if len(parts) >= 2 and parts[1] else None
            timeout = float(parts[2]) if len(parts) == 3 and parts[2] else None
        except ValueError as exc:
            raise ValueError(f"FDP_RATE_LIMITS 数值错误：{source}={value}") from exc
        for name, number in (("rate", rate), ("burst", burst), ("timeout", timeout)):
            if number is not None and not math.isfinite(number):
                raise ValueError(
                    f"FDP_RATE_LIMITS 的 {name} 必须为有限数值：{source}={number}"
                )
        if rate <= 0:
            raise ValueError(f"FDP_RATE_LIMITS 的 rate 必须为正数：{source}={rate}")
        if burst is not None and burst <= 0:
            raise ValueError(f"FDP_RATE_LIMITS 的 burst 必须为正数：{source}={burst}")
        if timeout is not None and timeout <= 0:
            raise ValueError(f"FDP_RATE_LIMITS 的 timeout 必须为正数：{source}={timeout}")
        result[source] = RateLimitConfig(rate=rate, burst=burst, timeout=timeout)
    return result


def parse_budget_calls(raw: str) -> dict[str, int]:
    """解析 ``FDP_BUDGET_CALLS``（``source=int``，正整数）。"""
    result: dict[str, int] = {}
    for source, value in _parse_pairs(raw, name="FDP_BUDGET_CALLS"):
        try:
            calls = int(value)
        except ValueError as exc:
            raise ValueError(f"FDP_BUDGET_CALLS 数值错误：{source}={value}") from exc
        if calls <= 0:
            raise ValueError(f"FDP_BUDGET_CALLS 必须为正整数：{source}={calls}")
        result[source] = calls
    return result


def parse_budget_cost(raw: str) -> dict[str, float]:
    """解析 ``FDP_BUDGET_COST``（``source=float``，正数）。"""
    result: dict[str, float] = {}
    for source, value in _parse_pairs(raw, name="FDP_BUDGET_COST"):
        try:
            cost = float(value)
        except ValueError as exc:
            raise ValueError(f"FDP_BUDGET_COST 数值错误：{source}={value}") from exc
        if not math.isfinite(cost):
            raise ValueError(f"FDP_BUDGET_COST 必须为有限数值：{source}={cost}")
        if cost <= 0:
            raise ValueError(f"FDP_BUDGET_COST 必须为正数：{source}={cost}")
        result[source] = cost
    return result


def load_quota_settings(env: Mapping[str, str] | None = None) -> QuotaSettings:
    """从环境变量加载配额配置（缺省全部为空 = 不启用）。"""
    source_env = os.environ if env is None else env
    rate_limits = parse_rate_limits(source_env.get("FDP_RATE_LIMITS", ""))
    calls = parse_budget_calls(source_env.get("FDP_BUDGET_CALLS", ""))
    cost = parse_budget_cost(source_env.get("FDP_BUDGET_COST", ""))
    warn_ratio_raw = source_env.get("FDP_BUDGET_WARN_RATIO", "").strip()
    if warn_ratio_raw:
        try:
            warn_ratio = float(warn_ratio_raw)
        except ValueError as exc:
            raise ValueError(
                f"FDP_BUDGET_WARN_RATIO 数值错误：{warn_ratio_raw}"
            ) from exc
        if not 0 < warn_ratio <= 1:
            raise ValueError(
                f"FDP_BUDGET_WARN_RATIO 必须在 (0, 1]：{warn_ratio}"
            )
    else:
        warn_ratio = 0.8
    budget = BudgetConfig(
        calls_per_day=dict(calls),
        cost_per_day=dict(cost),
        warn_ratio=warn_ratio,
    )
    configured = sorted({*rate_limits, *calls, *cost})
    for name in configured:
        try:
            Source(name)
        except ValueError as exc:
            raise ValueError(
                f"配额配置了未知数据源：{name!r}（须为 Hub 支持的源）"
            ) from exc
    sources = sorted(
        {
            *configured,
            source_env.get("FDP_SYNC_SOURCE", "").strip(),
            source_env.get("FDP_REGISTRY_SOURCE", "").strip(),
        }
        - {""}
    )
    return QuotaSettings(
        rate_limits=rate_limits, budget=budget, sources=tuple(sources)
    )
