"""跨序列对齐（asof join）：按事件时间取右表最近观测（TASK-3.13）。

- 语义 = pandas ``merge_asof``：``backward``（默认，PIT 安全：只用 ≤ 当前时点的
  右表观测）；``forward`` / ``nearest`` 会引用**未来**数据，仅限非回测展示场景；
- 两侧序列应以**同一 ``as_of``** 读取（PIT 一致）；本函数只做事件时间对齐，不做
  知识时间过滤；
- 事件时间列以 ``datetime.date`` 传入/返回（内部转 datetime64 以支持 merge_asof）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import pandas as pd

from fin_data_platform.panel.errors import InvalidArgument

#: 对齐方向（backward 为 PIT 安全默认）
DIRECTIONS = frozenset({"backward", "forward", "nearest"})

Direction = Literal["backward", "forward", "nearest"]


def _prepare(frame: pd.DataFrame, on: str, by: Sequence[str]) -> pd.DataFrame:
    missing = [name for name in (on, *by) if name not in frame.columns]
    if missing:
        raise InvalidArgument(f"asof join 缺少列：{missing}", hint=f"实际列 {list(frame.columns)}")
    prepared = frame.copy()
    prepared[on] = pd.to_datetime(prepared[on])
    # merge_asof 要求 on 列全局有序；by 分组由 pandas 内部处理（不可按 [by, on] 排序）
    return prepared.sort_values([on, *by], kind="stable")


def asof_join(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    left_on: str,
    right_on: str | None = None,
    by: str | Sequence[str] | None = None,
    direction: Direction = "backward",
    tolerance: Any = None,
    suffixes: tuple[str, str] = ("", "_right"),
) -> pd.DataFrame:
    """按 ``left_on`` 逐行取右侧最近观测（``by`` 分组内），返回合并后的长表。"""
    if direction not in DIRECTIONS:
        raise InvalidArgument(
            f"对齐方向非法：{direction!r}", hint=f"可选 {sorted(DIRECTIONS)}"
        )
    right_on = right_on or left_on
    by_columns: list[str] = [] if by is None else ([by] if isinstance(by, str) else list(by))
    left_sorted = _prepare(left, left_on, by_columns)
    right_sorted = _prepare(right, right_on, by_columns)

    if right_on == left_on:
        merged = pd.merge_asof(
            left_sorted,
            right_sorted,
            on=left_on,
            by=by_columns or None,
            direction=direction,
            tolerance=tolerance,
            suffixes=suffixes,
        )
    else:
        merged = pd.merge_asof(
            left_sorted,
            right_sorted,
            left_on=left_on,
            right_on=right_on,
            by=by_columns or None,
            direction=direction,
            tolerance=tolerance,
            suffixes=suffixes,
        )
    for name in {left_on, right_on}:
        if name in merged.columns and pd.api.types.is_datetime64_any_dtype(merged[name]):
            merged[name] = merged[name].dt.date
    order = list(dict.fromkeys([*left_sorted.columns, *right_sorted.columns]))
    merged = merged.loc[:, [name for name in order if name in merged.columns]]
    merged = merged.sort_values([*by_columns, left_on], kind="stable").reset_index(drop=True)
    merged.attrs.update(direction=direction, asof_on=left_on)
    return merged
