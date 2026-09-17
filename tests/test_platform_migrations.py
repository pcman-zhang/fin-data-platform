"""迁移（TASK-3.3.1 / 3.18 / 3.12）单元测试：基线 + 修订与 schema 一致（漂移校验）。"""

from __future__ import annotations

from fin_data_platform.derived.schema import TABLES as DERIVED_TABLES
from fin_data_platform.runtime.schema import TABLES as META_TABLES
from fin_data_platform.storage.migrations import (
    ALGORITHM_META_PATH,
    BASELINE_PATH,
    DDL_HYGIENE_PATH,
    RUNTIME_META_PATH,
    alembic_config,
    algorithm_meta_statements,
    baseline_statements,
    ddl_hygiene_statements,
    expected_head_revision,
    render_algorithm_meta_revision,
    render_baseline_script,
    render_ddl_hygiene_revision,
    render_runtime_meta_revision,
    runtime_meta_statements,
)
from fin_data_platform.storage.schema import build_metadata


def test_baseline_script_matches_dictionary() -> None:
    """字典 → 迁移漂移校验：字典变更后必须重新生成基线（write_baseline）。"""
    assert BASELINE_PATH.read_text(encoding="utf-8") == render_baseline_script()


def test_baseline_upgrade_covers_schemas_tables_hypertables_and_read_models() -> None:
    upgrade, _ = baseline_statements()
    joined = "\n".join(upgrade)
    assert "CREATE SCHEMA IF NOT EXISTS cn_equity" in joined
    assert "CREATE SCHEMA IF NOT EXISTS mart" in joined
    assert "CREATE TABLE IF NOT EXISTS cn_equity.daily_bar" in joined
    assert "create_hypertable('cn_equity.financials_balance_sheet', 'knowledge_time'" in joined
    assert "add_compression_policy('cn_equity.daily_bar'" in joined
    # doc-13 §4：默认不建物理外键；读模型（mart.entity_*）纳入基线
    assert "REFERENCES" not in joined
    assert "CREATE INDEX IF NOT EXISTS ix_entity_code ON ref.entity" in joined
    assert "CREATE INDEX IF NOT EXISTS ix_daily_bar_business" in joined
    assert "CREATE OR REPLACE VIEW mart.entity_latest_v1" in joined
    assert "CREATE OR REPLACE FUNCTION mart.entity_asof(as_of timestamptz)" in joined
    # 基线冻结：meta 控制面表不在 0001（由修订 0002 创建）
    assert "meta.job_runs" not in joined


def test_baseline_covers_every_metadata_index() -> None:
    """doc-13 §4/§9：字典/ref 手写表的业务索引必须随基线落地。"""
    metadata, _ = build_metadata(include_runtime=False)
    upgrade, _ = baseline_statements()
    joined = "\n".join(upgrade)
    indexes = [index for table in metadata.tables.values() for index in table.indexes if index.name]
    assert indexes, "metadata 应包含业务索引"
    for index in indexes:
        assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined


def test_baseline_downgrade_drops_read_models_then_tables() -> None:
    metadata, _ = build_metadata(include_runtime=False)
    _, downgrade = baseline_statements()
    # 先删依赖 ref.entity 的读模型，再删基表（否则 DROP TABLE 被依赖阻塞）
    assert downgrade[:2] == [
        "DROP VIEW IF EXISTS mart.entity_latest_v1;",
        "DROP FUNCTION IF EXISTS mart.entity_asof(timestamptz);",
    ]
    table_drops = [statement for statement in downgrade if statement.startswith("DROP TABLE")]
    assert len(table_drops) == len(metadata.tables)
    dropped = {
        statement.removeprefix("DROP TABLE IF EXISTS ").removesuffix(";")
        for statement in table_drops
    }
    assert dropped == set(metadata.tables)


def test_runtime_meta_revision_matches_schema() -> None:
    """修订 0002 漂移校验：meta schema 变更后必须重新生成 0002。"""
    assert RUNTIME_META_PATH.read_text(encoding="utf-8") == render_runtime_meta_revision()


def test_runtime_meta_revision_covers_all_tables() -> None:
    upgrade, downgrade = runtime_meta_statements()
    joined = "\n".join(upgrade)
    assert "CREATE SCHEMA IF NOT EXISTS meta" in joined
    dropped = {
        statement.removeprefix("DROP TABLE IF EXISTS ").removesuffix(";") for statement in downgrade
    }
    assert dropped == {table.key for table in META_TABLES}
    for table in META_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table.key}" in joined
        for index in table.indexes:
            assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined


def test_ddl_hygiene_revision_matches_generator() -> None:
    """修订 0003 漂移校验：字典/类型映射变更后必须重新生成 0003。"""
    assert DDL_HYGIENE_PATH.read_text(encoding="utf-8") == render_ddl_hygiene_revision()


def test_ddl_hygiene_covers_types_and_compression_keys() -> None:
    upgrade, _ = ddl_hygiene_statements()
    joined = "\n".join(upgrade)
    # 改压缩键前先解压；存量 varchar → text
    assert "decompress_chunk" in joined
    assert "character varying" in joined
    assert "ALTER COLUMN %I TYPE text" in joined
    # 压缩键覆盖物理键（knowledge_time/version）
    assert "compress_orderby = 'trade_date, knowledge_time, version'" in joined
    assert "compress_orderby = 'end_date, report_type, knowledge_time, version'" in joined
    assert "compress_orderby = 'trade_date, con_entity_id, knowledge_time, version'" in joined
    assert "compress_orderby = 'date, knowledge_time, version'" in joined


def test_algorithm_meta_revision_matches_generator() -> None:
    """修订 0004 漂移校验：派生引擎 meta schema 变更后必须重新生成 0004。"""
    assert ALGORITHM_META_PATH.read_text(encoding="utf-8") == render_algorithm_meta_revision()


def test_algorithm_meta_revision_covers_all_tables() -> None:
    upgrade, downgrade = algorithm_meta_statements()
    joined = "\n".join(upgrade)
    dropped = {
        statement.removeprefix("DROP TABLE IF EXISTS ").removesuffix(";") for statement in downgrade
    }
    assert dropped == {table.key for table in DERIVED_TABLES}
    for table in DERIVED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table.key}" in joined
        for index in table.indexes:
            assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined
    # 升级台账唯一约束（幂等事件）与登记表主键
    assert "uq_algorithm_events_id_from" in joined
    assert "PRIMARY KEY (algorithm_id)" in joined


def test_expected_head_is_algorithm_meta() -> None:
    assert (
        expected_head_revision("postgresql+psycopg://u:p@localhost:5432/db")
        == "0004_algorithm_meta"
    )


def test_alembic_config_escapes_dsn_interpolation() -> None:
    config = alembic_config("postgresql+psycopg://u:p%40w@localhost:5432/db")
    assert config.get_main_option("script_location").endswith("migrations")
    # configparser 插值后还原原始密码（% 转义）
    assert config.get_main_option("sqlalchemy.url").endswith("p%40w@localhost:5432/db")
