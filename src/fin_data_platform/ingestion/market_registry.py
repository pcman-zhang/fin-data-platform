"""全市场基础信息与生命周期同步（TASK-3.35）：Hub 参照数据 → ``ref.entity`` + 生命周期表。

来源：FinDataHub 参照数据（tushare 基础信息）：

- **身份**：``stock_list`` / ``delist_list``（退市股票）/ ``etf_list`` / ``fund_list`` /
  ``index_list`` → ``ref.entity``（新建 + 名称/市场刷新；``entity_class`` 遗留非法值置空）；
- **生命周期**（分级构造，见下）：写入 ``cn_equity.listing_lifecycle``；
- **knowledge_time 口径**：首版 = 区间起点 ``start_date`` 当日的稳定时刻（15:00 CST，
  与日线「首版取收盘时刻」同思路：重跑幂等、历史 as-of 可还原）；修订版本 = 同步时刻；
- **修订语义**：同 ``(entity_id, start_date)`` 的字段（status / end_date）变化 → 追加
  新版本（``version+1``）；未变化不写；**被更正而失效的旧开放行**做「零长度闭合」
  （``end_date = start_date``）标记失效——append-only、不删除历史，且保持 PIT 视图
  区间不重叠；
- **停牌不入本表**（per-day 停牌由 ``cn_equity.daily_status`` 承担）。

生命周期构造规则（字典已冻结）：

- 股票（``stock_list`` / ``delist_list``）：``listed [list_date, delist_date-1]`` +
  ``delisted [delist_date, null]``；未退市仅 ``listed`` 行；
- ETF / 基金：``listed [list_date, null]``（退市字段待真实数据核对后扩展）；
- 指数：仅登记身份，不写 lifecycle；
- 源缺 ``list_date``、退市不晚于上市、或「已退市但缺退市日期」（``list_status=D``）
  的行按跳过计数，不产出可能错误的区间。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

import pandas as pd
from sqlalchemy import Engine, select

from fin_data_hub.errors import UnsupportedCapability
from fin_data_platform.ingestion.common import knowledge_time, resolve_provider
from fin_data_platform.registry._util import clean_code, to_date
from fin_data_platform.registry.models import EntityClass, EntityType, Market
from fin_data_platform.registry.store import EntityStore
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.storage.schema import build_metadata
from fin_data_platform.storage.writers import append_rows

#: 目标数据集（生命周期；身份落在 ref.entity）
DATASET = "cn_equity.listing_lifecycle"

#: 身份清单 → 实体本体
_IDENTITY_KINDS: tuple[tuple[str, str], ...] = (
    ("stock_list", EntityType.EQUITY.value),
    ("delist_list", EntityType.EQUITY.value),
    ("etf_list", EntityType.ETF.value),
    ("fund_list", EntityType.FUND.value),
    ("index_list", EntityType.INDEX.value),
)

#: 生命周期参与类别（指数仅登记身份）
_LIFECYCLE_KINDS = ("stock_list", "delist_list", "etf_list", "fund_list")

#: provider 识别用（生命周期行的来源）
_PROVIDER_KINDS = ("stock_list", "delist_list", "etf_list", "fund_list")

#: 合法 entity_class 枚举（登记不主动细分；遗留非法值置空）
_VALID_CLASSES = frozenset(item.value for item in EntityClass)


@dataclass(frozen=True, slots=True)
class RegistrySyncResult:
    """全市场登记同步结果（统计口径）。"""

    entities_created: int = 0
    entities_refreshed: int = 0
    entities_unchanged: int = 0
    legacy_cleaned: int = 0
    lifecycle_written: int = 0
    lifecycle_revisions: int = 0
    lifecycle_retracted: int = 0
    lifecycle_skipped: int = 0

    @property
    def rows_written(self) -> int:
        """写入行数合计（供 Runtime 运行记录使用）。"""
        return (
            self.entities_created
            + self.entities_refreshed
            + self.lifecycle_written
            + self.lifecycle_revisions
            + self.lifecycle_retracted
        )


@lru_cache(maxsize=1)
def _lifecycle_table():  # type: ignore[no-untyped-def]
    """生命周期表（进程内缓存：build_metadata 开销较大）。"""
    metadata, _specs = build_metadata()
    return metadata.tables[DATASET]


def _reference(hub: Any, kind: str, *, source: Any) -> pd.DataFrame:
    """拉取参照数据；源不支持该 kind 时返回空表（其余错误正常抛出）。

    ``source`` 必须显式转发（Hub 不做隐式路由；缺省会报 ValueError）。
    """
    try:
        frame = hub.get_reference(kind, source=source)
    except (UnsupportedCapability, ValueError):
        return pd.DataFrame()
    return pd.DataFrame() if frame is None else frame


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return [
        {str(key): value for key, value in row.items()}
        for row in frame.to_dict("records")
    ]


def _provider(frames: list[pd.DataFrame], source: Any) -> str:
    for frame in frames:
        if frame is not None and not frame.empty:
            return resolve_provider(frame, source)
    return resolve_provider(pd.DataFrame(), source)


def _latest_rows(engine: Engine) -> dict[int, dict[date, Any]]:
    """库内每个 ``(entity_id, start_date)`` 的当前最新版本行。"""
    table = _lifecycle_table()
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(
                    table.c.entity_id,
                    table.c.start_date,
                    table.c.status,
                    table.c.end_date,
                    table.c.knowledge_time,
                    table.c.version,
                )
            )
            .mappings()
            .all()
        )
    latest: dict[tuple[int, date], Any] = {}
    for row in rows:
        key = (int(row["entity_id"]), row["start_date"])
        current = latest.get(key)
        if current is None or (row["knowledge_time"], row["version"]) > (
            current["knowledge_time"],
            current["version"],
        ):
            latest[key] = row
    grouped: dict[int, dict[date, Any]] = {}
    for (entity_id, start_date), row in latest.items():
        grouped.setdefault(entity_id, {})[start_date] = row
    return grouped


def _intervals(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """单行参照数据 → 目标区间（不可构造时返回 ``None``，由调用方计跳过）。"""
    list_date = to_date(row.get("list_date"))
    if list_date is None:
        return None
    delist = to_date(row.get("delist_date"))
    if delist is not None and delist <= list_date:
        return None
    if delist is None and str(row.get("list_status") or "").strip().upper() == "D":
        return None  # 已退市但缺退市日期：不产出（避免把退市标的标成在市）
    intervals: list[dict[str, Any]] = [
        {
            "status": "listed",
            "start_date": list_date,
            "end_date": delist - timedelta(days=1) if delist is not None else None,
        }
    ]
    if delist is not None:
        intervals.append({"status": "delisted", "start_date": delist, "end_date": None})
    return intervals


def sync_market_registry(
    engine: Engine,
    hub: Any,
    *,
    source: Any = None,
) -> RegistrySyncResult:
    """全市场身份 + 生命周期同步（幂等；口径见模块 docstring）。"""
    frames = {
        kind: _reference(hub, kind, source=source) for kind, _type in _IDENTITY_KINDS
    }
    store = EntityStore(engine)
    created = refreshed = unchanged = legacy = 0
    entities: dict[str, int] = {}

    for kind, entity_type in _IDENTITY_KINDS:
        for row in _records(frames[kind]):
            code = clean_code(row.get("code"))
            if code is None:
                continue
            name = str(row.get("name") or "").strip()
            existing = store.get_entity(code)
            if existing is None:
                entity_record = store.ensure_entity(
                    code=code, entity_type=entity_type, name=name, market=Market.CN.value
                )
                entities[code] = entity_record.entity_id
                created += 1
                continue
            entities[code] = existing.entity_id
            changes: dict[str, Any] = {}
            if name and existing.name != name:
                changes["name"] = name
            if existing.market != Market.CN.value:
                changes["market"] = Market.CN.value
            if existing.entity_class is not None and existing.entity_class not in _VALID_CLASSES:
                changes["entity_class"] = None  # 遗留非法分类置空（不猜测细分）
            if changes:
                store.update_entity(code=code, **changes)
                refreshed += 1
                legacy += int("entity_class" in changes)
            else:
                unchanged += 1

    skipped = 0
    targets: dict[int, list[dict[str, Any]]] = {}
    for kind in _LIFECYCLE_KINDS:
        for row in _records(frames[kind]):
            code = clean_code(row.get("code"))
            entity_id = entities.get(code) if code else None
            if entity_id is None:
                skipped += 1
                continue
            intervals = _intervals(row)
            if intervals is None:
                skipped += 1
                continue
            targets[entity_id] = intervals

    if not targets:
        return RegistrySyncResult(
            entities_created=created,
            entities_refreshed=refreshed,
            entities_unchanged=unchanged,
            legacy_cleaned=legacy,
            lifecycle_skipped=skipped,
        )

    provider = _provider([frames[kind] for kind in _PROVIDER_KINDS], source)
    latest = _latest_rows(engine)
    now = utcnow()
    rows: list[dict[str, Any]] = []
    written = revisions = retracted = 0

    for entity_id, intervals in sorted(targets.items()):
        prior = latest.get(entity_id, {})
        target_starts = {item["start_date"] for item in intervals}
        for item in intervals:
            previous = prior.get(item["start_date"])
            insert_row = {
                "entity_id": entity_id,
                "status": item["status"],
                "start_date": item["start_date"],
                "end_date": item["end_date"],
                "knowledge_time": (
                    now if previous is not None else knowledge_time(item["start_date"])
                ),
                "version": int(previous["version"]) + 1 if previous is not None else 1,
                "provider": provider,
            }
            if previous is None:
                rows.append(insert_row)
                written += 1
            elif (
                previous["status"] != item["status"]
                or previous["end_date"] != item["end_date"]
            ):
                rows.append(insert_row)
                revisions += 1
            else:
                skipped += 1
        # 被更正而失效的旧开放行：零长度闭合（保持 PIT 视图区间不重叠、保留审计）
        for start_date, previous in prior.items():
            if start_date in target_starts or previous["end_date"] is not None:
                continue
            rows.append(
                {
                    "entity_id": entity_id,
                    "status": previous["status"],
                    "start_date": start_date,
                    "end_date": start_date,
                    "knowledge_time": now,
                    "version": int(previous["version"]) + 1,
                    "provider": provider,
                }
            )
            retracted += 1

    if rows:
        with engine.begin() as connection:
            append_rows(connection, _lifecycle_table(), rows)

    return RegistrySyncResult(
        entities_created=created,
        entities_refreshed=refreshed,
        entities_unchanged=unchanged,
        legacy_cleaned=legacy,
        lifecycle_written=written,
        lifecycle_revisions=revisions,
        lifecycle_retracted=retracted,
        lifecycle_skipped=skipped,
    )
