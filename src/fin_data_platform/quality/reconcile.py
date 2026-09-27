"""跨源对账（TASK-3.5；方法见 doc-8 §2 SOP 子集）。

小样本实拉对账（默认 Tushare 基准 vs BaoStock）：

- **原始价一致**：重叠窗口内 ``|close_a − close_b|`` ≤ 容差（默认 0.01 元）；
- **复权因子归一化**：``f / f_last`` 最大绝对差 ≤ 容差（默认 1e-4；doc-8 R2 实测 6.5e-06）。

每个指标独立成 :class:`CheckResult`；源完全缺失 / 取数失败 → ``skipped``（不判失败）；
单边部分缺失（重叠不足）记入 ``metrics.dropped_rows`` 并在消息中提示。
对账样本与容差由 ``FDP_QUALITY_RECONCILE_CODES`` 等配置注入（付费源按次计费，
样本保持最小：默认 2 只标的 × 30 个交易日；复权因子窗口另取 365 天）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pandas as pd

from fin_data_platform.quality.models import (
    FAMILY_CROSS_SOURCE,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    CheckResult,
)

#: 基准源 / 对照源（doc-8：BaoStock 原始价与因子经 R2 对账通过）
SOURCE_A = "tushare"
SOURCE_B = "baostock"

_SAMPLE_LIMIT = 20


def cross_source_checks(
    hub: object,
    *,
    codes: Sequence[str],
    window_start: date,
    window_end: date,
    close_tolerance: float = 0.01,
    factor_tolerance: float = 1e-4,
    factor_window_start: date | None = None,
    source_a: str = SOURCE_A,
    source_b: str = SOURCE_B,
) -> list[CheckResult]:
    """执行两个关键指标的小样本跨源对账（返回 2 项结果）。

    ``factor_window_start``：复权因子对账的独立起点（除权事件稀疏，通常需更长窗口；
    缺省与价格窗口一致）。
    """
    dataset = "cn_equity.daily_bar"
    check_id = f"{FAMILY_CROSS_SOURCE}:close"
    if not codes:
        return skipped_pair(window_start, window_end, "未配置对账样本")
    factor_start = factor_window_start if factor_window_start is not None else window_start
    return [
        _compare(
            hub,
            dataset=dataset,
            check_id=check_id,
            field="close",
            codes=codes,
            window_start=window_start,
            window_end=window_end,
            tolerance=close_tolerance,
            source_a=source_a,
            source_b=source_b,
            normalized=False,
        ),
        _compare(
            hub,
            dataset="cn_equity.adj_factor",
            check_id=f"{FAMILY_CROSS_SOURCE}:factor",
            field="adj_factor",
            codes=codes,
            window_start=factor_start,
            window_end=window_end,
            tolerance=factor_tolerance,
            source_a=source_a,
            source_b=source_b,
            normalized=True,
        ),
    ]


def _compare(
    hub: object,
    *,
    dataset: str,
    check_id: str,
    field: str,
    codes: Sequence[str],
    window_start: date,
    window_end: date,
    tolerance: float,
    source_a: str,
    source_b: str,
    normalized: bool,
) -> CheckResult:
    try:
        left = _fetch(hub, codes, window_start, window_end, source_a, field)
        right = _fetch(hub, codes, window_start, window_end, source_b, field)
    except Exception as exc:  # 源不可用 / 未装配：跳过而非失败
        return _skipped(
            dataset,
            check_id,
            window_start,
            window_end,
            f"跨源取数失败: {type(exc).__name__}: {exc}",
        )
    if left.empty or right.empty:
        return _skipped(dataset, check_id, window_start, window_end, "源数据为空")
    merged = left.merge(right, on=["code", "date"], suffixes=("_a", "_b"), how="inner")
    if merged.empty:
        return _skipped(dataset, check_id, window_start, window_end, "无重叠样本")
    if normalized:
        # 以两源「共同末值」为锚：单边晚发布导致锚点日期不一致时不产生假失败
        merged = _normalize_common(merged, field)
    difference = (merged[f"{field}_a"] - merged[f"{field}_b"]).abs()
    invalid = int(difference.isna().sum())
    # 缺失/无效样本计为违规：不允许 NaN 静默通过（质量工具自身的数据完整性）
    violations = int((difference > tolerance).sum()) + invalid
    worst = merged.assign(_diff=difference).sort_values(
        "_diff", ascending=False, na_position="first"
    )
    samples = [
        f"{record['code']} {record['date']}: {record[f'{field}_a']} vs {record[f'{field}_b']}"
        for _, record in worst.head(_SAMPLE_LIMIT).iterrows()
        if pd.isna(record["_diff"]) or record["_diff"] > tolerance
    ]
    status = STATUS_PASSED if violations == 0 else STATUS_FAILED
    finite = difference.dropna()
    max_diff = None if finite.empty else float(finite.max())
    max_text = "—" if max_diff is None else f"{max_diff:.6g}"
    dropped = int(len(left) + len(right) - 2 * len(merged))
    metrics = {
        "overlap_rows": int(len(merged)),
        "rows_a": int(len(left)),
        "rows_b": int(len(right)),
        "dropped_rows": dropped,
        "invalid_rows": invalid,
        "max_abs_diff": max_diff,
        "mean_abs_diff": None if finite.empty else float(finite.mean()),
        "tolerance": tolerance,
        "normalized": normalized,
        "source_a": source_a,
        "source_b": source_b,
        "codes": list(codes),
    }
    message = (
        f"{source_a} vs {source_b}：重叠 {len(merged)} 行，"
        f"最大差 {max_text}（容差 {tolerance:g}）"
    )
    if dropped:
        message += f"，单边缺失 {dropped} 行"
    if invalid:
        message += f"，缺失/无效 {invalid} 行"
    return CheckResult(
        dataset=dataset,
        check_id=check_id,
        family=FAMILY_CROSS_SOURCE,
        severity="error",
        status=status,
        window_start=window_start,
        window_end=window_end,
        rows_checked=int(len(merged)),
        violations=violations,
        samples=tuple(samples),
        metrics=metrics,
        message=message,
    )


def _fetch(
    hub: object,
    codes: Sequence[str],
    window_start: date,
    window_end: date,
    source: str,
    field: str,
) -> pd.DataFrame:
    start, end = window_start.isoformat(), window_end.isoformat()
    if field == "adj_factor":
        frame = hub.get_adjust_factors(  # type: ignore[attr-defined]
            list(codes), start=start, end=end, source=source
        )
    else:
        frame = hub.get_bars(  # type: ignore[attr-defined]
            list(codes), start=start, end=end, adjust=None, source=source
        )
    return _prepare(frame, field)


def _prepare(frame: pd.DataFrame | None, field: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["code", "date", field])
    out = frame[["code", "date", field]].copy()
    out["code"] = out["code"].astype(str)
    out["date"] = pd.to_datetime(out["date"]).dt.date
    return out


def _normalize_common(merged: pd.DataFrame, field: str) -> pd.DataFrame:
    """按各标的的**共同末值**归一化（``f / f_last``；锚点取两源重叠窗口末行）。"""
    ordered = merged.sort_values(["code", "date"])
    last = ordered.groupby("code").tail(1).set_index("code")
    out = ordered.copy()
    out[f"{field}_a"] = out[f"{field}_a"] / out["code"].map(last[f"{field}_a"])
    out[f"{field}_b"] = out[f"{field}_b"] / out["code"].map(last[f"{field}_b"])
    return out


def _skipped(
    dataset: str, check_id: str, window_start: date, window_end: date, reason: str
) -> CheckResult:
    return CheckResult(
        dataset=dataset,
        check_id=check_id,
        family=FAMILY_CROSS_SOURCE,
        severity="error",
        status=STATUS_SKIPPED,
        window_start=window_start,
        window_end=window_end,
        message=reason,
    )


def skipped_pair(window_start: date, window_end: date, reason: str) -> list[CheckResult]:
    """跨源对账的两项跳过占位（未配置样本 / hub 未装配）。"""
    return [
        _skipped(
            "cn_equity.daily_bar",
            f"{FAMILY_CROSS_SOURCE}:close",
            window_start,
            window_end,
            reason,
        ),
        _skipped(
            "cn_equity.adj_factor",
            f"{FAMILY_CROSS_SOURCE}:factor",
            window_start,
            window_end,
            reason,
        ),
    ]
