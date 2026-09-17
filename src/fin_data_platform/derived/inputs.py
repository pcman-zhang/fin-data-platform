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
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, text

from fin_data_platform.access import ADJUST_MODES, normalize_as_of
from fin_data_platform.access import read as access_read
from fin_data_platform.access import read_sql as access_read_sql
from fin_data_platform.derived.errors import AsOfNotAligned, FactorNotMaterialized
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
    spec = specs[dataset]
    known_fields = {item.name for item in spec.fields}
    known_outputs = {item.output for item in (spec.derived or [])}
    if field_name not in known_fields | known_outputs:
        raise ValueError(f"派生输入字段不存在：{base}")
    if field_name in known_outputs:
        if mode:
            raise ValueError(f"因子输出不支持口径后缀：{ref}")
        return dataset, field_name, None
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
    tables: dict[str, pa.Table] = {}

    # 因子输入：读上游 latest 投影（as_of 对齐；子图求值见 TASK-3.25）
    for ref, (dataset, output, _mode) in parsed.items():
        spec = dictionary[dataset]
        if output not in {item.output for item in (spec.derived or [])}:
            continue
        tables[ref] = _read_factor_projection(
            engine,
            dataset,
            output,
            as_of=as_of,
            specs=dictionary,
            entity_ids=entity_ids,
            window=window,
        )

    # 数据输入：按「有效口径」分组，缺省后缀与显式同口径合并为一次读取
    groups: dict[tuple[str, str], list[str]] = {}
    for dataset, field_name, mode in parsed.values():
        if field_name in {item.output for item in (dictionary[dataset].derived or [])}:
            continue
        groups.setdefault((dataset, _effective_mode(dataset, mode, dictionary)), []).append(
            field_name
        )

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
            if ref_field in {item.output for item in (dictionary[ref_dataset].derived or [])}:
                continue
            if ref_dataset != dataset or (
                _effective_mode(ref_dataset, ref_mode, dictionary) != effective
            ):
                continue
            columns = list(dict.fromkeys([*business_key, ref_field]))
            tables[ref] = result.table.select(columns)
    return tables


def _filter_frame(
    frame: Any,
    spec: DatasetSpec,
    *,
    entity_ids: Sequence[int] | None,
    window: tuple[date, date] | None,
) -> Any:
    """对因子投影做实体/窗口过滤（与数据输入同一语义）。"""
    import pandas as pd

    if entity_ids is not None:
        if "entity_id" not in frame.columns:
            raise ValueError(f"{spec.dataset}: 投影无 entity_id，无法按实体过滤")
        frame = frame[frame["entity_id"].isin(list(entity_ids))]
    if window is not None:
        event = next((item.name for item in spec.fields if item.pit_role == "event_time"), None)
        if event is None or event not in frame.columns:
            raise ValueError(f"{spec.dataset}: 投影无 event_time 字段，无法按窗口过滤")
        dates = pd.to_datetime(frame[event])
        start, end = window
        frame = frame[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    return frame


def _read_factor_projection(
    engine: Engine,
    dataset: str,
    output: str,
    *,
    as_of: datetime,
    specs: Mapping[str, DatasetSpec],
    entity_ids: Sequence[int] | None = None,
    window: tuple[date, date] | None = None,
) -> pa.Table:
    """读取上游因子投影（单份 latest）；as_of 早于知识锚（computed_at）即报错。"""
    import pandas as pd
    import pyarrow as pa

    from fin_data_platform.derived.engine import projection_name

    spec = specs[dataset]
    entry = next(item for item in (spec.derived or []) if item.output == output)
    projection = projection_name(spec, entry)
    columns = [
        *spec.business_key,
        output,
        "algorithm_id",
        "computed_at",
        "data_generation",
        "upstream_fingerprint",
    ]
    select = ", ".join(f'"{name}"' for name in columns)
    try:
        with engine.connect() as connection:
            frame = pd.read_sql(text(f"SELECT {select} FROM {projection}"), connection)
    except Exception as exc:  # 表不存在/不可读：未物化
        raise FactorNotMaterialized(
            f"上游因子未物化：{dataset}.{output}",
            hint="先物化上游因子（control.materialize）或改用按需因子（TASK-3.25 子图）",
        ) from exc
    if not frame.empty:
        anchor = pd.Timestamp(frame["computed_at"].iloc[0]).to_pydatetime()
        normalized = normalize_as_of(as_of)
        if anchor > normalized:
            raise AsOfNotAligned(
                f"{dataset}.{output}: 投影知识锚 {anchor} 晚于请求 as_of {normalized}",
                hint="改用 version_mode=latest 或等待按需子图能力（TASK-3.25）",
            )
    # 空投影是合法结果（无数据）；过滤后仍可能为空，由调用方按空表处理
    frame = _filter_frame(frame, spec, entity_ids=entity_ids, window=window)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    return table.select([*spec.business_key, output])


def read_factor_projection_meta(
    engine: Engine, dataset: str, output: str, *, specs: Mapping[str, DatasetSpec]
) -> tuple[str | None, str | None, str | None]:
    """读取投影的 (algorithm_id, upstream_fingerprint, data_generation)（物化前置校验用）。"""
    from fin_data_platform.derived.engine import projection_name

    spec = specs[dataset]
    entry = next(item for item in (spec.derived or []) if item.output == output)
    projection = projection_name(spec, entry)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT DISTINCT algorithm_id, upstream_fingerprint, "
                    f"data_generation FROM {projection}"
                )
            ).first()
    except Exception as exc:  # 未物化 / 旧版本投影缺审计列 / 连接失败
        raise FactorNotMaterialized(
            f"上游因子投影不可读：{dataset}.{output}",
            hint="先物化上游因子；若投影由旧版本生成（缺审计列），请重算",
        ) from exc
    if row is None:
        return None, None, None  # 空投影：合法结果，无一致性可校验
    return row[0], row[1], row[2]


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
    "read_factor_projection_meta",
    "input_view_name",
    "normalize_as_of",
    "parse_ref",
    "read_inputs",
]
