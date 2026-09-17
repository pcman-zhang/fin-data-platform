"""派生引擎控制面 schema（doc-10 §3.5 / doc-13 §1）。

三张 ``meta.*`` 表由修订 0004 创建：

- ``algorithm_registry``：算法登记（代码 ``@register`` + 字典 derived 生成，CI 一致）；
- ``algorithm_events``：算法升级 / 重述台账（``algorithm_id / effective_from / reason``）；
- ``data_generation``：读模型 / 派生投影的构建代次（doc-12：``YYYYMMDDTHHMMSSZ``，
  供 ``X-Data-Generation`` 与缓存键使用）。
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

#: doc-13 §1：控制面 schema
SCHEMA = "meta"

metadata = MetaData()

algorithm_registry = Table(
    "algorithm_registry",
    metadata,
    Column("algorithm_id", String(64), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("owner", String(64), nullable=False),
    Column("implementation", String(255), nullable=False),
    Column("dataset", String(64)),
    Column("output", String(64)),
    #: 输入清单（JSON 数组：``dataset.field``；历史 id 可能缺省）
    Column("inputs", Text),
    Column("description", Text, nullable=False),
    #: active（被字典引用）| deprecated（历史保留，不删除）
    Column("status", String(16), nullable=False),
    Column("effective_from", Date),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)

algorithm_events = Table(
    "algorithm_events",
    metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("algorithm_id", String(64), nullable=False),
    Column("effective_from", Date, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("algorithm_id", "effective_from", name="uq_algorithm_events_id_from"),
    schema=SCHEMA,
)
Index("ix_algorithm_events_algorithm", algorithm_events.c.algorithm_id)

data_generation = Table(
    "data_generation",
    metadata,
    Column("read_model", String(128), primary_key=True),
    Column("generation", String(32), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)

TABLES = (algorithm_registry, algorithm_events, data_generation)
