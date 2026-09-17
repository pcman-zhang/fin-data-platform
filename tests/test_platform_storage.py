"""存储层（TASK-3.3 第一期）测试：字典 schema 生成、幂等写入、as-of 读取。

SQLite 用于语义验证（PG/Timescale 为集成环境）；Timescale DDL 以字符串断言。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.storage import (
    append_rows,
    as_of_query,
    build_metadata,
    ensure_schema,
    latest_query,
    schema_sql,
    timescale_statements,
)


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        for schema in ("cn_equity", "cn_fund", "ref", "meta"):
            connection.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    return engine


def test_metadata_from_dictionary() -> None:
    metadata, specs = build_metadata()
    assert "cn_equity.daily_bar" in metadata.tables
    assert "ref.entity" in metadata.tables
    table = metadata.tables["cn_equity.daily_bar"]
    assert [column.name for column in table.primary_key] == [
        "entity_id",
        "trade_date",
        "knowledge_time",
        "version",
    ]
    assert str(table.c.amount.type) == "NUMERIC(24, 4)"
    assert str(table.c.provider.type) == "TEXT"
    assert any(
        index.name == "ix_daily_bar_business" for index in table.indexes
    )
    assert specs["cn_equity.daily_bar"].dataset == "cn_equity.daily_bar"
    # 财务数据集以 issuer_id 为业务键（doc-10 §3.3：财务/股东/公司事件挂 issuer）
    financials = metadata.tables["cn_equity.financials_balance_sheet"]
    assert [column.name for column in financials.primary_key] == [
        "issuer_id",
        "end_date",
        "report_type",
        "knowledge_time",
        "version",
    ]


def test_ref_indexes_preserved_after_merge() -> None:
    metadata, _ = build_metadata()
    entity_indexes = {index.name for index in metadata.tables["ref.entity"].indexes}
    code_indexes = {
        index.name for index in metadata.tables["ref.entity_code_history"].indexes
    }
    assert "ix_entity_code" in entity_indexes
    assert "ix_entity_code_history_code" in code_indexes


def test_schema_sql_for_postgres() -> None:
    metadata, _ = build_metadata()
    statements = schema_sql(metadata)
    joined = "\n".join(statements)
    assert "CREATE SCHEMA IF NOT EXISTS cn_equity" in joined
    assert "CREATE TABLE cn_equity.daily_bar" in joined
    assert "NUMERIC(24, 4)" in joined


def test_compression_keys_cover_physical_key() -> None:
    """TimescaleDB 唯一性要求：物理键列必须包含在 segmentby ∪ orderby（TASK-3.20）。"""
    _, specs = build_metadata()
    compressed = {
        dataset: spec
        for dataset, spec in specs.items()
        if spec.storage.compression is not None
    }
    assert compressed, "应存在带压缩配置的数据集"
    for dataset, spec in compressed.items():
        compression = spec.storage.compression
        assert compression is not None
        covered = {compression.segment_by}
        covered.update(
            column.strip() for column in compression.order_by.split(",")
        )
        missing = set(spec.physical_key) - covered
        assert not missing, f"{dataset} 压缩键未覆盖物理键列: {sorted(missing)}"


def test_timescale_statements_by_partition_strategy() -> None:
    metadata, specs = build_metadata()
    statements = timescale_statements(metadata, specs)
    joined = "\n".join(statements)
    assert "create_hypertable('cn_equity.daily_bar', 'trade_date'" in joined
    assert "migrate_data => TRUE" in joined
    assert "INTERVAL '1 month'" in joined
    assert "compress_segmentby = 'entity_id'" in joined
    assert "add_compression_policy('cn_equity.daily_bar', INTERVAL '7 days'" in joined
    # versioned 按 knowledge_time 分区
    assert "create_hypertable('cn_equity.financials_balance_sheet', 'knowledge_time'" in joined
    # scd2 无 hypertable
    assert "index_member" not in joined


def _daily_bar_row(**overrides):
    row = {
        "entity_id": 10001,
        "trade_date": date(2026, 1, 5),
        "knowledge_time": datetime(2026, 1, 5, 18, 0),
        "ingest_time": datetime(2026, 1, 5, 18, 1),
        "version": 1,
        "provider": "tushare",
        "close": 10.0,
    }
    row.update(overrides)
    return row


def test_ensure_schema_and_idempotent_append(engine) -> None:
    metadata, _ = build_metadata()
    executed = ensure_schema(engine, metadata=metadata)
    assert any("daily_bar" in statement for statement in executed)
    # SQLite 下未执行 CREATE SCHEMA，不应出现在"已执行"清单
    assert not any(statement.startswith("CREATE SCHEMA") for statement in executed)

    table = metadata.tables["cn_equity.daily_bar"]
    with engine.begin() as connection:
        first = append_rows(connection, table, [_daily_bar_row()])
        second = append_rows(connection, table, [_daily_bar_row()])
        count = connection.execute(select(func.count()).select_from(table)).scalar_one()
    assert first == 1
    assert second == 0  # 物理键冲突 → 跳过
    assert count == 1


def test_metadata_includes_runtime_meta_tables() -> None:
    metadata, _ = build_metadata()
    assert "meta.job_runs" in metadata.tables
    assert "meta.job_dependencies" in metadata.tables
    # 基线（0001）不含 meta：由修订 0002 创建
    baseline_metadata, _ = build_metadata(include_runtime=False)
    assert "meta.job_runs" not in baseline_metadata.tables


def test_database_document_generated() -> None:
    from fin_data_platform.storage.report import database_markdown

    document = database_markdown()
    for key in (
        "cn_equity.daily_bar",
        "cn_equity.financials_balance_sheet",
        "cn_equity.index_member",
        "cn_equity.listing_lifecycle",
        "cn_fund.nav",
        "ref.entity",
        "ref.entity_code_history",
        "ref.entity_relation",
        "ref.entity_external_id",
        "ref.relation_type_dict",
        "meta.job_defs",
        "meta.job_dependencies",
        "meta.job_runs",
        "meta.watermarks",
        "meta.algorithm_registry",
        "meta.algorithm_events",
        "meta.data_generation",
    ):
        assert f"`{key}`" in document
    assert "## 1. 表清单与作用" in document
    assert "## 2. 字段与类型" in document
    assert "## 3. 表依赖关系" in document
    assert "NUMERIC(24, 4)" in document
    assert "ref.entity（entity_id/issuer_id 逻辑引用）" in document
    assert (
        "`cn_equity.financials_balance_sheet` ← "
        "ref.entity（entity_id/issuer_id 逻辑引用）"
    ) in document


def test_as_of_and_latest_queries(engine) -> None:
    metadata, _ = build_metadata()
    ensure_schema(engine, metadata=metadata)
    table = metadata.tables["cn_equity.daily_bar"]
    rows = [
        _daily_bar_row(close=10.0),
        _daily_bar_row(
            knowledge_time=datetime(2026, 2, 1, 18, 0), version=2, close=12.0
        ),
    ]
    with engine.begin() as connection:
        append_rows(connection, table, rows)
        early = connection.execute(
            as_of_query(
                table,
                as_of=datetime(2026, 1, 15),
                key_columns=["entity_id", "trade_date"],
            )
        ).mappings().all()
        late = connection.execute(
            as_of_query(
                table,
                as_of=datetime(2026, 3, 1),
                key_columns=["entity_id", "trade_date"],
            )
        ).mappings().all()
        latest = connection.execute(
            latest_query(table, key_columns=["entity_id", "trade_date"])
        ).mappings().all()
    assert len(early) == 1 and float(early[0]["close"]) == 10.0
    assert len(late) == 1 and float(late[0]["close"]) == 12.0
    assert len(latest) == 1 and float(latest[0]["close"]) == 12.0


# ------------------------------------------------------------------ 引擎工厂
def test_engine_factory_accepts_sqlite_dsn() -> None:
    """SQLite 等后端不受 connect_timeout 影响（回归：非 PG 后端不可注入该参数）。"""
    from fin_data_platform.storage.config import StorageConfig
    from fin_data_platform.storage.engine import create_read_engine, create_write_engine

    write_engine = create_write_engine(
        StorageConfig(write_dsn="sqlite+pysqlite:///:memory:")
    )
    with write_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1
    write_engine.dispose()

    read_engine = create_read_engine(
        StorageConfig(write_dsn="sqlite+pysqlite:///:memory:")
    )
    with read_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1
    read_engine.dispose()


def test_storage_config_connect_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from fin_data_platform.storage.config import StorageConfig

    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "p")
    monkeypatch.setenv("DATABASE_HOST", "db")
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT", "1.5")
    assert StorageConfig.from_env().connect_timeout == 1.5

    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT", "abc")
    with pytest.raises(ValueError, match="CONNECT_TIMEOUT"):
        StorageConfig.from_env()

    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT", "0")
    with pytest.raises(ValueError, match="CONNECT_TIMEOUT"):
        StorageConfig.from_env()


# ------------------------------------------------------------------ 只读授权
def test_readonly_statements_cover_readable_schemas_only() -> None:
    from fin_data_platform.storage.grants import readonly_statements

    statements = readonly_statements(domains=["cn_equity", "cn_fund"], writer="fdp")
    joined = "\n".join(statements)
    assert 'CREATE ROLE "fdp_ro" NOLOGIN' in joined
    for schema in ("mart", "ref", "cn_equity", "cn_fund"):
        assert f'GRANT USAGE ON SCHEMA "{schema}" TO "fdp_ro";' in joined
        assert (
            f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "fdp_ro";' in joined
        )
        assert (
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "fdp" IN SCHEMA "{schema}" '
            'GRANT SELECT ON TABLES TO "fdp_ro";' in joined
        )
    assert 'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA "mart" TO "fdp_ro";' in joined
    # 内部 schema 一律不授权
    for internal in ("raw", "meta", "alembic_version"):
        assert f'"{internal}"' not in joined


def test_readable_schemas_exclude_internal() -> None:
    from fin_data_platform.storage.grants import readable_schemas

    assert readable_schemas(["cn_equity", "raw", "meta"]) == [
        "mart",
        "ref",
        "cn_equity",
    ]


def test_storage_config_read_dsn_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from fin_data_platform.storage.config import StorageConfig

    monkeypatch.setenv("DATABASE_USER", "writer")
    monkeypatch.setenv("DATABASE_PASSWORD", "wp")
    monkeypatch.setenv("DATABASE_HOST", "db")
    monkeypatch.delenv("DATABASE_READ_USER", raising=False)
    monkeypatch.delenv("DATABASE_READ_PASSWORD", raising=False)
    assert StorageConfig.from_env().read_dsn is None

    monkeypatch.setenv("DATABASE_READ_USER", "reader")
    monkeypatch.setenv("DATABASE_READ_PASSWORD", "rp")
    config = StorageConfig.from_env()
    assert config.read_dsn is not None
    assert "reader" in config.read_dsn and "reader" not in config.write_dsn
    assert config.reader_dsn == config.read_dsn

    # 部分提供 → 显式报错（含空串等同未配置的部署路径）
    monkeypatch.delenv("DATABASE_READ_PASSWORD")
    with pytest.raises(ValueError, match="必须同时提供"):
        StorageConfig.from_env()
    monkeypatch.setenv("DATABASE_READ_USER", "reader")
    monkeypatch.setenv("DATABASE_READ_PASSWORD", "")  # compose 注入空值
    with pytest.raises(ValueError, match="必须同时提供"):
        StorageConfig.from_env()
    # 两侧均为空串 → 未配置（回退写端）
    monkeypatch.setenv("DATABASE_READ_USER", "")
    monkeypatch.setenv("DATABASE_READ_PASSWORD", "")
    assert StorageConfig.from_env().read_dsn is None


def test_readonly_default_schemas_deduplicated() -> None:
    from fin_data_platform.storage.grants import readable_schemas

    schemas = readable_schemas()  # 缺省取字典域（其中含 ref）
    assert schemas.count("ref") == 1
    assert schemas.count("mart") == 1
    assert "raw" not in schemas and "meta" not in schemas


def test_readonly_role_name_validation() -> None:
    from fin_data_platform.storage.grants import readonly_statements

    with pytest.raises(ValueError, match="角色名非法"):
        readonly_statements(role="bad role")
    with pytest.raises(ValueError, match="角色名非法"):
        readonly_statements(role="a'b")
