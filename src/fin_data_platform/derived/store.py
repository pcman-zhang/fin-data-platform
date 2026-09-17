"""算法登记 / 升级台账 / 代次的持久化（meta.*；doc-20 §5）。

规则：登记表只 upsert、不删除（历史 id 永久保留）；事件按
``(algorithm_id, effective_from)`` 幂等；代次覆盖写（每读模型一行）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Protocol, cast

from sqlalchemy import Engine, func, select

from fin_data_platform.derived.schema import algorithm_events as _events
from fin_data_platform.derived.schema import algorithm_registry as _registry
from fin_data_platform.derived.schema import data_generation as _generation
from fin_data_platform.runtime._util import utcnow


@dataclass(frozen=True, slots=True)
class AlgorithmRow:
    """算法登记行（``output / inputs / description`` 缺省表示历史 id）。"""

    algorithm_id: str
    version: int
    owner: str
    implementation: str
    dataset: str | None
    output: str | None
    inputs: tuple[str, ...]
    description: str
    status: str
    effective_from: date | None


@dataclass(frozen=True, slots=True)
class AlgorithmEvent:
    """升级 / 重述事件（doc-10 §3.5：算法升级台账，WebUI 可见）。"""

    algorithm_id: str
    effective_from: date
    reason: str


class AlgorithmStore(Protocol):
    def upsert(self, rows: Sequence[AlgorithmRow]) -> int: ...

    def list_all(self) -> list[AlgorithmRow]: ...

    def record_events(self, events: Sequence[AlgorithmEvent]) -> int: ...

    def list_events(self) -> list[AlgorithmEvent]: ...

    def set_generation(self, read_model: str, generation: str) -> None: ...

    def get_generation(self, read_model: str) -> str | None: ...


def _row_values(row: AlgorithmRow) -> dict[str, object]:
    return {
        "algorithm_id": row.algorithm_id,
        "version": row.version,
        "owner": row.owner,
        "implementation": row.implementation,
        "dataset": row.dataset,
        "output": row.output,
        # 无字典引用（退役）时三项为 NULL：update 侧用 COALESCE 保留旧值（审计口径）
        "inputs": json.dumps(list(row.inputs), ensure_ascii=False) if row.inputs else None,
        "description": row.description,
        "status": row.status,
        "effective_from": row.effective_from,
        "updated_at": utcnow(),
    }


def _row_from_mapping(values: Mapping[str, Any]) -> AlgorithmRow:
    raw_inputs = values.get("inputs")
    return AlgorithmRow(
        algorithm_id=str(values["algorithm_id"]),
        version=int(values["version"]),
        owner=str(values["owner"]),
        implementation=str(values["implementation"]),
        dataset=values["dataset"] if values["dataset"] is None else str(values["dataset"]),
        output=values["output"] if values["output"] is None else str(values["output"]),
        inputs=tuple(json.loads(str(raw_inputs))) if raw_inputs else (),
        description=str(values["description"]),
        status=str(values["status"]),
        effective_from=cast("date | None", values["effective_from"]),
    )


class InMemoryAlgorithmStore:
    """测试 / 内存实现。"""

    def __init__(self) -> None:
        self._rows: dict[str, AlgorithmRow] = {}
        self._events: dict[tuple[str, date], AlgorithmEvent] = {}
        self._generations: dict[str, str] = {}

    def upsert(self, rows: Sequence[AlgorithmRow]) -> int:
        for row in rows:
            previous = self._rows.get(row.algorithm_id)
            if previous is not None:
                # 与 SQL 实现同构：退役（None/空）不覆盖已登记的归属信息
                row = replace(
                    row,
                    dataset=row.dataset if row.dataset is not None else previous.dataset,
                    output=row.output if row.output is not None else previous.output,
                    inputs=row.inputs or previous.inputs,
                )
            self._rows[row.algorithm_id] = row
        return len(rows)

    def list_all(self) -> list[AlgorithmRow]:
        return [self._rows[key] for key in sorted(self._rows)]

    def record_events(self, events: Sequence[AlgorithmEvent]) -> int:
        for event in events:
            self._events[(event.algorithm_id, event.effective_from)] = event
        return len(events)

    def list_events(self) -> list[AlgorithmEvent]:
        return [
            self._events[key] for key in sorted(self._events, key=lambda item: (item[1], item[0]))
        ]

    def set_generation(self, read_model: str, generation: str) -> None:
        self._generations[read_model] = generation

    def get_generation(self, read_model: str) -> str | None:
        return self._generations.get(read_model)


class SqlAlgorithmStore:
    """SQLAlchemy 实现（PostgreSQL / SQLite 同构 upsert）。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _insert(self, table):  # type: ignore[no-untyped-def]
        if self._engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert

        return dialect_insert(table)

    def upsert(self, rows: Sequence[AlgorithmRow]) -> int:
        if not rows:
            return 0
        with self._engine.begin() as connection:
            for row in rows:
                statement = self._insert(_registry).values(**_row_values(row))
                preserved = ("dataset", "output", "inputs")
                update = {
                    key: statement.excluded[key]
                    for key in _row_values(row)
                    if key not in ("algorithm_id", *preserved)
                }
                # 退役行不带归属信息：保留既有值（历史 id 审计口径）
                update.update(
                    {
                        key: func.coalesce(statement.excluded[key], _registry.c[key])
                        for key in preserved
                    }
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[_registry.c.algorithm_id], set_=update
                    )
                )
        return len(rows)

    def list_all(self) -> list[AlgorithmRow]:
        with self._engine.connect() as connection:
            result = connection.execute(select(_registry).order_by(_registry.c.algorithm_id))
            return [_row_from_mapping(dict(row._mapping)) for row in result]

    def record_events(self, events: Sequence[AlgorithmEvent]) -> int:
        if not events:
            return 0
        written = 0
        with self._engine.begin() as connection:
            for event in events:
                statement = self._insert(_events).values(
                    algorithm_id=event.algorithm_id,
                    effective_from=event.effective_from,
                    reason=event.reason,
                    created_at=utcnow(),
                )
                result = connection.execute(
                    statement.on_conflict_do_nothing(
                        index_elements=[
                            _events.c.algorithm_id,
                            _events.c.effective_from,
                        ]
                    )
                )
                written += result.rowcount or 0
        return written

    def list_events(self) -> list[AlgorithmEvent]:
        with self._engine.connect() as connection:
            result = connection.execute(
                select(_events).order_by(_events.c.effective_from, _events.c.algorithm_id)
            )
            return [
                AlgorithmEvent(
                    algorithm_id=str(row._mapping["algorithm_id"]),
                    effective_from=row._mapping["effective_from"],
                    reason=str(row._mapping["reason"]),
                )
                for row in result
            ]

    def set_generation(self, read_model: str, generation: str) -> None:
        with self._engine.begin() as connection:
            statement = self._insert(_generation).values(
                read_model=read_model, generation=generation, updated_at=utcnow()
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[_generation.c.read_model],
                    set_={
                        "generation": statement.excluded.generation,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )

    def get_generation(self, read_model: str) -> str | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(_generation.c.generation).where(_generation.c.read_model == read_model)
            ).first()
        return str(row[0]) if row is not None else None
