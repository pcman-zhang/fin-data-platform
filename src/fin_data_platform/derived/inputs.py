"""派生输入读取（经访问面执行；doc-10 §3.1、doc-11 §3.6）。

- 引用语法：``dataset.field[@mode]``，``mode ∈ {raw, qfq, hfq}``；
  缺省取数据集字典声明的 ``adjust.default``（不登记 = raw）；
- PIT（as-of）与口径组合统一由访问面执行（单一实现，本模块只做引用解析与投影）；
- 返回 ``{引用原文: Arrow 表}``，列 = 数据集业务键 + 该字段。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Engine

from fin_data_platform.access import ADJUST_MODES, normalize_as_of
from fin_data_platform.access import read as access_read
from fin_data_platform.access import read_sql as access_read_sql
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec

if TYPE_CHECKING:
    import pyarrow as pa

#: 视图名安全字符（非字母数字统一折叠为 ``__``，含 ``.`` 与 ``@``）
_VIEW_SAFE = re.compile(r"[^0-9a-zA-Z_]+")


def input_view_name(ref: str) -> str:
    """输入引用的计算视图名（算法 SQL 与内联模板共用）。"""
    return _VIEW_SAFE.sub("__", ref).strip("_")


def parse_ref(ref: str, specs: Mapping[str, DatasetSpec]) -> tuple[str, str, str | None]:
    """解析 ``dataset.field[@mode]`` → ``(dataset, field, mode)``（mode 可为 None）。"""
    base, _, mode = ref.partition("@")
    dataset, separator, field_name = base.rpartition(".")
    if not separator or dataset not in specs:
        raise ValueError(f"派生输入数据集不存在：{ref}")
    if field_name not in {item.name for item in specs[dataset].fields}:
        raise ValueError(f"派生输入字段不存在：{base}")
    if mode and mode not in ADJUST_MODES:
        raise ValueError(f"派生输入口径非法：{ref}（可选 {sorted(ADJUST_MODES)}）")
    if mode and mode != "raw":
        declared = specs[dataset].adjust
        allowed = tuple(declared.fields) if declared else ()
        if field_name not in allowed:
            raise ValueError(f"派生输入字段不可复权：{base}（可复权字段 {list(allowed) or '无'}）")
    return dataset, field_name, mode or None


def _effective_mode(dataset: str, mode: str | None, specs: Mapping[str, DatasetSpec]) -> str:
    """有效口径：显式后缀优先，否则取字典 default（none → raw）。"""
    if mode:
        return mode
    declared = specs[dataset].adjust
    default = declared.default if declared else "none"
    return "raw" if default == "none" else default


def read_inputs(
    engine: Engine,
    refs: Sequence[str],
    *,
    as_of: datetime,
    specs: Mapping[str, DatasetSpec] | None = None,
    entity_ids: Sequence[int] | None = None,
    window: tuple[date, date] | None = None,
) -> dict[str, pa.Table]:
    """按引用读取派生输入（同数据集同口径一次读取，多处投影）。"""
    dictionary = specs if specs is not None else load_all()
    parsed = {ref: parse_ref(ref, dictionary) for ref in dict.fromkeys(refs)}
    # 按「有效口径」分组：缺省后缀与显式同口径合并为一次读取
    groups: dict[tuple[str, str], list[str]] = {}
    for dataset, field_name, mode in parsed.values():
        groups.setdefault((dataset, _effective_mode(dataset, mode, dictionary)), []).append(
            field_name
        )

    tables: dict[str, pa.Table] = {}
    for (dataset, effective), fields in groups.items():
        result = access_read(
            engine,
            dataset,
            list(dict.fromkeys(fields)),
            as_of=as_of,
            adjust=effective,
            entities=entity_ids,
            window=window,
            specs=dictionary,
        )
        business_key = list(dictionary[dataset].business_key)
        for ref, (ref_dataset, ref_field, ref_mode) in parsed.items():
            if ref_dataset != dataset or (
                _effective_mode(ref_dataset, ref_mode, dictionary) != effective
            ):
                continue
            columns = list(dict.fromkeys([*business_key, ref_field]))
            tables[ref] = result.table.select(columns)
    return tables


def inline_input_sql(
    ref: str,
    *,
    as_of: datetime,
    specs: Mapping[str, DatasetSpec] | None = None,
    window: tuple[date, date] | None = None,
) -> str:
    """渲染单个输入引用的内联 SQL（字面量参数；含口径组合）。"""
    dictionary = specs if specs is not None else load_all()
    dataset, field_name, mode = parse_ref(ref, dictionary)
    sql, _params, _effective = access_read_sql(
        dictionary[dataset],
        [field_name],
        as_of=as_of,
        adjust=mode,
        specs=dictionary,
        window=window,
        literal=True,
    )
    return sql


__all__ = [
    "inline_input_sql",
    "input_view_name",
    "normalize_as_of",
    "parse_ref",
    "read_inputs",
]
