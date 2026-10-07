"""导出结果表 schema（``meta.export_requests``；doc-12 §2.3 / doc-13 §1）。

由迁移修订 0008 创建；同时合并进 :func:`fin_data_platform.storage.schema.build_metadata`。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

#: doc-13 §1：控制面 schema
SCHEMA = "meta"

metadata = MetaData()

export_requests = Table(
    "export_requests",
    metadata,
    Column("export_id", String(32), primary_key=True),
    Column("dataset", String(64), nullable=False),
    Column("params", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("format", String(8), nullable=False),
    Column("artifact_path", String(255)),
    Column("rows", BigInteger),
    Column("bytes", BigInteger),
    Column("error", Text),
    Column("run_id", Integer),
    Column("request_id", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    schema=SCHEMA,
)
Index("ix_export_requests_status", export_requests.c.status, export_requests.c.created_at)

TABLES = (export_requests,)
