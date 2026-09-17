"""字典 → SQLAlchemy Core schema（Schema First，doc-11 §7 / doc-13 §9）。

- 类型映射：int64→BigInteger、float64→Double、decimal→Numeric(p,s)、
  string/enum→Text（枚举取值由字典 CI 校验，不用长度约束）、bool→Boolean、
  date→Date、timestamp(_tz)→DateTime；
- 主键 = ``physical_key``；业务查询索引 = ``business_key``；
- 合并 实体注册表 参照表（``ref`` schema）；
- TimescaleDB 专属语句（hypertable/压缩）由 :func:`timescale_statements` 生成。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    Index,
    MetaData,
    Numeric,
    Table,
    Text,
)
from sqlalchemy.sql.type_api import TypeEngine

from fin_data_platform.dictionary import DEFAULT_ROOT, load_all
from fin_data_platform.dictionary.models import DatasetSpec, FieldSpec
from fin_data_platform.registry.schema import metadata as ref_metadata


def column_type(field: FieldSpec) -> TypeEngine:
    if field.type == "int64":
        return BigInteger()
    if field.type == "float64":
        return Double()
    if field.type == "decimal":
        return Numeric(field.precision, field.scale)
    if field.type == "bool":
        return Boolean()
    if field.type == "date":
        return Date()
    if field.type == "timestamp":
        return DateTime()
    if field.type == "timestamp_tz":
        return DateTime(timezone=True)
    if field.type == "enum":
        return Text()
    return Text()


def _table_name(spec: DatasetSpec) -> str:
    return spec.storage.canonical_table.split(".", 1)[1]


def build_metadata(
    root: Path | None = None, *, include_runtime: bool = True
) -> tuple[MetaData, dict[str, DatasetSpec]]:
    """按字典构建全量 schema（含 ref 参照表；``include_runtime`` 控制 meta 控制面表）。

    基线迁移（修订 0001）不含 meta（由修订 0002 创建）；本地建库与文档使用全量。
    """
    specs = load_all(root or DEFAULT_ROOT)
    metadata = MetaData()
    for dataset, spec in sorted(specs.items()):
        physical = set(spec.physical_key)
        columns = [
            Column(
                field.name,
                column_type(field),
                nullable=field.nullable,
                primary_key=field.name in physical,
            )
            for field in spec.fields
        ]
        table = Table(
            _table_name(spec),
            metadata,
            *columns,
            schema=str(spec.domain),
            comment=f"{dataset} v{spec.semantic_version}",
        )
        # 业务查询索引（主键 = physical_key，见列定义）
        Index(
            f"ix_{_table_name(spec)}_business",
            *(table.c[name] for name in spec.business_key),
        )
    for table in ref_metadata.tables.values():
        if table.key not in metadata.tables:  # 字典条目优先（Schema First）
            table.to_metadata(metadata)
            continue
        # 字典已定义同表：保留手写表上的唯一约束/查询索引，避免定义漂移
        target = metadata.tables[table.key]
        for index in table.indexes:
            if index.name in target.indexes:
                continue
            Index(
                index.name,
                *(target.c[column.name] for column in index.columns),
                unique=index.unique,
            )
    if include_runtime:
        # 延迟导入：runtime 包依赖 storage（health 等），避免模块级循环
        from fin_data_platform.runtime.schema import metadata as runtime_metadata

        for table in runtime_metadata.tables.values():
            if table.key not in metadata.tables:
                table.to_metadata(metadata)
        # 派生引擎控制面（meta.algorithm_registry / algorithm_events / data_generation）
        from fin_data_platform.derived.schema import metadata as derived_metadata

        for table in derived_metadata.tables.values():
            if table.key not in metadata.tables:
                table.to_metadata(metadata)
    return metadata, specs


def timescale_statements(
    metadata: MetaData, specs: dict[str, DatasetSpec]
) -> list[str]:
    """生成 TimescaleDB 专属 DDL（hypertable/压缩策略）。"""
    statements: list[str] = []
    for _dataset, spec in sorted(specs.items()):
        storage = spec.storage
        if storage.partition_strategy == "none":
            continue
        if storage.partition_strategy == "knowledge_time":
            partition_column: str | None = "knowledge_time"
        else:
            partition_column = next(
                (
                    field.name
                    for field in spec.fields
                    if field.pit_role == "event_time"
                ),
                None,
            )
            if partition_column is None:
                raise ValueError(
                    f"{spec.dataset}: partition_strategy=event_time "
                    "但字典缺少 event_time 字段"
                )
        qualified = f"{spec.domain}.{_table_name(spec)}"
        statements.append(
            "SELECT create_hypertable("
            f"'{qualified}', '{partition_column}', "
            f"chunk_time_interval => INTERVAL '{storage.partition_interval}', "
            "migrate_data => TRUE, if_not_exists => TRUE);"
        )
        compression = storage.compression
        if compression is not None:
            statements.append(
                f"ALTER TABLE {qualified} SET ("
                "timescaledb.compress, "
                f"timescaledb.compress_segmentby = '{compression.segment_by}', "
                f"timescaledb.compress_orderby = '{compression.order_by}');"
            )
            statements.append(
                "SELECT add_compression_policy("
                f"'{qualified}', INTERVAL '{compression.after}', "
                "if_not_exists => TRUE);"
            )
    return statements


def schema_sql(
    metadata: MetaData, *, dialect: str = "postgresql", if_not_exists: bool = False
) -> list[str]:
    """生成建 schema/表/索引的 DDL（迁移与审查用）。"""
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateIndex, CreateSchema, CreateTable

    dialect_obj = postgresql.dialect() if dialect == "postgresql" else sqlite.dialect()
    schemas = sorted({table.schema for table in metadata.tables.values() if table.schema})
    statements = [
        str(CreateSchema(schema, if_not_exists=True).compile(dialect=dialect_obj))
        for schema in schemas
    ]
    for table in metadata.sorted_tables:
        statements.append(
            str(
                CreateTable(table, if_not_exists=if_not_exists).compile(
                    dialect=dialect_obj
                )
            )
        )
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            statements.append(
                str(
                    CreateIndex(index, if_not_exists=if_not_exists).compile(
                        dialect=dialect_obj
                    )
                )
            )
    return statements
