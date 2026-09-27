"""质量扫描执行器（TASK-3.5）：规则 / 完整性 / 时效性 / 引用对账 / 跨源对账。

- **扫描窗口**：``[window_end - lookback 个交易日, window_end]``（日历来自
  ``ref.trade_calendar``；日历未落地时退化为自然日回看）；
- **规则检查**：字典声明的规则逐条执行（见 :mod:`fin_data_platform.quality.rules`）；
- **完整性**：期望（在市 × 交易日，停牌感知）vs 实际，输出覆盖率与断点；
- **时效性**：``update_sla`` + 最新数据日 → 滞后交易日数；
- **引用对账**：``reconcile against`` 参照表（共享键存在性）；
- **跨源对账**：小样本实拉（见 :mod:`fin_data_platform.quality.reconcile`）。

区间型数据集（同时含 ``start_date`` / ``end_date``，如 ``listing_lifecycle``）：
规则扫描不做事件窗口过滤；完整性仅在配置期望范围（codes）时执行（区间覆盖判定）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, MetaData, Table, and_, func, or_, select, true

from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.quality import reconcile
from fin_data_platform.quality.models import (
    FAMILY_COMPLETENESS,
    FAMILY_FRESHNESS,
    FAMILY_RULE,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    CheckResult,
    QualityScanResult,
)
from fin_data_platform.quality.rules import (
    PlannedRule,
    event_column,
    latest_source,
    plan_rules,
)
from fin_data_platform.storage.schema import build_metadata

logger = logging.getLogger(__name__)

_SAMPLE_LIMIT = 20
_KEY_PREFERENCE = ("entity_id", "exchange_id", "issuer_id")
#: 复权因子对账回看（自然日）：除权事件稀疏，过短窗口会退化为 0–1 个样本点
_FACTOR_LOOKBACK_DAYS = 365


@lru_cache(maxsize=1)
def _metadata() -> MetaData:
    """平台全量 metadata（进程内缓存：构建开销较大）。"""
    return build_metadata()[0]


def run_quality_scan(
    engine: Engine,
    *,
    datasets: Sequence[str],
    window_end: date,
    lookback_days: int = 10,
    codes: Sequence[str] = (),
    reconcile_codes: Sequence[str] = (),
    hub: Any = None,
    coverage_min: float = 0.99,
    reconcile_lookback: int = 30,
    close_tolerance: float = 0.01,
    factor_tolerance: float = 1e-4,
    specs: dict[str, DatasetSpec] | None = None,
) -> QualityScanResult:
    """执行一轮质量扫描（不写库；写入由任务执行器负责）。"""
    dictionary = specs if specs is not None else load_all()
    metadata = _metadata()
    results: list[CheckResult] = []
    with engine.connect() as connection:
        window, open_days = _resolve_window(connection, metadata, window_end, lookback_days)
        reconcile_window, _ = _resolve_window(
            connection, metadata, window_end, reconcile_lookback
        )
        scope_ids = _scope_entity_ids(connection, metadata, codes)
        for dataset in datasets:
            spec = dictionary.get(dataset)
            if spec is None:
                results.append(_error_result(dataset, window, "数据集未登记"))
                continue
            table = metadata.tables.get(spec.storage.canonical_table)
            if table is None:
                results.append(_error_result(dataset, window, "canonical 表未落地"))
                continue
            interval = _interval_dataset(table)
            event = event_column(spec, table)
            scan_window = None if (interval or event is None) else window
            source = latest_source(spec, table)
            try:
                rows_checked = _row_count(connection, source, event, scan_window)
                plans = plan_rules(spec, table, metadata=metadata, window=scan_window)
            except Exception as exc:  # 规划 / 计数失败：整数据集记 error，不中断其余
                results.append(_error_result(dataset, window, f"{type(exc).__name__}: {exc}"))
                continue
            for planned in plans:
                try:
                    results.append(
                        _execute_rule(
                            connection,
                            planned,
                            dataset=dataset,
                            window=window,
                            rows_checked=rows_checked,
                        )
                    )
                except Exception as exc:  # 单条规则失败：记 error，其余照常
                    results.append(
                        _error_result(
                            dataset,
                            window,
                            f"{type(exc).__name__}: {exc}",
                            check_id=planned.check_id,
                            family=planned.family,
                            severity=planned.severity,
                        )
                    )
            try:
                completeness = _completeness(
                    connection,
                    spec=spec,
                    table=table,
                    dictionary=dictionary,
                    metadata=metadata,
                    window=window,
                    open_days=open_days,
                    scope_ids=scope_ids,
                    scope_requested=bool(codes),
                    min_ratio=coverage_min,
                )
            except Exception as exc:
                completeness = _error_result(
                    dataset,
                    window,
                    f"completeness: {type(exc).__name__}: {exc}",
                    check_id=FAMILY_COMPLETENESS,
                    family=FAMILY_COMPLETENESS,
                )
            if completeness is not None:
                results.append(completeness)
            try:
                freshness = _freshness(
                    connection, spec=spec, table=table, window=window, open_days=open_days
                )
            except Exception as exc:
                freshness = _error_result(
                    dataset,
                    window,
                    f"freshness: {type(exc).__name__}: {exc}",
                    check_id=FAMILY_FRESHNESS,
                    family=FAMILY_FRESHNESS,
                    severity="warn",
                )
            if freshness is not None:
                results.append(freshness)
    start, end = reconcile_window
    if not reconcile_codes:
        results.extend(
            reconcile.skipped_pair(start, end, "未配置对账样本（FDP_QUALITY_RECONCILE_CODES）")
        )
    elif hub is None:
        results.extend(reconcile.skipped_pair(start, end, "hub 未装配（无同步源配置）"))
    else:
        results.extend(
            reconcile.cross_source_checks(
                hub,
                codes=list(reconcile_codes),
                window_start=start,
                window_end=end,
                close_tolerance=close_tolerance,
                factor_tolerance=factor_tolerance,
                factor_window_start=end - timedelta(days=_FACTOR_LOOKBACK_DAYS),
            )
        )
    return QualityScanResult(window_start=window[0], window_end=window[1], results=results)


def _resolve_window(
    connection: Any, metadata: MetaData, window_end: date, lookback_days: int
) -> tuple[tuple[date, date], list[date]]:
    """解析扫描窗口：最近 ``lookback_days`` 个交易日（无日历时用自然日回看）。"""
    days: list[date] = []
    calendar = metadata.tables.get("ref.trade_calendar")
    if calendar is not None:
        statement = (
            select(calendar.c.trade_date)
            .where(calendar.c.is_open.is_(True), calendar.c.trade_date <= window_end)
            .distinct()
            .order_by(calendar.c.trade_date.desc())
            .limit(max(lookback_days, 1))
        )
        days = [row[0] for row in connection.execute(statement)]
    if days:
        ordered = sorted(days)
        return (ordered[0], ordered[-1]), ordered
    fallback = window_end - timedelta(days=max(lookback_days - 1, 0))
    return (fallback, window_end), []


def _scope_entity_ids(connection: Any, metadata: MetaData, codes: Sequence[str]) -> list[int]:
    """期望范围（codes → entity_id；缺省/未登记即空 = 全量在市）。"""
    if not codes:
        return []
    entity = metadata.tables.get("ref.entity")
    if entity is None:
        return []
    statement = select(entity.c.entity_id).where(entity.c.code.in_(list(codes)))
    return [int(row[0]) for row in connection.execute(statement)]


def _interval_dataset(table: Table) -> bool:
    """区间型数据集：同时含 ``start_date`` / ``end_date``（如上市生命周期）。"""
    return "start_date" in table.c and "end_date" in table.c


def _row_count(
    connection: Any, source: Any, event: str | None, window: tuple[date, date] | None
) -> int:
    statement = select(func.count()).select_from(source)
    if event is not None and window is not None:
        statement = statement.where(source.c[event].between(window[0], window[1]))
    return int(connection.execute(statement).scalar_one())


def _result(
    dataset: str,
    check_id: str,
    family: str,
    severity: str,
    status: str,
    window: tuple[date, date],
    *,
    rows_checked: int = 0,
    violations: int = 0,
    samples: tuple[str, ...] = (),
    metrics: dict[str, Any] | None = None,
    message: str = "",
) -> CheckResult:
    """构造结果（统一填充窗口与基础字段）。"""
    return CheckResult(
        dataset=dataset,
        check_id=check_id,
        family=family,
        severity=severity,
        status=status,
        window_start=window[0],
        window_end=window[1],
        rows_checked=rows_checked,
        violations=violations,
        samples=samples,
        metrics=metrics if metrics is not None else {},
        message=message,
    )


def _execute_rule(
    connection: Any,
    planned: PlannedRule,
    *,
    dataset: str,
    window: tuple[date, date],
    rows_checked: int,
) -> CheckResult:
    if planned.query is None:
        return _result(
            dataset,
            planned.check_id,
            planned.family,
            planned.severity,
            STATUS_SKIPPED,
            window,
            rows_checked=rows_checked,
            message=planned.skip_reason or "结构性跳过",
        )
    violations = int(
        connection.execute(
            select(func.count()).select_from(planned.query.subquery())
        ).scalar_one()
    )
    samples = tuple(
        _format_row(planned.sample_columns, row)
        for row in connection.execute(planned.query.limit(_SAMPLE_LIMIT))
    )
    message = f"违规 {violations} 条" if violations else "通过"
    if planned.note:
        message = f"{message}（{planned.note}）"
    return _result(
        dataset,
        planned.check_id,
        planned.family,
        planned.severity,
        STATUS_PASSED if violations == 0 else STATUS_FAILED,
        window,
        rows_checked=rows_checked,
        violations=violations,
        samples=samples,
        message=message,
    )


def _completeness(
    connection: Any,
    *,
    spec: DatasetSpec,
    table: Table,
    dictionary: dict[str, DatasetSpec],
    metadata: MetaData,
    window: tuple[date, date],
    open_days: list[date],
    scope_ids: list[int],
    scope_requested: bool,
    min_ratio: float,
) -> CheckResult | None:
    """完整性：期望（在市 × 交易日，停牌感知）vs 实际；区间数据集用区间覆盖判定。"""
    coverage = spec.coverage
    if coverage is None or coverage.universe_source in ("", "self"):
        return None

    def _skip(reason: str) -> CheckResult:
        return _result(
            spec.dataset,
            FAMILY_COMPLETENESS,
            FAMILY_COMPLETENESS,
            "error",
            STATUS_SKIPPED,
            window,
            message=reason,
        )

    dataset_interval = _interval_dataset(table)
    if not open_days and not dataset_interval:
        return _skip("窗口内无交易日")
    if scope_requested and not scope_ids:
        # 配置了期望范围但未匹配任何实体：报错而非退化为全市场期望（掩盖配置错误）
        return _skip("期望范围未解析（codes 未匹配任何实体）")
    if dataset_interval and not scope_ids:
        return _skip("区间数据集需配置期望范围（codes）")
    universe_spec = dictionary.get(coverage.universe_source)
    universe = (
        metadata.tables.get(universe_spec.storage.canonical_table)
        if universe_spec is not None
        else None
    )
    if universe is None:
        return _skip(f"期望集合未落地: {coverage.universe_source}")
    key = next(
        (
            name
            for name in _KEY_PREFERENCE
            if name in table.c and name in universe.c
        ),
        None,
    )
    if key is None:
        return _skip("未找到共享键列（期望集合 × 数据集）")
    scope_applied = bool(scope_ids) and key == "entity_id"
    calendar = metadata.tables.get(coverage.expected_dates.calendar)
    if calendar is None:
        return _skip(f"日历未落地: {coverage.expected_dates.calendar}")

    days = (
        select(calendar.c.trade_date)
        .where(
            calendar.c.is_open.is_(True),
            calendar.c.trade_date.between(window[0], window[1]),
        )
        .distinct()
        .subquery("open_days")
    )
    universe_interval = "start_date" in universe.c and "end_date" in universe.c
    if universe_interval:
        window_condition = and_(
            universe.c.start_date <= days.c.trade_date,
            or_(
                universe.c.end_date.is_(None),
                universe.c.end_date >= days.c.trade_date,
            ),
        )
        # 同日多条覆盖区间取「获胜者」（与 registry.universe 一致：
        # start_date / version / knowledge_time 最大者），再排除退市终态区间；
        # 版本感知可避免被修正的旧区间（未闭合的历史行）造成幻影在市
        order_by: list[Any] = [universe.c.start_date.desc()]
        if "version" in universe.c:
            order_by.append(universe.c.version.desc())
        if "knowledge_time" in universe.c:
            order_by.append(universe.c.knowledge_time.desc())
        rank = (
            func.row_number()
            .over(
                partition_by=[universe.c[key], days.c.trade_date],
                order_by=order_by,
            )
            .label("_rank")
        )
        has_status = "status" in universe.c
        select_columns: list[Any] = [
            universe.c[key].label("k"),
            days.c.trade_date.label("d"),
            rank,
        ]
        if has_status:
            select_columns.insert(2, universe.c.status.label("status"))
        ranked = select(*select_columns).select_from(universe.join(days, window_condition))
        if scope_ids and key == "entity_id":
            ranked = ranked.where(universe.c[key].in_(scope_ids))
        ranked_sub = ranked.subquery("ranked_universe")
        pairs_statement = select(ranked_sub.c.k, ranked_sub.c.d).where(
            ranked_sub.c._rank == 1
        )
        if has_status:
            pairs_statement = pairs_statement.where(
                or_(ranked_sub.c.status.is_(None), ranked_sub.c.status != "delisted")
            )
    else:
        pairs_statement = select(
            universe.c[key].label("k"), days.c.trade_date.label("d")
        ).select_from(universe.join(days, true()))
        if scope_ids and key == "entity_id":
            pairs_statement = pairs_statement.where(universe.c[key].in_(scope_ids))
        # SCD2 期望集合按键去重（同键多版本不得重复计入期望）
        pairs_statement = pairs_statement.distinct()
    pairs = pairs_statement.subquery("expected_pairs")

    status_table = metadata.tables.get("cn_equity.daily_status")

    def missing_statement() -> Any:
        if dataset_interval:
            covered = (
                select(1)
                .select_from(table)
                .where(
                    table.c[key] == pairs.c.k,
                    table.c.start_date <= pairs.c.d,
                    or_(table.c.end_date.is_(None), table.c.end_date >= pairs.c.d),
                )
                .exists()
            )
        else:
            assert event is not None
            covered = (
                select(1)
                .select_from(table)
                .where(table.c[key] == pairs.c.k, table.c[event] == pairs.c.d)
                .exists()
            )
        conditions = [~covered]
        # 停牌排除仅适用于实体键的逐日行情类数据集：exchange_id 等键无停牌语义
        # （跨类型比较在 PostgreSQL 报错）；区间型数据集在停牌日同样应有区间行
        if (
            status_table is not None
            and spec.dataset != "cn_equity.daily_status"
            and key == "entity_id"
            and not dataset_interval
        ):
            suspended = (
                select(1)
                .select_from(status_table)
                .where(
                    status_table.c.entity_id == pairs.c.k,
                    status_table.c.trade_date == pairs.c.d,
                    status_table.c.is_suspended.is_(True),
                )
                .exists()
            )
            conditions.append(~suspended)
        return select(pairs.c.k, pairs.c.d).where(*conditions)

    event = event_column(spec, table)
    if not dataset_interval and event is None:
        return _skip("数据集无事件时间列，无法做逐日覆盖判定")

    expected = int(
        connection.execute(select(func.count()).select_from(pairs)).scalar_one()
    )
    if expected == 0:
        return _skip("窗口内无期望行（在市集合或范围为空）")
    missing_query = missing_statement()
    missing_sub = missing_query.subquery()
    missing = int(
        connection.execute(
            select(func.count()).select_from(missing_sub)
        ).scalar_one()
    )
    samples = tuple(
        f"{key}={row[0]}, trade_date={_stringify(row[1])}"
        for row in connection.execute(missing_query.limit(_SAMPLE_LIMIT))
    )
    ratio = (expected - missing) / expected
    missing_by_day = {
        row[0]: int(row[1])
        for row in connection.execute(
            select(missing_sub.c.d, func.count()).group_by(missing_sub.c.d)
        )
    }
    expected_by_day = {
        row[0]: int(row[1])
        for row in connection.execute(
            select(pairs.c.d, func.count()).group_by(pairs.c.d)
        )
    }
    break_days = sorted(
        day
        for day, day_missing in missing_by_day.items()
        if expected_by_day.get(day, 0)
        and 1 - day_missing / expected_by_day[day] < min_ratio
    )
    status = STATUS_PASSED if ratio >= min_ratio else STATUS_FAILED
    message = (
        f"覆盖率 {ratio:.4%}（期望 {expected}，缺失 {missing}）"
        if missing
        else f"覆盖率 100%（期望 {expected}）"
    )
    if break_days:
        message += f"；断点 {len(break_days)} 天"
    if scope_requested and not scope_applied:
        message += "；期望范围未应用（数据集键非 entity_id）"
    metrics = {
        "expected_pairs": expected,
        "missing": missing,
        "ratio": round(ratio, 6),
        "min_ratio": min_ratio,
        "open_days": len(open_days),
        "break_days": [_stringify(day) for day in break_days[:10]],
        "universe": coverage.universe_source,
        "scoped": bool(scope_ids),
        "scope_requested": scope_requested,
        "scope_applied": scope_applied,
        "interval": dataset_interval,
    }
    return _result(
        spec.dataset,
        FAMILY_COMPLETENESS,
        FAMILY_COMPLETENESS,
        "error",
        status,
        window,
        rows_checked=expected,
        violations=missing,
        samples=samples,
        metrics=metrics,
        message=message,
    )


def _freshness(
    connection: Any,
    *,
    spec: DatasetSpec,
    table: Table,
    window: tuple[date, date],
    open_days: list[date],
) -> CheckResult | None:
    """时效性：最新数据日 vs 最近已收盘交易日（滞后交易日数）。"""
    if spec.update_sla is None or _interval_dataset(table):
        return None
    event = event_column(spec, table)
    if event is None or not open_days:
        return None
    actual = connection.execute(select(func.max(table.c[event]))).scalar_one_or_none()
    expected = open_days[-1]
    if actual is not None and isinstance(actual, datetime):
        actual = actual.date()
    lag_days = (
        len(open_days)
        if actual is None
        else sum(1 for day in open_days if day > actual)
    )
    status = STATUS_PASSED if actual is not None and actual >= expected else STATUS_FAILED
    message = (
        f"最新数据 {actual}，期望 {expected}（滞后 {lag_days} 个交易日）"
        if actual is not None
        else f"无数据，期望 {expected}"
    )
    metrics = {
        "latest": _stringify(actual) if actual is not None else None,
        "expected": _stringify(expected),
        "lag_days": lag_days,
        "sla": {
            "frequency": spec.update_sla.frequency,
            "latest_available": spec.update_sla.latest_available,
            "tolerance": spec.update_sla.tolerance,
        },
    }
    return _result(
        spec.dataset,
        FAMILY_FRESHNESS,
        FAMILY_FRESHNESS,
        "warn",
        status,
        window,
        metrics=metrics,
        message=message,
    )


def _error_result(
    dataset: str,
    window: tuple[date, date],
    message: str,
    *,
    check_id: str = "scan",
    family: str = FAMILY_RULE,
    severity: str = "error",
) -> CheckResult:
    return CheckResult(
        dataset=dataset,
        check_id=check_id,
        family=family,
        severity=severity,
        status=STATUS_ERROR,
        window_start=window[0],
        window_end=window[1],
        message=message,
    )


def _format_row(columns: Sequence[str], row: Any) -> str:
    return ", ".join(
        f"{name}={_stringify(value)}" for name, value in zip(columns, row, strict=False)
    )


def _stringify(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
