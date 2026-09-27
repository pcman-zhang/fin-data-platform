"""质量结果 schema（``meta.quality_results``；doc-13 §1 冻结稿）。

由迁移修订 0007 创建；同时合并进 :func:`fin_data_platform.storage.schema.build_metadata`
（本地建库与数据库文档生成）。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
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

quality_results = Table(
    "quality_results",
    metadata,
    Column("run_id", Integer, primary_key=True),
    Column("dataset", String(64), primary_key=True),
    Column("check_id", String(64), primary_key=True),
    Column("family", String(16), nullable=False),
    Column("severity", String(8), nullable=False),
    Column("status", String(8), nullable=False),
    Column("window_start", Date),
    Column("window_end", Date),
    Column("rows_checked", BigInteger),
    Column("violations", BigInteger, nullable=False),
    Column("samples", Text),
    Column("metrics", Text),
    Column("message", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)
Index("ix_quality_results_window", quality_results.c.window_end, quality_results.c.dataset)

TABLES = (quality_results,)
