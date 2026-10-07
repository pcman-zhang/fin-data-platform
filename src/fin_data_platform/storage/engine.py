"""存储引擎工厂与 schema 初始化。"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.schema import CreateIndex, CreateSchema, CreateTable

from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata, timescale_statements


def _connect_args(config: StorageConfig, dsn: str, kwargs: dict) -> dict:
    """注入连接超时（网络不可达时快速失败；可被调用方覆盖）。

    仅 PostgreSQL 后端支持 ``connect_timeout`` / ``statement_timeout``；
    其他后端（如 SQLite）跳过。
    """
    from sqlalchemy.engine import make_url

    existing = dict(kwargs.pop("connect_args", {}) or {})
    if make_url(dsn).get_backend_name() == "postgresql":
        existing.setdefault("connect_timeout", config.connect_timeout)
        if config.statement_timeout:
            # 读连接语句超时（资源隔离）：写端与导出走平台内部连接，不受限
            existing.setdefault(
                "options",
                f"-c statement_timeout={int(config.statement_timeout * 1000)}",
            )
    kwargs["connect_args"] = existing
    return kwargs


def create_write_engine(config: StorageConfig, **kwargs: object) -> Engine:
    # 写端不注入 statement_timeout（采集/派生/导出为长事务，按任务级控制）
    from dataclasses import replace

    return create_engine(
        config.write_dsn,
        **_connect_args(
            replace(config, statement_timeout=None), config.write_dsn, dict(kwargs)
        ),
    )


def create_read_engine(config: StorageConfig, **kwargs: object) -> Engine:
    return create_engine(
        config.reader_dsn, **_connect_args(config, config.reader_dsn, dict(kwargs))
    )


def ensure_schema(
    engine: Engine,
    *,
    config: StorageConfig | None = None,
    metadata=None,
    specs=None,
) -> list[str]:
    """创建 schema/表（幂等）；PostgreSQL + TimescaleDB 时追加 hypertable/压缩语句。

    返回已执行的语句列表（便于审查/迁移留档）。
    """
    if metadata is None:
        metadata, specs = build_metadata()
    elif specs is None:
        from fin_data_platform.dictionary import load_all

        specs = load_all()
    executed: list[str] = []
    schemas = sorted(
        {table.schema for table in metadata.tables.values() if table.schema}
    )
    is_sqlite = engine.dialect.name == "sqlite"
    with engine.begin() as connection:
        if not is_sqlite:
            for schema in schemas:
                connection.execute(CreateSchema(schema, if_not_exists=True))
                executed.append(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for table in metadata.sorted_tables:
            connection.execute(CreateTable(table, if_not_exists=True))
            executed.append(f"CREATE TABLE IF NOT EXISTS {table.key}")
            for index in sorted(table.indexes, key=lambda item: item.name or ""):
                connection.execute(CreateIndex(index, if_not_exists=True))
                executed.append(f"CREATE INDEX IF NOT EXISTS {index.name}")
        if (
            config is not None
            and config.timescale
            and not is_sqlite
            and engine.dialect.name == "postgresql"
            and specs
        ):
            for statement in timescale_statements(metadata, specs):
                connection.execute(text(statement))
                executed.append(statement)
    return executed
