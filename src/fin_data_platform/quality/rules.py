"""质量规则 → 违规行查询（TASK-3.5；规则语义见 doc-11 §3.4）。

统一约定：每条规则编译为「**违规行查询**」（返回样例行的 ``SELECT``），执行器用同一
查询做计数与样例提取——避免每类规则各写一套统计 SQL；样例由此天然携带窗口与键位。

读取口径（与读层一致）：

- **值检查**（not_null / range / enum / expression / jump / reconcile）在
  「每个业务键的最新版本」上执行（``storage.readers.latest_query``）——被后续版本修正的
  历史值不参与判定，跳变序列也因此对同一键唯一；
- **unique** 检查物理键（字典声明口径），在原始表上执行；
- 数据集缺 ``business_key`` / ``knowledge_time`` / ``version`` 时退回原表（兼容性兜底）。

结构性跳过（``query=None`` + ``skip_reason``）：键列缺失、目标表未落地（如 ``raw.*``）、
无可对账共享键等；执行器记录 ``status=skipped``，不判失败。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import (
    Float,
    MetaData,
    Select,
    Table,
    cast,
    func,
    or_,
    select,
    tuple_,
)
from sqlalchemy.sql import ColumnElement

from fin_data_platform.dictionary.models import DatasetSpec, QualityRule
from fin_data_platform.quality.expr import compile_expression
from fin_data_platform.quality.models import FAMILY_RECONCILE, FAMILY_RULE
from fin_data_platform.storage.readers import latest_query


@dataclass(frozen=True, slots=True)
class PlannedRule:
    """一条已编译的规则（或结构性跳过项）。"""

    check_id: str
    family: str
    severity: str
    sample_columns: tuple[str, ...] = ()
    query: Select[Any] | None = None
    skip_reason: str | None = None
    note: str = ""


def event_column(spec: DatasetSpec, table: Table) -> str | None:
    """数据集的事件时间列（按字典 ``pit_role=event_time`` 唯一登记）。"""
    for field in spec.fields:
        if field.pit_role == "event_time" and field.name in table.c:
            return field.name
    return None


def latest_source(spec: DatasetSpec, table: Table) -> Any:
    """值检查的读取口径：每个业务键取最新版本（不可用时退回原表）。"""
    if not spec.business_key or any(name not in table.c for name in spec.business_key):
        return table
    if "knowledge_time" not in table.c or "version" not in table.c:
        return table
    return latest_query(table, key_columns=spec.business_key).subquery("latest_rows")


def plan_rules(
    spec: DatasetSpec,
    table: Table,
    *,
    metadata: MetaData,
    window: tuple[date, date] | None,
) -> list[PlannedRule]:
    """把字典声明的规则逐条编译为可执行检查（顺序即字典声明顺序）。"""
    event = event_column(spec, table)
    latest = latest_source(spec, table)
    plans: list[PlannedRule] = []
    for index, rule in enumerate(spec.quality or [], start=1):
        family = FAMILY_RECONCILE if rule.rule == "reconcile" else FAMILY_RULE
        label = rule.against if rule.rule == "reconcile" else rule.rule
        check_id = f"{family}:{label}#{index}"
        severity = rule.severity or "warn"
        source = table if rule.rule == "unique" else latest
        try:
            plan = _plan(
                spec,
                source,
                rule,
                check_id=check_id,
                family=family,
                severity=severity,
                metadata=metadata,
                window=window,
                event=event,
            )
        except ValueError as exc:
            plan = PlannedRule(
                check_id=check_id,
                family=family,
                severity=severity,
                skip_reason=f"规则编译失败: {exc}",
            )
        plans.append(plan)
    return plans


def _window_predicate(
    source: Any, event: str | None, window: tuple[date, date] | None
) -> ColumnElement[bool] | None:
    if event is None or window is None:
        return None
    return source.c[event].between(window[0], window[1])


def _conditions(
    source: Any, event: str | None, window: tuple[date, date] | None
) -> list[ColumnElement[bool]]:
    predicate = _window_predicate(source, event, window)
    return [predicate] if predicate is not None else []


def _sample_columns(spec: DatasetSpec, source: Any, extra: Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    for name in [*spec.business_key, *extra]:
        if name in source.c and name not in names:
            names.append(name)
    return tuple(names)


def _plan(
    spec: DatasetSpec,
    source: Any,
    rule: QualityRule,
    *,
    check_id: str,
    family: str,
    severity: str,
    metadata: MetaData,
    window: tuple[date, date] | None,
    event: str | None,
) -> PlannedRule:
    if rule.rule == "unique":
        return _plan_unique(spec, source, rule, check_id, family, severity, window, event)
    if rule.rule == "not_null":
        return _plan_not_null(spec, source, rule, check_id, family, severity, window, event)
    if rule.rule == "range":
        return _plan_range(spec, source, rule, check_id, family, severity, window, event)
    if rule.rule == "enum":
        return _plan_enum(spec, source, rule, check_id, family, severity, window, event)
    if rule.rule == "expression":
        return _plan_expression(spec, source, rule, check_id, family, severity, window, event)
    if rule.rule == "jump":
        return _plan_jump(spec, source, rule, check_id, family, severity, window, event)
    if rule.rule == "reconcile":
        return _plan_reconcile(
            spec, source, rule, check_id, family, severity, metadata, window, event
        )
    if rule.rule == "freshness":
        raise ValueError("freshness 规则暂未实现（时效性检查由 update_sla 承担）")
    raise ValueError(f"暂不支持的规则种类: {rule.rule}")


def _plan_unique(
    spec: DatasetSpec,
    source: Any,
    rule: QualityRule,
    check_id: str,
    family: str,
    severity: str,
    window: tuple[date, date] | None,
    event: str | None,
) -> PlannedRule:
    keys = tuple(rule.keys or ())
    if not keys or any(key not in source.c for key in keys):
        return PlannedRule(check_id, family, severity, skip_reason=f"键列缺失: {keys}")
    columns = [source.c[key] for key in keys]
    duplicate = (
        select(*columns)
        .where(*_conditions(source, event, window))
        .group_by(*columns)
        .having(func.count() > 1)
    )
    query = select(*[source.c[name] for name in keys]).where(
        *_conditions(source, event, window),
        tuple_(*columns).in_(duplicate),
    )
    return PlannedRule(check_id, family, severity, sample_columns=keys, query=query)


def _plan_not_null(
    spec: DatasetSpec,
    source: Any,
    rule: QualityRule,
    check_id: str,
    family: str,
    severity: str,
    window: tuple[date, date] | None,
    event: str | None,
) -> PlannedRule:
    fields = tuple(rule.fields or ())
    missing = [name for name in fields if name not in source.c]
    if not fields or missing:
        return PlannedRule(check_id, family, severity, skip_reason=f"字段缺失: {missing or fields}")
    columns = _sample_columns(spec, source, fields)
    query = select(*[source.c[name] for name in columns]).where(
        *_conditions(source, event, window),
        or_(*[source.c[name].is_(None) for name in fields]),
    )
    return PlannedRule(check_id, family, severity, sample_columns=columns, query=query)


def _plan_range(
    spec: DatasetSpec,
    source: Any,
    rule: QualityRule,
    check_id: str,
    family: str,
    severity: str,
    window: tuple[date, date] | None,
    event: str | None,
) -> PlannedRule:
    if rule.field is None or rule.field not in source.c:
        return PlannedRule(check_id, family, severity, skip_reason=f"字段缺失: {rule.field}")
    if rule.min is None and rule.max is None:
        return PlannedRule(check_id, family, severity, skip_reason="range 未提供 min/max")
    bounds: list[ColumnElement[bool]] = []
    if rule.min is not None:
        bounds.append(source.c[rule.field] < rule.min)
    if rule.max is not None:
        bounds.append(source.c[rule.field] > rule.max)
    columns = _sample_columns(spec, source, [rule.field])
    query = select(*[source.c[name] for name in columns]).where(
        *_conditions(source, event, window),
        source.c[rule.field].is_not(None),
        or_(*bounds),
    )
    return PlannedRule(check_id, family, severity, sample_columns=columns, query=query)


def _plan_enum(
    spec: DatasetSpec,
    source: Any,
    rule: QualityRule,
    check_id: str,
    family: str,
    severity: str,
    window: tuple[date, date] | None,
    event: str | None,
) -> PlannedRule:
    if rule.field is None or rule.field not in source.c:
        return PlannedRule(check_id, family, severity, skip_reason=f"字段缺失: {rule.field}")
    values = tuple(rule.values or ())
    if not values:
        return PlannedRule(check_id, family, severity, skip_reason="enum 未提供取值")
    columns = _sample_columns(spec, source, [rule.field])
    query = select(*[source.c[name] for name in columns]).where(
        *_conditions(source, event, window),
        source.c[rule.field].is_not(None),
        source.c[rule.field].not_in(values),
    )
    return PlannedRule(check_id, family, severity, sample_columns=columns, query=query)


def _plan_expression(
    spec: DatasetSpec,
    source: Any,
    rule: QualityRule,
    check_id: str,
    family: str,
    severity: str,
    window: tuple[date, date] | None,
    event: str | None,
) -> PlannedRule:
    clause, referenced = compile_expression(
        rule.expr or "", {column.name: column for column in source.c}
    )
    columns = _sample_columns(spec, source, list(referenced))
    query = select(*[source.c[name] for name in columns]).where(
        *_conditions(source, event, window),
        ~clause,
    )
    return PlannedRule(
        check_id,
        family,
        severity,
        sample_columns=columns,
        query=query,
        note=rule.expr or "",
    )


def _plan_jump(
    spec: DatasetSpec,
    source: Any,
    rule: QualityRule,
    check_id: str,
    family: str,
    severity: str,
    window: tuple[date, date] | None,
    event: str | None,
) -> PlannedRule:
    field = rule.field
    if field is None or field not in source.c:
        return PlannedRule(check_id, family, severity, skip_reason=f"字段缺失: {field}")
    if event is None:
        return PlannedRule(check_id, family, severity, skip_reason="数据集无事件时间列")
    if not spec.business_key or spec.business_key[0] not in source.c:
        return PlannedRule(check_id, family, severity, skip_reason="缺少分组键（business_key）")
    partition = source.c[spec.business_key[0]]
    columns = _sample_columns(spec, source, [field])
    previous = (
        func.lag(source.c[field])
        .over(partition_by=partition, order_by=source.c[event])
        .label("_previous")
    )
    lagged = (
        select(*[source.c[name] for name in columns], previous)
        .where(*_conditions(source, event, window))
        .subquery()
    )
    # 显式浮点除法：整数列的相对变化不得被整除截断
    change = cast(func.abs(lagged.c[field] - lagged.c["_previous"]), Float) / cast(
        func.abs(lagged.c["_previous"]), Float
    )
    query = select(*[lagged.c[name] for name in columns]).where(
        lagged.c[field].is_not(None),
        lagged.c["_previous"].is_not(None),
        lagged.c["_previous"] != 0,
        change > rule.max_ratio,
    )
    return PlannedRule(
        check_id,
        family,
        severity,
        sample_columns=columns,
        query=query,
        note=f"max_ratio={rule.max_ratio}",
    )


def _plan_reconcile(
    spec: DatasetSpec,
    source: Any,
    rule: QualityRule,
    check_id: str,
    family: str,
    severity: str,
    metadata: MetaData,
    window: tuple[date, date] | None,
    event: str | None,
) -> PlannedRule:
    target_name = rule.against or ""
    target = metadata.tables.get(target_name)
    if target is None:
        return PlannedRule(
            check_id, family, severity, skip_reason=f"对账目标未落地: {target_name}"
        )
    shared = [
        name
        for name in spec.business_key
        if name in source.c and name in target.c
    ]
    if not shared:
        return PlannedRule(
            check_id, family, severity, skip_reason=f"与 {target_name} 无共享业务键"
        )
    matched = (
        select(1)
        .select_from(target)
        .where(*[target.c[name] == source.c[name] for name in shared])
    )
    if "is_open" in target.c:
        matched = matched.where(target.c.is_open.is_(True))
    columns = _sample_columns(spec, source, shared)
    query = select(*[source.c[name] for name in columns]).where(
        *_conditions(source, event, window),
        *[source.c[name].is_not(None) for name in shared],
        ~matched.exists(),
    )
    return PlannedRule(
        check_id,
        family,
        severity,
        sample_columns=columns,
        query=query,
        note=f"共享键: {', '.join(shared)}",
    )
