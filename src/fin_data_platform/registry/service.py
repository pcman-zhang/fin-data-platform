"""实体注册表服务：注册/刷新、代码解析、关系/外部标识、as-of 属性还原（doc-10 §3.3）。

存储落地（DDL/迁移/持久化仓储）由 TASK-3.3 接入；本模块为存储无关核心逻辑。

- 分类面：``entity_type`` / ``entity_class`` / ``market``（替代旧 ``sec_type``）；
- 交易状态不在注册表：PIT Universe 由 :mod:`fin_data_platform.registry.universe`
  从交易状态数据集（``cn_equity.listing_lifecycle``）推导；
- 关系单向存储，双向查询由词表 ``inverse_relation`` 驱动（零硬编码）；
- 区间语义：SCD2 行 ``valid_from <= as_of <= valid_to``（闭区间，``valid_to=None`` 表示至今）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import pandas as pd

from fin_data_hub.errors import UnsupportedCapability
from fin_data_platform.registry._util import (
    EPOCH,
    clean_code,
    covers,
    now,
    to_date,
)
from fin_data_platform.registry.models import (
    BuildStats,
    CodeHistoryRecord,
    EntityClass,
    EntityRecord,
    EntityType,
    ExternalIdRecord,
    IdType,
    Market,
    RelationRecord,
    RelationTypeRecord,
    SocialStatus,
)
from fin_data_platform.registry.relation_types import (
    load_relation_types,
    validate_relation_types,
)
from fin_data_platform.registry.repository import (
    EntityRegistryRepository,
    InMemoryEntityRegistryRepository,
)

#: Hub 基础信息种类 → 实体本体（首期 A 股市场）
_KIND_ENTITY_TYPE = {
    "stock_list": EntityType.EQUITY,
    "etf_list": EntityType.ETF,
    "fund_list": EntityType.FUND,
    "index_list": EntityType.INDEX,
}

_EXCHANGE_VENUES = frozenset({"SH", "SZ", "BJ"})


def _facet(value: Any, enum_cls: Any, label: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{label} 不能为空")
        return None
    try:
        return enum_cls(value).value
    except ValueError as exc:
        allowed = ", ".join(str(item.value) for item in enum_cls)
        raise ValueError(f"{label} 非法: {value!r}（可选: {allowed}）") from exc


def _bump(
    stats: BuildStats,
    *,
    registered: int = 0,
    updated: int = 0,
    attributes: int = 0,
    skipped: int = 0,
) -> BuildStats:
    return BuildStats(
        registered=stats.registered + registered,
        updated=stats.updated + updated,
        attributes=stats.attributes + attributes,
        skipped=stats.skipped + skipped,
    )


def _venue(code: str) -> str:
    return code.rsplit(".", 1)[-1].upper() if "." in code else ""


def _market_from_code(code: str) -> str:
    venue = _venue(code)
    if venue == "HK":
        return Market.HK.value
    if venue in {"O", "N", "A"}:
        return Market.US.value
    return Market.CN.value


def _listing_type(kind: str, code: str, default: str) -> str:
    if kind == "fund_list" and _venue(code) in _EXCHANGE_VENUES:
        return EntityType.LOF.value
    return default


class EntityRegistry:
    """实体身份与 PIT 履历（issuer / equity / etf / lof / fund / index / …）。"""

    def __init__(
        self,
        repository: EntityRegistryRepository | None = None,
        *,
        relation_types: dict[str, RelationTypeRecord] | None = None,
    ) -> None:
        self._repo = repository or InMemoryEntityRegistryRepository()
        vocab = load_relation_types() if relation_types is None else dict(relation_types)
        errors = validate_relation_types(vocab)
        if errors:
            raise ValueError("关系词表非法: " + "; ".join(errors))
        self._relation_types = vocab
        for record in self._relation_types.values():
            self._repo.append_relation_type(record)

    # ------------------------------------------------------------ 注册/刷新
    def register(
        self,
        *,
        code: str,
        entity_type: str,
        name: str = "",
        entity_class: str | None = None,
        market: str | None = None,
        currency: str | None = None,
        exchange: str | None = None,
        frequency: str | None = None,
        unit: str | None = None,
        algorithm_id: str | None = None,
        social_status: str | None = None,
        valid_from: Any = None,
        attrs: dict[str, Any] | None = None,
    ) -> EntityRecord:
        """注册或刷新实体（按 canonical code 幂等；变化以 SCD2 新行表达）。

        ``entity_type`` 为身份分类：实体一旦建立，后续刷新不改变本体类型
        （首见优先；例如基金列表与 ETF 列表的交集）。
        """
        record, _, _ = self._register_or_refresh(
            code=code,
            entity_type=entity_type,
            name=name,
            entity_class=entity_class,
            market=market,
            currency=currency,
            exchange=exchange,
            frequency=frequency,
            unit=unit,
            algorithm_id=algorithm_id,
            social_status=social_status,
            valid_from=valid_from,
            attrs=attrs,
        )
        return record

    def register_issuer(
        self,
        *,
        code: str,
        name: str,
        social_status: str = SocialStatus.OPERATING.value,
        currency: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> EntityRecord:
        """注册社会实体（issuer）；``code`` 优先取统一社会信用代码（缺失时平台码）。"""
        return self.register(
            code=code,
            entity_type=EntityType.ISSUER.value,
            name=name,
            social_status=social_status,
            currency=currency,
            attrs=attrs,
        )

    def _register_or_refresh(
        self,
        *,
        code: str,
        entity_type: str,
        name: str = "",
        entity_class: str | None = None,
        market: str | None = None,
        currency: str | None = None,
        exchange: str | None = None,
        frequency: str | None = None,
        unit: str | None = None,
        algorithm_id: str | None = None,
        social_status: str | None = None,
        valid_from: Any = None,
        attrs: dict[str, Any] | None = None,
    ) -> tuple[EntityRecord, bool, bool]:
        """返回 ``(record, created, updated)``。"""
        entity_id = self.resolve(str(code))
        if entity_id is None:
            return (
                self._create(
                    code=str(code),
                    entity_type=entity_type,
                    name=name,
                    entity_class=entity_class,
                    market=market,
                    currency=currency,
                    exchange=exchange,
                    frequency=frequency,
                    unit=unit,
                    algorithm_id=algorithm_id,
                    social_status=social_status,
                    valid_from=valid_from,
                    attrs=attrs,
                ),
                True,
                False,
            )
        updated = self._refresh(
            entity_id,
            name=name,
            entity_class=entity_class,
            market=market,
            currency=currency,
            exchange=exchange,
            frequency=frequency,
            unit=unit,
            algorithm_id=algorithm_id,
            social_status=social_status,
        )
        return self._current_row(entity_id), False, updated

    def _create(
        self,
        *,
        code: str,
        entity_type: str,
        name: str,
        entity_class: str | None,
        market: str | None,
        currency: str | None,
        exchange: str | None,
        frequency: str | None,
        unit: str | None,
        algorithm_id: str | None,
        social_status: str | None,
        valid_from: Any,
        attrs: dict[str, Any] | None,
    ) -> EntityRecord:
        etype = _facet(entity_type, EntityType, "entity_type", required=True)
        assert etype is not None
        eclass = _facet(entity_class, EntityClass, "entity_class")
        emarket = _facet(market, Market, "market")
        sstatus = _facet(social_status, SocialStatus, "social_status")
        if sstatus is not None and etype != EntityType.ISSUER.value:
            raise ValueError("social_status 仅 issuer 使用")
        if etype == EntityType.ISSUER.value and sstatus is None:
            sstatus = SocialStatus.OPERATING.value
        start = to_date(valid_from) or EPOCH
        base = EntityRecord(
            entity_id=self._repo.next_entity_id(),
            entity_type=etype,
            code=code,
            name=str(name or ""),
            entity_class=eclass,
            market=emarket,
            currency=currency,
            exchange=exchange,
            frequency=frequency,
            unit=unit,
            algorithm_id=algorithm_id,
            social_status=sstatus,
            valid_from=start,
            knowledge_time=now(),
            attrs=attrs,
        )
        self._repo.append_code(
            CodeHistoryRecord(
                entity_id=base.entity_id,
                code=base.code,
                valid_from=base.valid_from,
                knowledge_time=base.knowledge_time,
            )
        )
        self._repo.append_entity(base)
        return base

    def _refresh(
        self,
        entity_id: int,
        *,
        name: str,
        entity_class: str | None,
        market: str | None,
        currency: str | None,
        exchange: str | None,
        frequency: str | None,
        unit: str | None,
        algorithm_id: str | None,
        social_status: str | None,
    ) -> bool:
        current = self._current_row(entity_id)
        updates: dict[str, Any] = {}
        if name and str(name) != current.name:
            updates["name"] = str(name)
        for field, raw, enum_cls, label in (
            ("entity_class", entity_class, EntityClass, "entity_class"),
            ("market", market, Market, "market"),
            ("social_status", social_status, SocialStatus, "social_status"),
        ):
            if raw is None:
                continue
            value = _facet(raw, enum_cls, label)
            if field == "social_status" and current.entity_type != EntityType.ISSUER.value:
                raise ValueError("social_status 仅 issuer 使用")
            if value != getattr(current, field):
                updates[field] = value
        for field, raw in (
            ("currency", currency),
            ("exchange", exchange),
            ("frequency", frequency),
            ("unit", unit),
            ("algorithm_id", algorithm_id),
        ):
            if raw is not None and raw != getattr(current, field):
                updates[field] = raw
        if not updates:
            return False
        self._append_row(replace(current, **updates), valid_from=current.valid_from)
        return True

    def add_name_change(
        self,
        entity_id: int,
        *,
        name: str,
        start_date: Any,
        end_date: Any = None,
        ann_date: Any = None,
    ) -> None:
        """名称变更：闭合覆盖行 + 追加新名称行（SCD2）。"""
        start = to_date(start_date)
        if start is None:
            raise ValueError(f"名称变更 start_date 无效: {start_date!r}")
        end = to_date(end_date)
        before = start - timedelta(days=1)
        previous = self._covering_row(entity_id, before)
        resume_name = (
            previous.name if previous is not None else self._current_row(entity_id).name
        )
        if previous is not None and (
            previous.valid_to is None or previous.valid_to > before
        ):
            self._close_row(previous, before)
        current = self._current_row(entity_id)
        self._append_row(
            current,
            name=str(name),
            valid_from=start,
            valid_to=end,
            attrs_extra={"ann_date": ann_date} if ann_date else None,
        )
        if end is not None:
            self._append_row(
                current,
                name=resume_name,
                valid_from=end + timedelta(days=1),
            )

    def _close_row(self, row: EntityRecord, valid_to: date | None) -> None:
        self._repo.append_entity(
            replace(
                row,
                valid_to=valid_to,
                knowledge_time=now(),
                version=self._next_version(row.entity_id),
            )
        )

    def _append_row(
        self,
        base: EntityRecord,
        *,
        code: str | None = None,
        name: str | None = None,
        valid_from: date | None = None,
        valid_to: date | None = None,
        attrs_extra: dict[str, Any] | None = None,
    ) -> EntityRecord:
        attrs = dict(base.attrs or {})
        if attrs_extra:
            attrs.update(attrs_extra)
        record = replace(
            base,
            code=code if code is not None else base.code,
            name=name if name is not None else base.name,
            valid_from=valid_from if valid_from is not None else base.valid_from,
            valid_to=valid_to,
            knowledge_time=now(),
            version=self._next_version(base.entity_id),
            attrs=attrs or None,
        )
        self._repo.append_entity(record)
        return record

    def _current_row(self, entity_id: int) -> EntityRecord:
        """当前有效行：优先 open 区间（valid_to=None），版本单调递增。"""
        rows = self._repo.entity_rows(entity_id)
        if not rows:
            raise KeyError(f"entity_id 不存在: {entity_id}")
        open_rows = [row for row in rows if row.valid_to is None]
        pool = open_rows or rows
        return max(pool, key=lambda row: (row.valid_from or EPOCH, row.version))

    def _next_version(self, entity_id: int) -> int:
        return max(row.version for row in self._repo.entity_rows(entity_id)) + 1

    def _covering_row(self, entity_id: int, as_of: date | None) -> EntityRecord | None:
        rows = self._repo.entity_rows(entity_id)
        if not rows:
            return None
        if as_of is None:
            return self._current_row(entity_id)
        candidates = [row for row in rows if covers(row, as_of)]
        if not candidates:
            return None
        return max(candidates, key=lambda row: (row.valid_from or EPOCH, row.version))

    def _require_entity(self, entity_id: int) -> EntityRecord:
        if not self._repo.entity_rows(entity_id):
            raise KeyError(f"entity_id 不存在: {entity_id}")
        return self._current_row(entity_id)

    # ------------------------------------------------------------ 查询
    def resolve(self, code: str, *, as_of: Any = None) -> int | None:
        """canonical 代码（含历史代码）→ entity_id。

        append-only 仓储可能保留同键旧版本；按 ``(entity_id, code, valid_from)``
        取最新 version 后再做有效期过滤。
        """
        target = to_date(as_of)
        latest: dict[tuple[int, str, date | None], CodeHistoryRecord] = {}
        for entry in self._repo.find_by_code(code):
            key = (entry.entity_id, entry.code, entry.valid_from)
            if key not in latest or entry.version > latest[key].version:
                latest[key] = entry
        entries = sorted(
            latest.values(),
            key=lambda item: (item.valid_from or EPOCH, item.version),
            reverse=True,
        )
        for entry in entries:
            if target is None:
                return entry.entity_id
            if entry.valid_from is not None and target < entry.valid_from:
                continue
            if entry.valid_to is not None and target > entry.valid_to:
                continue
            return entry.entity_id
        return None

    def entity(self, entity_id: int, *, as_of: Any = None) -> EntityRecord | None:
        target = to_date(as_of)
        if target is None:
            rows = self._repo.entity_rows(entity_id)
            return self._current_row(entity_id) if rows else None
        return self._covering_row(entity_id, target)

    def entity_ids(self) -> list[int]:
        return sorted({row.entity_id for row in self._repo.all_rows()})

    def entity_many(
        self, entity_ids: Iterable[int], *, as_of: Any = None
    ) -> dict[int, EntityRecord]:
        """批量身份查询（服务实现逐实体查询；读面 ``RegistryReader`` 为单次 SQL）。"""
        result: dict[int, EntityRecord] = {}
        for entity_id in dict.fromkeys(int(item) for item in entity_ids):
            record = self.entity(entity_id, as_of=as_of)
            if record is not None:
                result[entity_id] = record
        return result

    def code_history(self, entity_id: int) -> list[CodeHistoryRecord]:
        return self._repo.code_history(entity_id)

    def add_code_change(self, entity_id: int, *, new_code: str, start_date: Any) -> None:
        """canonical 代码变更：闭合旧代码条目 + 追加新代码（历史码仍可解析）。"""
        start = to_date(start_date)
        if start is None:
            raise ValueError(f"代码变更 start_date 无效: {start_date!r}")
        before = start - timedelta(days=1)
        for entry in self._repo.code_history(entity_id):
            if entry.valid_to is None:
                self._repo.append_code(
                    replace(
                        entry,
                        valid_to=before,
                        knowledge_time=now(),
                        version=entry.version + 1,
                    )
                )
        current = self._current_row(entity_id)
        self._close_row(current, before)
        self._repo.append_code(
            CodeHistoryRecord(
                entity_id=entity_id,
                code=str(new_code),
                valid_from=start,
                knowledge_time=now(),
            )
        )
        self._append_row(current, code=str(new_code), valid_from=start)

    def name_as_of(self, entity_id: int, as_of: Any) -> str | None:
        row = self._covering_row(entity_id, to_date(as_of))
        return row.name if row else None

    # ------------------------------------------------------------ 关系与外部标识
    def relation_types(self) -> list[RelationTypeRecord]:
        return sorted(self._relation_types.values(), key=lambda item: item.relation_type)

    def add_relation(
        self,
        entity_id: int,
        related_id: int,
        relation_type: str,
        *,
        valid_from: Any = None,
        valid_to: Any = None,
    ) -> RelationRecord:
        """登记单向关系；关系词必须已登记（CI 门禁）。同一对实体重复登记幂等。"""
        if relation_type not in self._relation_types:
            raise ValueError(
                f"关系词未登记: {relation_type!r}"
                "（先登记 relation_types.yaml / ref.relation_type_dict）"
            )
        self._require_entity(entity_id)
        self._require_entity(related_id)
        for row in self._repo.relation_rows(entity_id):
            if (
                row.related_id == related_id
                and row.relation_type == relation_type
                and row.valid_to is None
            ):
                return row
        record = RelationRecord(
            entity_id=entity_id,
            related_id=related_id,
            relation_type=relation_type,
            valid_from=to_date(valid_from) or EPOCH,
            valid_to=to_date(valid_to),
            knowledge_time=now(),
        )
        self._repo.append_relation(record)
        return record

    def related_ids(
        self, entity_id: int, relation_type: str, *, as_of: Any = None
    ) -> list[int]:
        """按词表语义返回相关实体 id（双向查询，零硬编码）。

        - 存储方向命中：``entity_id --relation_type--> related_id``；
        - 反向命中：``entity_id --inverse(relation_type)--> related_id`` 的存储行反查
          （即查询词为存储词的 ``inverse_relation``）。
        """
        if relation_type not in self._relation_types:
            raise ValueError(f"关系词未登记: {relation_type!r}")
        inverse = self._relation_types[relation_type].inverse_relation
        target = to_date(as_of)
        forward = [
            row.related_id
            for row in self._repo.relation_rows(entity_id)
            if row.relation_type == relation_type and covers(row, target)
        ]
        backward = [
            row.entity_id
            for row in self._repo.find_relations_by_related(entity_id)
            if row.relation_type == inverse and covers(row, target)
        ]
        return sorted(set(forward) | set(backward))

    def relations_of(self, entity_id: int, *, as_of: Any = None) -> list[RelationRecord]:
        """实体相关的全部关系行（存储双向；仅 open/覆盖行）。"""
        target = to_date(as_of)
        rows = [
            *self._repo.relation_rows(entity_id),
            *self._repo.find_relations_by_related(entity_id),
        ]
        return [row for row in rows if covers(row, target)]

    def link_issuer(self, listing_id: int, issuer_id: int) -> RelationRecord:
        """``listing --issued_by--> issuer``。"""
        issuer = self._require_entity(issuer_id)
        if issuer.entity_type != EntityType.ISSUER.value:
            raise ValueError(f"entity_id={issuer_id} 不是 issuer")
        return self.add_relation(listing_id, issuer_id, "issued_by")

    def issuer_of(self, listing_id: int) -> int | None:
        ids = self.related_ids(listing_id, "issued_by")
        return ids[0] if ids else None

    def listings_of(self, issuer_id: int) -> list[int]:
        return self.related_ids(issuer_id, "issues")

    def add_external_id(
        self,
        entity_id: int,
        id_type: str,
        id_value: str,
        *,
        valid_from: Any = None,
        valid_to: Any = None,
    ) -> ExternalIdRecord:
        """登记外部标识（不含 ticker）；重复登记幂等。"""
        self._require_entity(entity_id)
        itype = _facet(id_type, IdType, "id_type", required=True)
        value = str(id_value).strip()
        if not value:
            raise ValueError("id_value 不能为空")
        assert itype is not None
        for row in self._repo.external_id_rows(entity_id):
            if row.id_type == itype and row.id_value == value and row.valid_to is None:
                return row
        record = ExternalIdRecord(
            entity_id=entity_id,
            id_type=itype,
            id_value=value,
            valid_from=to_date(valid_from) or EPOCH,
            valid_to=to_date(valid_to),
            knowledge_time=now(),
        )
        self._repo.append_external_id(record)
        return record

    def external_ids(self, entity_id: int, *, as_of: Any = None) -> list[ExternalIdRecord]:
        target = to_date(as_of)
        return [
            row for row in self._repo.external_id_rows(entity_id) if covers(row, target)
        ]

    def find_by_external_id(
        self, id_type: str, id_value: str, *, as_of: Any = None
    ) -> int | None:
        """外部标识 → entity_id（as-of 含区间）；未匹配返回 None。"""
        itype = _facet(id_type, IdType, "id_type", required=True)
        assert itype is not None
        target = to_date(as_of)
        value = str(id_value).strip()
        matches = [
            row
            for row in self._repo.find_external_ids(itype, value)
            if covers(row, target)
        ]
        if not matches:
            return None
        latest = max(matches, key=lambda row: (row.valid_from or EPOCH, row.version))
        return latest.entity_id

    # ------------------------------------------------------------ Hub 构建
    def build_from_hub(
        self,
        hub: Any,
        *,
        namechange_start: str | None = None,
        namechange_end: str | None = None,
    ) -> BuildStats:
        """从 FinDataHub 基础信息构建/刷新身份（分类面 + 名称）。

        含退市标的身份（Tushare ``stock_basic`` 默认仅返回在市，退市标的自
        ``delist_list`` 取得）：交易状态（上市/停牌/退市）不在注册表，由
        ``cn_equity.listing_lifecycle`` 数据集承载，PIT Universe 见
        :func:`fin_data_platform.registry.universe.universe`。
        """
        stats = BuildStats()
        for kind, entity_type in _KIND_ENTITY_TYPE.items():
            try:
                frame = hub.get_reference(kind)
            except (UnsupportedCapability, ValueError):
                continue
            stats = self._register_frame(
                frame, kind=kind, entity_type=entity_type.value, stats=stats
            )
        try:
            delisted = hub.get_reference("delist_list")
        except (UnsupportedCapability, ValueError):
            delisted = None
        if delisted is not None and not delisted.empty:
            stats = self._register_frame(
                delisted,
                kind="stock_list",
                entity_type=EntityType.EQUITY.value,
                stats=stats,
            )
        if namechange_start and namechange_end:
            try:
                events = hub.get_market_events(
                    kind="namechange", start=namechange_start, end=namechange_end
                )
            except (UnsupportedCapability, ValueError):
                events = None
            if events is not None and not events.empty:
                for row in events.to_dict("records"):
                    code = clean_code(row.get("code"))
                    entity_id = self.resolve(code) if code else None
                    if entity_id is None:
                        stats = _bump(stats, skipped=1)
                        continue
                    self.add_name_change(
                        entity_id,
                        name=str(row["name"]),
                        start_date=row["start_date"],
                        end_date=row.get("end_date"),
                        ann_date=row.get("ann_date"),
                    )
                    stats = _bump(stats, attributes=1)
        return stats

    def _register_frame(
        self, frame: pd.DataFrame, *, kind: str, entity_type: str, stats: BuildStats
    ) -> BuildStats:
        if frame is None or frame.empty:
            return stats
        for row in frame.to_dict("records"):
            code = clean_code(row.get("code"))
            if code is None:
                continue
            _, created, updated = self._register_or_refresh(
                code=code,
                entity_type=_listing_type(kind, code, entity_type),
                name=str(row.get("name") or ""),
                market=_market_from_code(code),
            )
            stats = _bump(
                stats,
                registered=int(created),
                updated=int(updated),
                skipped=int(not created and not updated),
            )
        return stats
