"""时序查询：范围序列 / 截面 / 面板（TASK-3.13，doc-2 §6.14）。

PIT 正确性：读取先按 ``as_of`` 过滤（访问面执行），对齐 / 重采样 / 填充 / 窗口
算子都在**可见数据**上完成；**不使用数据库连续聚合**（物化为非 PIT 语义，连续
聚合留待非 PIT 读模型 / 性能优化）。

口径：

- ``freq``：``1d``（默认，不聚合）/ ``1w`` / ``1mo`` / ``1q`` / ``1y``；桶锚点 =
  该期**最后一个观测日**（``calendar="trading"`` 时即最后一个交易日）；
- 聚合：默认按字段语义（``open=first`` / ``high=max`` / ``low=min`` / ``close=last`` /
  ``volume``·``amount=sum`` / 其它 ``last``），``agg`` 可逐字段覆盖；空桶值为 null；
- ``fill``：``none``（默认，缺口保持 null）/ ``ffill``（前值，仅用 ``as_of`` 可见数据；
  停牌行同样前值填充，原因由 ``status`` / ``is_suspended`` 列保留）；对**无预期行**的
  序列（如 ``calendar=None``，或重采样后不产生空桶）ffill 为无操作；
- ``calendar``：``trading``（默认；按落库日历补齐预期行 + 状态列）| ``None``
  （只返回已存行）。对不支持对齐的数据集（业务键非实体 × 事件时间）显式报
  ``unsupported_alignment``；
- 窗口算子：``rolling``（均值 / 求和 / 极值 / 末值 / 计数）与 ``change``（同比 / 环比，
  即 ``pct_change``）在重采样与填充之后、按实体流式计算，仅使用可见数据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from fin_data_platform.access import read as access_read
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.panel.errors import (
    InvalidArgument,
    InvalidFill,
    UnsupportedFrequency,
)

#: 频率 → pandas 周期别名（``1d`` 不聚合）
_FREQ_PERIOD: dict[str, str] = {"1w": "W", "1mo": "M", "1q": "Q", "1y": "Y"}

#: 支持频率
FREQUENCIES = frozenset({"1d", *_FREQ_PERIOD})

#: 缺口策略
FILL_STRATEGIES = frozenset({"none", "ffill"})

#: 日历取值
CALENDARS = frozenset({"trading", None})

#: 聚合默认（按 canonical 字段语义；未列出的字段取 ``last``）
DEFAULT_AGGS: dict[str, str] = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "pre_close": "last",
    "volume": "sum",
    "amount": "sum",
}

#: 允许的聚合算子
AGG_OPS = frozenset({"first", "last", "sum", "min", "max", "mean", "count"})

#: 允许的窗口算子
ROLLING_OPS = frozenset({"mean", "sum", "min", "max", "last", "count"})

#: 对齐路径的状态列
_STATUS_COLUMNS = ("status", "is_suspended", "is_st")


@dataclass(frozen=True, slots=True)
class Rolling:
    """滚动窗口算子（按实体流式，窗口含当前行）。"""

    window: int
    op: str = "mean"
    min_periods: int | None = None

    def __post_init__(self) -> None:
        if self.window < 1:
            raise InvalidArgument(f"rolling.window 必须为正整数：{self.window}")
        if self.op not in ROLLING_OPS:
            raise InvalidArgument(
                f"rolling.op 非法：{self.op!r}", hint=f"可选 {sorted(ROLLING_OPS)}"
            )
        if self.min_periods is not None and self.min_periods < 1:
            raise InvalidArgument(f"rolling.min_periods 必须为正整数：{self.min_periods}")


def _validate_freq(freq: str) -> None:
    if freq not in FREQUENCIES:
        raise UnsupportedFrequency(
            f"频率不受支持：{freq!r}", hint=f"可选 {sorted(FREQUENCIES)}"
        )


def _validate_fill(fill: str) -> None:
    if fill not in FILL_STRATEGIES:
        raise InvalidFill(f"缺口策略非法：{fill!r}", hint=f"可选 {sorted(FILL_STRATEGIES)}")


def _validate_calendar(calendar: str | None) -> None:
    if calendar not in CALENDARS:
        raise InvalidArgument(
            f"calendar 取值非法：{calendar!r}", hint="可选 'trading' / None"
        )


def _event_time_field(spec: DatasetSpec) -> str:
    for field in spec.fields:
        if field.pit_role == "event_time":
            return field.name
    raise InvalidArgument(f"{spec.dataset}: 无事件时间字段，无法做时序查询")


def _key_columns(spec: DatasetSpec, event_field: str) -> list[str]:
    return [name for name in spec.business_key if name != event_field] or ["entity_id"]


def _read_frame(
    engine: Engine,
    dataset: str,
    *,
    entities: Sequence[int] | None,
    fields: Sequence[str],
    start: date,
    end: date,
    as_of: datetime,
    adjust: str | None,
    calendar: str | None,
    specs: Mapping[str, DatasetSpec] | None,
) -> pd.DataFrame:
    """访问面读取（calendar="trading" 走对齐路径；显式 as_of，PIT 严格）。"""
    if calendar == "trading":
        result = access_read(
            engine,
            dataset,
            list(fields),
            as_of=as_of,
            adjust=adjust,
            entities=None if entities is None else list(entities),
            window=(start, end),
            align_calendar=True,
            specs=specs,
        )
    else:
        result = access_read(
            engine,
            dataset,
            list(fields),
            as_of=as_of,
            adjust=adjust,
            entities=None if entities is None else list(entities),
            window=(start, end),
            specs=specs,
        )
    return result.table.to_pandas()


def _combine_status(values: pd.Series) -> str:
    if bool((values == "ok").any()):
        return "ok"
    if bool((values == "suspended").any()):
        return "suspended"
    return "missing"


def _combine_flag(values: pd.Series) -> Any:
    if bool(values.isna().all()):
        return pd.NA
    return bool(values.fillna(False).astype(bool).any())


def _resample(
    frame: pd.DataFrame,
    *,
    event_field: str,
    key_columns: Sequence[str],
    freq: str,
    agg: Mapping[str, str] | None,
) -> pd.DataFrame:
    """日历锚定重采样：桶锚点 = 该期最后一个观测日；默认聚合见模块 docstring。"""
    overrides = dict(agg or {})
    unknown = set(overrides) - set(frame.columns)
    if unknown:
        raise InvalidArgument(f"agg 引用了不存在的字段：{sorted(unknown)}")
    bad_ops = {op for op in overrides.values() if op not in AGG_OPS}
    if bad_ops:
        raise InvalidArgument(
            f"agg 算子非法：{sorted(bad_ops)}", hint=f"可选 {sorted(AGG_OPS)}"
        )
    work = frame.copy()
    work["_period"] = pd.to_datetime(work[event_field]).dt.to_period(_FREQ_PERIOD[freq])
    work = work.sort_values([*key_columns, "_period", event_field], kind="stable")
    grouped = work.groupby([*key_columns, "_period"], sort=True)
    data_fields = [
        name
        for name in work.columns
        if name not in {*key_columns, event_field, "_period", *_STATUS_COLUMNS}
    ]
    columns: dict[str, Any] = {}
    for name in data_fields:
        op = overrides.get(name) or DEFAULT_AGGS.get(name, "last")
        columns[name] = grouped[name].agg(op)
    out = pd.DataFrame(columns)
    out[event_field] = grouped[event_field].max()  # 桶锚点 = 期内最后一个观测日
    for name in _STATUS_COLUMNS:
        if name in work.columns:
            out[name] = grouped[name].agg(
                _combine_status if name == "status" else _combine_flag
            )
    order = [
        *key_columns,
        event_field,
        *data_fields,
        *[name for name in _STATUS_COLUMNS if name in out.columns],
    ]
    out = out.reset_index().drop(columns="_period")
    return out.loc[:, order]


def _apply_fill(
    frame: pd.DataFrame,
    *,
    fields: Sequence[str],
    key_columns: Sequence[str],
    event_field: str,
    fill: str,
) -> pd.DataFrame:
    if fill == "none":
        return frame
    out = frame.sort_values([*key_columns, event_field], kind="stable").copy()
    group = out.groupby(list(key_columns), sort=False)
    for name in fields:
        out[name] = group[name].ffill()
    return out.reset_index(drop=True)


def _apply_rolling(
    frame: pd.DataFrame,
    *,
    rolling: Rolling | None,
    fields: Sequence[str],
    key_columns: Sequence[str],
    event_field: str,
) -> tuple[pd.DataFrame, list[str]]:
    if rolling is None:
        return frame, []
    out = frame.sort_values([*key_columns, event_field], kind="stable").copy()
    group = out.groupby(list(key_columns), sort=False)
    min_periods = rolling.min_periods or rolling.window
    derived: list[str] = []
    for name in fields:
        column = f"{name}_roll{rolling.window}_{rolling.op}"

        def _window(values: pd.Series) -> pd.Series:
            window = values.rolling(rolling.window, min_periods=min_periods)
            if rolling.op == "last":
                return window.apply(lambda items: items[-1], raw=True)
            return getattr(window, rolling.op)()

        out[column] = group[name].transform(_window)
        derived.append(column)
    return out.reset_index(drop=True), derived


def _apply_change(
    frame: pd.DataFrame,
    *,
    change: int | None,
    fields: Sequence[str],
    key_columns: Sequence[str],
    event_field: str,
) -> tuple[pd.DataFrame, list[str]]:
    if change is None:
        return frame, []
    if change < 1:
        raise InvalidArgument(f"change 期数必须为正整数：{change}")
    out = frame.sort_values([*key_columns, event_field], kind="stable").copy()
    group = out.groupby(list(key_columns), sort=False)
    derived: list[str] = []
    for name in fields:
        column = f"{name}_chg{change}"
        out[column] = group[name].pct_change(periods=change)
        derived.append(column)
    return out.reset_index(drop=True), derived


def _set_attrs(frame: pd.DataFrame, **values: Any) -> pd.DataFrame:
    frame.attrs.update({key: value for key, value in values.items() if value is not None})
    return frame


def get_series(
    engine: Engine,
    dataset: str,
    *,
    fields: Sequence[str],
    start: date,
    end: date,
    as_of: datetime,
    entities: Sequence[int] | None = None,
    freq: str = "1d",
    fill: str = "none",
    calendar: str | None = "trading",
    adjust: str | None = None,
    agg: Mapping[str, str] | None = None,
    rolling: Rolling | None = None,
    change: int | None = None,
    specs: Mapping[str, DatasetSpec] | None = None,
) -> pd.DataFrame:
    """范围序列查询（多键 × 字段 × 频率；PIT：``as_of`` 显式）。

    返回长表（业务键 + 事件时间 + 字段 + 对齐状态列 + 窗口算子派生列）。
    """
    _validate_freq(freq)
    _validate_fill(fill)
    _validate_calendar(calendar)
    dictionary = specs if specs is not None else load_all()
    spec = dictionary.get(dataset)
    if spec is None:
        from fin_data_platform.access import UnknownDataset

        raise UnknownDataset(f"数据集不存在：{dataset}", hint="见数据字典")
    event_field = _event_time_field(spec)
    key_columns = _key_columns(spec, event_field)

    frame = _read_frame(
        engine,
        dataset,
        entities=entities,
        fields=fields,
        start=start,
        end=end,
        as_of=as_of,
        adjust=adjust,
        calendar=calendar,
        specs=dictionary,
    )
    if freq != "1d":
        frame = _resample(
            frame, event_field=event_field, key_columns=key_columns, freq=freq, agg=agg
        )
    elif agg:
        raise InvalidArgument("agg 仅在 freq != '1d' 时可用", hint="设置 freq=1w/1mo/1q/1y")
    frame = _apply_fill(
        frame, fields=fields, key_columns=key_columns, event_field=event_field, fill=fill
    )
    frame, rolling_columns = _apply_rolling(
        frame,
        rolling=rolling,
        fields=fields,
        key_columns=key_columns,
        event_field=event_field,
    )
    frame, change_columns = _apply_change(
        frame,
        change=change,
        fields=fields,
        key_columns=key_columns,
        event_field=event_field,
    )
    order = [
        *key_columns,
        event_field,
        *fields,
        *[name for name in _STATUS_COLUMNS if name in frame.columns],
        *rolling_columns,
        *change_columns,
    ]
    frame = frame.loc[:, order].sort_values([*key_columns, event_field], kind="stable")
    return _set_attrs(
        frame.reset_index(drop=True),
        dataset=dataset,
        as_of=as_of,
        freq=freq,
        fill=fill,
        calendar=calendar,
        adjust=adjust,
    )


def get_cross_section(
    engine: Engine,
    dataset: str,
    *,
    date: date,
    as_of: datetime,
    fields: Sequence[str],
    entities: Sequence[int] | None = None,
    calendar: str | None = None,
    adjust: str | None = None,
    specs: Mapping[str, DatasetSpec] | None = None,
) -> pd.DataFrame:
    """截面查询（单一事件日）：默认返回该日已存行；``calendar="trading"`` 时按日历对齐。"""
    _validate_calendar(calendar)
    dictionary = specs if specs is not None else load_all()
    frame = _read_frame(
        engine,
        dataset,
        entities=entities,
        fields=fields,
        start=date,
        end=date,
        as_of=as_of,
        adjust=adjust,
        calendar=calendar,
        specs=dictionary,
    )
    return _set_attrs(
        frame.reset_index(drop=True),
        dataset=dataset,
        as_of=as_of,
        calendar=calendar,
        adjust=adjust,
    )


def get_panel(
    engine: Engine,
    dataset: str,
    *,
    fields: Sequence[str],
    start: date,
    end: date,
    as_of: datetime,
    entities: Sequence[int] | None = None,
    freq: str = "1d",
    fill: str = "none",
    calendar: str | None = "trading",
    adjust: str | None = None,
    agg: Mapping[str, str] | None = None,
    rolling: Rolling | None = None,
    change: int | None = None,
    shape: str = "wide",
    specs: Mapping[str, DatasetSpec] | None = None,
) -> pd.DataFrame:
    """面板查询：``shape="long"`` 等价 :func:`get_series`；``"wide"`` 转宽表。"""
    long = get_series(
        engine,
        dataset,
        fields=fields,
        start=start,
        end=end,
        as_of=as_of,
        entities=entities,
        freq=freq,
        fill=fill,
        calendar=calendar,
        adjust=adjust,
        agg=agg,
        rolling=rolling,
        change=change,
        specs=specs,
    )
    if shape == "long":
        return long
    if shape != "wide":
        raise InvalidArgument(f"shape 取值非法：{shape!r}", hint="可选 'wide' / 'long'")
    dictionary = specs if specs is not None else load_all()
    spec = dictionary.get(dataset)
    if spec is None:
        from fin_data_platform.access import UnknownDataset

        raise UnknownDataset(f"数据集不存在：{dataset}", hint="见数据字典")
    event_field = _event_time_field(spec)
    key_columns = _key_columns(spec, event_field)
    if len(key_columns) != 1:
        raise InvalidArgument(
            f"宽表仅支持单一键列（当前 {key_columns}）", hint="改用 shape='long'"
        )
    key = key_columns[0]
    value_columns = [
        name for name in long.columns if name not in {key, event_field, *_STATUS_COLUMNS}
    ]
    wide = long.pivot(index=event_field, columns=key, values=value_columns)
    wide = wide.swaplevel(0, 1, axis=1).sort_index(axis=1)
    wide.columns.names = [key, "field"]
    wide = wide.sort_index()
    return _set_attrs(
        wide,
        dataset=dataset,
        as_of=as_of,
        freq=freq,
        fill=fill,
        calendar=calendar,
        adjust=adjust,
    )
