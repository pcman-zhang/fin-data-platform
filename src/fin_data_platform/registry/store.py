"""实体注册表最小持久化：canonical 代码 → 稳定 ``entity_id``。

TASK-3.14 的完整持久化仓储尚未落地；同步写入端需要稳定的实体主键，本模块提供
最小能力：按 canonical 代码读取现有身份，缺失则分配并写入
``ref.entity`` / ``ref.entity_code_history``（PG advisory lock 防并发重复）。

SCD2 更新（改名 / 退市）与完整仓储能力归后续任务；本模块只保证「同一代码 →
同一 entity_id」；TASK-3.35 增加属性刷新（同 ``valid_from`` 的版本修订）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, func, insert, select, text

from fin_data_platform.registry._util import EPOCH
from fin_data_platform.registry.models import EntityRecord
from fin_data_platform.registry.schema import entity, entity_code_history

#: 「不修改」哨兵：``None`` 是合法目标值（如清空非法 entity_class）
_UNSET: Any = object()


def _utcnow() -> datetime:
    """naive UTC（与存储层时间列口径一致；避免 registry → runtime 的循环导入）。"""
    return datetime.now(UTC).replace(tzinfo=None)


#: entity_id 分配锁（全局串行：跨代码的 max+1 竞争会分配出重复 id）
_ALLOC_LOCK_KEY = "fdp_entity:alloc"


def to_entity_record(row: object) -> EntityRecord:
    mapping = row  # RowMapping
    return EntityRecord(  # type: ignore[arg-type]
        entity_id=int(mapping["entity_id"]),  # type: ignore[index]
        entity_type=str(mapping["entity_type"]),  # type: ignore[index]
        code=str(mapping["code"]),  # type: ignore[index]
        name=str(mapping["name"]),  # type: ignore[index]
        entity_class=mapping["entity_class"],  # type: ignore[index]
        market=mapping["market"],  # type: ignore[index]
        currency=mapping["currency"],  # type: ignore[index]
        exchange=mapping["exchange"],  # type: ignore[index]
        frequency=mapping["frequency"],  # type: ignore[index]
        unit=mapping["unit"],  # type: ignore[index]
        algorithm_id=mapping["algorithm_id"],  # type: ignore[index]
        social_status=mapping["social_status"],  # type: ignore[index]
        valid_from=mapping["valid_from"],  # type: ignore[index]
        valid_to=mapping["valid_to"],  # type: ignore[index]
        knowledge_time=mapping["knowledge_time"],  # type: ignore[index]
        version=int(mapping["version"]),  # type: ignore[index]
    )


class EntityStore:
    """实体身份的数据库读写（最小集）。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_entity(
        self,
        *,
        code: str,
        entity_type: str = "equity",
        name: str = "",
        entity_class: str | None = None,
        market: str | None = "cn",
        currency: str | None = None,
        exchange: str | None = None,
    ) -> EntityRecord:
        """返回代码对应实体；不存在则分配 ``entity_id`` 并写入身份与代码履历。"""
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                # 全局分配锁：不同代码并发时 max+1 也会读到同一个值（SCD2 主键含
                # knowledge_time，重复 id 不会报冲突），必须全局串行化分配
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": _ALLOC_LOCK_KEY},
                )
            existing = connection.execute(
                select(entity)
                .where(entity.c.code == code)
                .order_by(entity.c.version.desc(), entity.c.knowledge_time.desc())
                .limit(1)
            ).mappings().first()
            if existing is not None:
                return to_entity_record(existing)

            next_id = int(
                connection.execute(
                    select(func.coalesce(func.max(entity.c.entity_id), 10000) + 1)
                ).scalar_one()
            )
            now = _utcnow()
            social_status = "operating" if entity_type == "issuer" else None
            record = EntityRecord(
                entity_id=next_id,
                entity_type=entity_type,
                code=code,
                name=name,
                entity_class=entity_class,
                market=market,
                currency=currency,
                exchange=exchange,
                social_status=social_status,
                valid_from=EPOCH,
                knowledge_time=now,
                version=1,
            )
            connection.execute(
                insert(entity).values(
                    entity_id=record.entity_id,
                    entity_type=record.entity_type,
                    entity_class=record.entity_class,
                    market=record.market,
                    code=record.code,
                    name=record.name,
                    currency=record.currency,
                    exchange=record.exchange,
                    frequency=record.frequency,
                    unit=record.unit,
                    algorithm_id=record.algorithm_id,
                    social_status=record.social_status,
                    valid_from=record.valid_from,
                    valid_to=None,
                    knowledge_time=record.knowledge_time,
                    version=record.version,
                )
            )
            connection.execute(
                insert(entity_code_history).values(
                    entity_id=record.entity_id,
                    code=record.code,
                    valid_from=record.valid_from,
                    valid_to=None,
                    knowledge_time=record.knowledge_time,
                    version=1,
                )
            )
        return record

    def get_entity(self, code: str) -> EntityRecord | None:
        """按 canonical 代码读取最新身份（无则 ``None``）。"""
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(entity)
                    .where(entity.c.code == code)
                    .order_by(entity.c.version.desc(), entity.c.knowledge_time.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return to_entity_record(row) if row is not None else None

    def update_entity(
        self,
        *,
        code: str,
        name: Any = _UNSET,
        entity_class: Any = _UNSET,
        market: Any = _UNSET,
        currency: Any = _UNSET,
        exchange: Any = _UNSET,
    ) -> tuple[EntityRecord | None, bool]:
        """属性刷新（SCD2 修订）：变化时追加新版本；未变化不写。

        返回 ``(最新记录, changed)``；实体不存在返回 ``(None, False)``。
        语义说明：v1 仅做「同 ``valid_from`` 的版本修订」（属性更正；每次变化
        ``version+1``、``knowledge_time=now``）；带日期的名称/属性区间由
        namechange 等事件通道另行落地。
        """
        attributes = {
            "name": name,
            "entity_class": entity_class,
            "market": market,
            "currency": currency,
            "exchange": exchange,
        }
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    select(entity)
                    .where(entity.c.code == code)
                    .order_by(entity.c.version.desc(), entity.c.knowledge_time.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None, False
            record = to_entity_record(row)
            changes = {
                field: value
                for field, value in attributes.items()
                if value is not _UNSET and getattr(record, field) != value
            }
            if not changes:
                return record, False
            updated = replace(
                record, version=record.version + 1, knowledge_time=_utcnow(), **changes
            )
            connection.execute(
                insert(entity).values(
                    entity_id=updated.entity_id,
                    entity_type=updated.entity_type,
                    entity_class=updated.entity_class,
                    market=updated.market,
                    code=updated.code,
                    name=updated.name,
                    currency=updated.currency,
                    exchange=updated.exchange,
                    frequency=updated.frequency,
                    unit=updated.unit,
                    algorithm_id=updated.algorithm_id,
                    social_status=updated.social_status,
                    valid_from=updated.valid_from,
                    valid_to=updated.valid_to,
                    knowledge_time=updated.knowledge_time,
                    version=updated.version,
                )
            )
        return updated, True
