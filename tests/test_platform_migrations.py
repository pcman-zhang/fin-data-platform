"""迁移（TASK-3.3.1 / 3.18 / 3.12）单元测试：基线 + 修订与 schema 一致（漂移校验）。"""

from __future__ import annotations

from fin_data_platform.derived.schema import TABLES as DERIVED_TABLES
from fin_data_platform.export.schema import TABLES as EXPORT_TABLES
from fin_data_platform.quality.schema import TABLES as QUALITY_TABLES
from fin_data_platform.runtime.schema import TABLES as META_TABLES
from fin_data_platform.storage.migrations import (
    ALGORITHM_META_PATH,
    BASELINE_PATH,
    DAILY_STATUS_DATASETS,
    DAILY_STATUS_PATH,
    DDL_HYGIENE_PATH,
    EXPORT_META_PATH,
    QUALITY_META_PATH,
    REFERENCE_DATA_DATASETS,
    REFERENCE_DATA_PATH,
    REVISION_DATASETS,
    REVISION_STATEMENTS,
    RUNTIME_META_PATH,
    alembic_config,
    algorithm_meta_statements,
    baseline_statements,
    daily_status_statements,
    ddl_hygiene_statements,
    expected_head_revision,
    export_meta_statements,
    frozen_datasets,
    quality_meta_statements,
    reference_data_statements,
    render_algorithm_meta_revision,
    render_baseline_script,
    render_daily_status_revision,
    render_ddl_hygiene_revision,
    render_export_meta_revision,
    render_quality_meta_revision,
    render_reference_data_revision,
    render_runtime_meta_revision,
    runtime_meta_statements,
)
from fin_data_platform.storage.schema import build_metadata


def test_baseline_script_matches_dictionary() -> None:
    """基线漂移校验：基线只含冻结数据集；新增字典条目须登记到新修订（否则漂移失败）。"""
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
    # 基线冻结：0005 承建的参考数据表不得出现在 0001
    assert "ref.market" not in joined
    assert "ref.trade_calendar" not in joined


def test_baseline_covers_frozen_metadata_indexes() -> None:
    """doc-13 §4/§9：冻结数据集/ref 手写表的业务索引必须随基线落地。"""
    metadata, _ = build_metadata(include_runtime=False, datasets=frozen_datasets())
    upgrade, _ = baseline_statements()
    joined = "\n".join(upgrade)
    indexes = [index for table in metadata.tables.values() for index in table.indexes if index.name]
    assert indexes, "metadata 应包含业务索引"
    for index in indexes:
        assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined


def test_dictionary_tables_covered_by_baseline_or_revisions() -> None:
    """字典全集的表/索引必须由基线或增量修订（按台账枚举）之一承建。"""
    metadata, _ = build_metadata(include_runtime=False)
    baseline, _ = baseline_statements()
    joined_statements = [*baseline]
    for revision in sorted(REVISION_STATEMENTS):
        upgrade, _ = REVISION_STATEMENTS[revision]()
        joined_statements.extend(upgrade)
    joined = "\n".join(joined_statements)
    for table in metadata.tables.values():
        assert f"CREATE TABLE IF NOT EXISTS {table.key}" in joined
        for index in table.indexes:
            if index.name:
                assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined


def test_revision_ledgers_are_consistent() -> None:
    """修订台账一致：数据集台账与语句生成器台账必须同键。"""
    assert set(REVISION_STATEMENTS) == set(REVISION_DATASETS)


def test_baseline_downgrade_drops_read_models_then_tables() -> None:
    metadata, _ = build_metadata(include_runtime=False, datasets=frozen_datasets())
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


def test_reference_data_revision_matches_generator() -> None:
    """修订 0005 漂移校验：承建数据集/字典存储变更后必须重新生成 0005。"""
    assert REFERENCE_DATA_PATH.read_text(encoding="utf-8") == render_reference_data_revision()


def test_reference_data_revision_covers_its_datasets() -> None:
    metadata, _ = build_metadata(
        include_runtime=False, datasets=frozenset(REFERENCE_DATA_DATASETS)
    )
    upgrade, downgrade = reference_data_statements()
    joined = "\n".join(upgrade)
    for table in metadata.tables.values():
        assert f"CREATE TABLE IF NOT EXISTS {table.key}" in joined
        for index in table.indexes:
            if index.name:
                assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined
    # ref.trade_calendar：hypertable + 压缩（orderby 覆盖物理键）
    assert "create_hypertable('ref.trade_calendar', 'trade_date'" in joined
    assert "compress_orderby = 'trade_date, knowledge_time, version'" in joined
    # ref.market：SCD2 非分区表，不建 hypertable
    assert "create_hypertable('ref.market'" not in joined
    dropped = {
        statement.removeprefix("DROP TABLE IF EXISTS ").removesuffix(";")
        for statement in downgrade
    }
    assert dropped == set(metadata.tables)


def test_daily_status_revision_matches_generator() -> None:
    """修订 0006 漂移校验：承建数据集/字典存储变更后必须重新生成 0006。"""
    assert DAILY_STATUS_PATH.read_text(encoding="utf-8") == render_daily_status_revision()


def test_daily_status_revision_covers_its_datasets() -> None:
    metadata, _ = build_metadata(
        include_runtime=False, datasets=frozenset(DAILY_STATUS_DATASETS)
    )
    upgrade, downgrade = daily_status_statements()
    joined = "\n".join(upgrade)
    for table in metadata.tables.values():
        assert f"CREATE TABLE IF NOT EXISTS {table.key}" in joined
        for index in table.indexes:
            if index.name:
                assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined
    # cn_equity.daily_status：hypertable + 压缩（orderby 覆盖物理键）
    assert "create_hypertable('cn_equity.daily_status', 'trade_date'" in joined
    assert "compress_orderby = 'trade_date, knowledge_time, version'" in joined
    dropped = {
        statement.removeprefix("DROP TABLE IF EXISTS ").removesuffix(";")
        for statement in downgrade
    }
    assert dropped == set(metadata.tables)


def test_frozen_datasets_exclude_revision_owned() -> None:
    frozen = frozen_datasets()
    assert set(REFERENCE_DATA_DATASETS).isdisjoint(frozen)
    assert set(DAILY_STATUS_DATASETS).isdisjoint(frozen)
    assert "cn_equity.daily_bar" in frozen
    assert "ref.entity" in frozen


def test_expected_head_is_latest_revision() -> None:
    """head = 台账中字典序最大的修订（新增修订文件未登记台账时即失败）。"""
    assert (
        expected_head_revision("postgresql+psycopg://u:p@localhost:5432/db")
        == max(REVISION_STATEMENTS)
    )


def test_alembic_config_escapes_dsn_interpolation() -> None:
    config = alembic_config("postgresql+psycopg://u:p%40w@localhost:5432/db")
    assert config.get_main_option("script_location").endswith("migrations")
    # configparser 插值后还原原始密码（% 转义）
    assert config.get_main_option("sqlalchemy.url").endswith("p%40w@localhost:5432/db")


def test_quality_meta_revision_matches_generator() -> None:
    """修订 0007 漂移校验：质量结果表 schema 变更后必须重新生成 0007。"""
    assert QUALITY_META_PATH.read_text(encoding="utf-8") == render_quality_meta_revision()


def test_quality_meta_revision_covers_all_tables() -> None:
    upgrade, downgrade = quality_meta_statements()
    joined = "\n".join(upgrade)
    dropped = {
        statement.removeprefix("DROP TABLE IF EXISTS ").removesuffix(";")
        for statement in downgrade
    }
    assert dropped == {table.key for table in QUALITY_TABLES}
    for table in QUALITY_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table.key}" in joined
        for index in table.indexes:
            assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined
    assert "PRIMARY KEY (run_id, dataset, check_id)" in joined


def test_export_meta_revision_matches_generator() -> None:
    """修订 0008 漂移校验：导出请求表 schema 变更后必须重新生成 0008。"""
    assert EXPORT_META_PATH.read_text(encoding="utf-8") == render_export_meta_revision()


def test_export_meta_revision_covers_all_tables() -> None:
    upgrade, downgrade = export_meta_statements()
    joined = "\n".join(upgrade)
    dropped = {
        statement.removeprefix("DROP TABLE IF EXISTS ").removesuffix(";")
        for statement in downgrade
    }
    assert dropped == {table.key for table in EXPORT_TABLES}
    for table in EXPORT_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table.key}" in joined
        for index in table.indexes:
            assert f"CREATE INDEX IF NOT EXISTS {index.name} ON" in joined
