"""源侧读取（TASK-3.32 步骤③）：Hub 行情 × 落库日历/状态（pandas 内合成）。

定位：**源层读取**——直连 Hub 取行情（不减损接入层能力），叠加平台落库的参照
数据（``ref.trade_calendar``、``cn_equity.daily_status``），把「交易日 × 标的」
补齐为预期行并标注状态，供缺口检查 / 对账 / 状态感知消费使用。

硬约束：所有跨表组合在 pandas 程序内完成，**SQL 不得 JOIN**（仅单表 SELECT）。

三态语义（非交易日无行——"无需有"，不是缺失）：

- ``ok``：交易日且行情存在（停牌日部分时段有成交亦归入此类，以 bar 存在为准）；
- ``suspended``：交易日、停牌且无行情（正常无交易，不是数据质量问题）；
- ``missing``：交易日、非停牌但无行情（数据质量问题，值以 NaN 表达，不填充）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

import pandas as pd
from sqlalchemy import Engine, select

from fin_data_hub.codes import SecCode, parse_codes
from fin_data_platform.registry._util import to_date
from fin_data_platform.storage.schema import build_metadata

#: 行情数据集（字典键）
DATASET = "cn_equity.daily_bar"

#: 状态推导使用的交易所日历（iso MIC；沪深日历一致，单边即可）
DEFAULT_EXCHANGE = "XSHG"

#: 状态取值
STATUS_OK = "ok"
STATUS_SUSPENDED = "suspended"
STATUS_MISSING = "missing"

#: 输出行情字段（Hub BARS_COLUMNS 子集；无值时 NaN）
BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount")

_BAR_COLUMNS = ("code", "trade_date", "_has_bar", *BAR_FIELDS)
_STATUS_COLUMNS = ("entity_id", "trade_date", "is_suspended", "is_st")
_OUTPUT_COLUMNS = ("code", "trade_date", "status", "is_suspended", "is_st", *BAR_FIELDS)


class SourceReadError(Exception):
    """源侧读取异常基类（结构化：code / hint）。"""

    code = "source_read_error"

    def __init__(self, detail: str, *, hint: str = "") -> None:
        self.detail = detail
        self.hint = hint
        super().__init__(detail)

    def __str__(self) -> str:
        return f"{self.detail}（{self.hint}）" if self.hint else self.detail


class UnknownEntity(SourceReadError):
    """代码在实体注册表中不存在（状态无法按 entity_id 对齐）。"""

    code = "unknown_entity"


@dataclass(frozen=True, slots=True)
class SourceReadMeta:
    """读取元数据（窗口、日历与三态计数、取数来源）。"""

    dataset: str
    codes: tuple[str, ...]
    start: date
    end: date
    exchange: str
    trading_days: int
    row_count: int
    ok_rows: int
    suspended_rows: int
    missing_rows: int
    provider: str | None


@dataclass(frozen=True, slots=True)
class SourceReadResult:
    """源侧读取结果：对齐后的 DataFrame + 元数据。"""

    frame: pd.DataFrame
    meta: SourceReadMeta


@lru_cache(maxsize=1)
def _tables() -> tuple[Any, Any, Any]:
    """（日历、每日状态、实体）表对象（进程内缓存：build_metadata 开销较大）。"""
    metadata, _specs = build_metadata()
    return (
        metadata.tables["ref.trade_calendar"],
        metadata.tables["cn_equity.daily_status"],
        metadata.tables["ref.entity"],
    )


def _empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype="object") for name in columns})


def _trading_days(
    connection: Any, calendar: Any, *, exchange: str, start: date, end: date
) -> list[date]:
    """落库日历中窗口内的交易日（单表 SELECT；非交易日不产出预期行）。"""
    rows = (
        connection.execute(
            select(calendar.c.trade_date).where(
                calendar.c.exchange_id == exchange,
                calendar.c.trade_date >= start,
                calendar.c.trade_date <= end,
                calendar.c.is_open.is_(True),
            )
        )
        .scalars()
        .all()
    )
    days: set[date] = set()
    for row in rows:
        day = to_date(row)
        if day is not None:
            days.add(day)
    return sorted(days)


def _entity_map(connection: Any, entity_table: Any, codes: Sequence[str]) -> dict[str, int]:
    """canonical 代码 → entity_id（SCD2 取最高版本；未注册代码显式报错）。"""
    rows = (
        connection.execute(
            select(
                entity_table.c.code,
                entity_table.c.entity_id,
                entity_table.c.knowledge_time,
                entity_table.c.version,
            )
            .where(entity_table.c.code.in_(list(codes)))
            .order_by(entity_table.c.code, entity_table.c.version, entity_table.c.knowledge_time)
        )
        .mappings()
        .all()
    )
    mapping: dict[str, int] = {}
    for row in rows:
        mapping[str(row["code"])] = int(row["entity_id"])
    missing = [code for code in codes if code not in mapping]
    if missing:
        raise UnknownEntity(
            f"代码未注册实体: {missing}",
            hint="先经采集/实体注册建立身份（ref.entity），每日状态按 entity_id 对齐",
        )
    return mapping


def _latest_status(
    connection: Any, table: Any, *, entity_ids: Sequence[int], start: date, end: date
) -> pd.DataFrame:
    """窗口内每个「实体 × 交易日」的当前最新状态版本（单表 SELECT，pandas 去重）。"""
    if not entity_ids:
        return _empty_frame(_STATUS_COLUMNS)
    rows = (
        connection.execute(
            select(
                table.c.entity_id,
                table.c.trade_date,
                table.c.is_suspended,
                table.c.is_st,
                table.c.knowledge_time,
                table.c.version,
            ).where(
                table.c.entity_id.in_(list(entity_ids)),
                table.c.trade_date >= start,
                table.c.trade_date <= end,
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return _empty_frame(_STATUS_COLUMNS)
    frame = pd.DataFrame([dict(row) for row in rows])
    frame = frame.sort_values(
        ["entity_id", "trade_date", "knowledge_time", "version"], kind="stable"
    )
    frame = frame.drop_duplicates(subset=["entity_id", "trade_date"], keep="last")
    return frame.loc[:, list(_STATUS_COLUMNS)].reset_index(drop=True)


def _normalize_bars(frame: Any, codes: Sequence[str]) -> pd.DataFrame:
    """Hub 行情 → 规范列（code / trade_date / 数值字段 / ``_has_bar`` 标记）。"""
    if frame is None or len(frame) == 0:
        return _empty_frame(_BAR_COLUMNS)
    if "code" not in frame.columns or "date" not in frame.columns:
        raise SourceReadError(
            "Hub 行情缺少 code/date 列",
            hint="检查 hub.get_bars 输出契约（BARS_COLUMNS）",
        )
    out = frame.copy()
    out["code"] = out["code"].map(str)
    out["trade_date"] = [to_date(value) for value in out["date"]]
    out = out[out["trade_date"].notna() & out["code"].isin(set(codes))]
    if out.empty:
        return _empty_frame(_BAR_COLUMNS)
    for field in BAR_FIELDS:
        out[field] = (
            pd.to_numeric(out[field], errors="coerce") if field in out.columns else float("nan")
        )
    out = out.drop_duplicates(subset=["code", "trade_date"], keep="last")
    out["_has_bar"] = True
    return out.loc[:, list(_BAR_COLUMNS)].reset_index(drop=True)


def read_bars_with_status(
    engine: Engine,
    hub: Any,
    codes: str | SecCode | Sequence[str | SecCode],
    *,
    start: date | str,
    end: date | str,
    source: Any = None,
    adjust: str | None = None,
    exchange: str = DEFAULT_EXCHANGE,
) -> SourceReadResult:
    """读取「交易日 × 标的」行情并标注状态（源侧；pandas 合成，SQL 不 JOIN）。

    预期行 = 落库日历的交易日 × 请求代码；行情经 Hub 直连取数（窗口内按代码批量
    一次），停牌行保留但数值为空；非交易日不产出行。读路径不写库。
    """
    window_start = to_date(start)
    window_end = to_date(end)
    if window_start is None or window_end is None or window_start > window_end:
        raise SourceReadError(f"窗口非法: start={start!r}, end={end!r}")
    values = [codes] if isinstance(codes, (str, SecCode)) else list(codes)
    canonical = [item.canonical for item in parse_codes(values)]
    if not canonical:
        raise SourceReadError("codes 不能为空", hint="至少提供一个标的代码")

    calendar, status_table, entity_table = _tables()
    with engine.connect() as connection:
        days = _trading_days(
            connection, calendar, exchange=exchange, start=window_start, end=window_end
        )
        entities = _entity_map(connection, entity_table, canonical)
        status = _latest_status(
            connection,
            status_table,
            entity_ids=list(entities.values()),
            start=window_start,
            end=window_end,
        )

    if not days:
        # 无交易日：无需取数（避免空窗口的源调用与配额消耗）
        return SourceReadResult(
            frame=_empty_frame(_OUTPUT_COLUMNS),
            meta=SourceReadMeta(
                dataset=DATASET,
                codes=tuple(canonical),
                start=window_start,
                end=window_end,
                exchange=exchange,
                trading_days=0,
                row_count=0,
                ok_rows=0,
                suspended_rows=0,
                missing_rows=0,
                provider=None,
            ),
        )

    bars_raw = hub.get_bars(
        canonical,
        start=window_start.isoformat(),
        end=window_end.isoformat(),
        freq="1d",
        adjust=adjust,
        source=source,
    )
    provider_raw = getattr(bars_raw, "attrs", {}).get("source")
    provider = str(provider_raw) if provider_raw else None
    bars = _normalize_bars(bars_raw, canonical)

    grid = pd.DataFrame(
        [(code, day) for code in canonical for day in days],
        columns=["code", "trade_date"],
    )
    merged = grid.merge(bars, on=["code", "trade_date"], how="left", sort=False)
    merged["_has_bar"] = merged["_has_bar"].fillna(False).astype(bool)
    merged["entity_id"] = merged["code"].map(entities)
    merged = merged.merge(status, on=["entity_id", "trade_date"], how="left", sort=False)
    for field in ("is_suspended", "is_st"):
        merged[field] = merged[field].fillna(False).astype(bool)

    status_col = pd.Series(STATUS_MISSING, index=merged.index, dtype="object")
    status_col = status_col.mask(merged["is_suspended"], STATUS_SUSPENDED)
    status_col = status_col.mask(merged["_has_bar"], STATUS_OK)
    merged["status"] = status_col

    out = merged.loc[:, list(_OUTPUT_COLUMNS)]
    out = out.sort_values(["code", "trade_date"], kind="stable").reset_index(drop=True)
    counts = out["status"].value_counts()
    return SourceReadResult(
        frame=out,
        meta=SourceReadMeta(
            dataset=DATASET,
            codes=tuple(canonical),
            start=window_start,
            end=window_end,
            exchange=exchange,
            trading_days=len(days),
            row_count=len(out),
            ok_rows=int(counts.get(STATUS_OK, 0)),
            suspended_rows=int(counts.get(STATUS_SUSPENDED, 0)),
            missing_rows=int(counts.get(STATUS_MISSING, 0)),
            provider=provider,
        ),
    )
