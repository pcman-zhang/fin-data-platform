"""Alembic 迁移的程序化入口（TASK-3.3.1，doc-13 §7）。

- 基线迁移由数据字典生成（Schema First）：:func:`baseline_statements` 产出冻结 DDL，
  :func:`render_baseline_script` 渲染修订文件；漂移校验见
  ``tests/test_platform_migrations.py``；
- :func:`upgrade` / :func:`downgrade` 供部署与集成测试调用（TASK-3.4 自动迁移）；
- DSN 由调用方或 ``DATABASE_*`` 环境变量提供（``FDP_DATABASE_HOST`` 可覆盖主机），
  凭证不落仓库。
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.read_models import (
    MART_SCHEMA,
    entity_read_model_drop_statements,
    entity_read_model_statements,
)
from fin_data_platform.storage.schema import (
    build_metadata,
    schema_sql,
    timescale_statements,
)

#: 仓库根（src/fin_data_platform/storage/migrations.py → 上溯 3 层）
REPO_ROOT = Path(__file__).resolve().parents[3]

BASELINE_REVISION = "0001_baseline"
BASELINE_PATH = REPO_ROOT / "migrations" / "versions" / f"{BASELINE_REVISION}.py"

#: 修订 0002：Runtime 控制面（meta.*）；基线冻结后增量一律走新修订
RUNTIME_META_REVISION = "0002_runtime_meta"
RUNTIME_META_PATH = REPO_ROOT / "migrations" / "versions" / f"{RUNTIME_META_REVISION}.py"


def baseline_statements() -> tuple[list[str], list[str]]:
    """返回基线 ``(upgrade, downgrade)`` DDL 清单（字典 + ref；不含 meta）。"""
    metadata, specs = build_metadata(include_runtime=False)
    upgrade = [
        f"CREATE SCHEMA IF NOT EXISTS {MART_SCHEMA}",
        *schema_sql(metadata, dialect="postgresql", if_not_exists=True),
        *timescale_statements(metadata, specs),
        *entity_read_model_statements(),
    ]
    downgrade = [
        *entity_read_model_drop_statements(),
        *(
            f"DROP TABLE IF EXISTS {table.key};"
            for table in reversed(metadata.sorted_tables)
        ),
    ]
    return upgrade, downgrade


def _statement_literals(statements: list[str]) -> list[str]:
    rendered: list[str] = []
    for statement in statements:
        text = statement.strip()
        if '"""' in text or text.endswith(('"', "\\")):
            rendered.append(f"    {text!r},")
        else:
            # 三引号字面量会解释反斜杠转义，需先转义（如 SQL 中的 E'\n'、LIKE 模式）
            escaped = text.replace("\\", "\\\\")
            rendered.append(f'    """{escaped}""",')
    return rendered


def render_baseline_script() -> str:
    """渲染基线修订文件源码（冻结 DDL；字典变更后重新生成基线或新增修订）。"""
    upgrade, downgrade = baseline_statements()
    lines = [
        '"""基线迁移：由数据字典生成（Schema First；请勿手改）。',
        "",
        "生成来源：``fin_data_platform.storage.schema``（dictionary → metadata）；",
        "重新生成：``write_baseline()``；漂移校验：``tests/test_platform_migrations.py``。",
        "",
        "Revision ID: 0001_baseline",
        "Revises:",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from alembic import op",
        "",
        'revision = "0001_baseline"',
        "down_revision = None",
        "branch_labels = None",
        "depends_on = None",
        "",
        "",
        "UPGRADE_STATEMENTS = [",
        *_statement_literals(upgrade),
        "]",
        "",
        "",
        "DOWNGRADE_STATEMENTS = [",
        *_statement_literals(downgrade),
        "]",
        "",
        "",
        "def upgrade() -> None:",
        "    for statement in UPGRADE_STATEMENTS:",
        "        op.execute(statement)",
        "",
        "",
        "def downgrade() -> None:",
        "    for statement in DOWNGRADE_STATEMENTS:",
        "        op.execute(statement)",
        "",
    ]
    return "\n".join(lines)


def write_baseline(path: Path | None = None) -> Path:
    """写入/刷新基线修订文件。

    仅适用于基线尚未在任一环境执行的阶段（v1 发布前）：基线一旦执行过
    （``alembic_version=0001_baseline``），字典变更必须新增修订（``alembic revision``）
    而非覆盖本文件，否则已迁移环境与新环境会静默分叉；届时漂移校验同步改为
    「基线 + 全部修订」。
    """
    target = path or BASELINE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_baseline_script(), encoding="utf-8")
    return target


# ---------------------------------------------------------------- 修订 0002（meta.*）
def runtime_meta_statements() -> tuple[list[str], list[str]]:
    """返回 Runtime 控制面（``meta.*``）的 ``(upgrade, downgrade)`` DDL 清单。"""
    from fin_data_platform.runtime.schema import metadata as runtime_metadata

    upgrade = schema_sql(runtime_metadata, dialect="postgresql", if_not_exists=True)
    downgrade = [
        f"DROP TABLE IF EXISTS {table.key};"
        for table in reversed(runtime_metadata.sorted_tables)
    ]
    return upgrade, downgrade


def render_runtime_meta_revision() -> str:
    """渲染修订 0002 源码（由 ``runtime/schema.py`` 生成，请勿手改）。"""
    upgrade, downgrade = runtime_meta_statements()
    lines = [
        '"""Runtime 控制面 schema（meta.*；doc-20）。由 runtime/schema.py 生成，请勿手改。',
        "",
        "重新生成：``write_runtime_meta_revision()``；漂移校验：``tests/test_platform_migrations.py``。",
        "",
        "Revision ID: 0002_runtime_meta",
        "Revises: 0001_baseline",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from alembic import op",
        "",
        'revision = "0002_runtime_meta"',
        'down_revision = "0001_baseline"',
        "branch_labels = None",
        "depends_on = None",
        "",
        "",
        "UPGRADE_STATEMENTS = [",
        *_statement_literals(upgrade),
        "]",
        "",
        "",
        "DOWNGRADE_STATEMENTS = [",
        *_statement_literals(downgrade),
        "]",
        "",
        "",
        "def upgrade() -> None:",
        "    for statement in UPGRADE_STATEMENTS:",
        "        op.execute(statement)",
        "",
        "",
        "def downgrade() -> None:",
        "    for statement in DOWNGRADE_STATEMENTS:",
        "        op.execute(statement)",
        "",
    ]
    return "\n".join(lines)


def write_runtime_meta_revision(path: Path | None = None) -> Path:
    """写入/刷新修订 0002（开发者操作；CI 校验生成结果与文件一致）。"""
    target = path or RUNTIME_META_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_runtime_meta_revision(), encoding="utf-8")
    return target


# ---------------------------------------------------------------- 修订 0003（DDL 卫生）
DDL_HYGIENE_REVISION = "0003_ddl_hygiene"
DDL_HYGIENE_PATH = REPO_ROOT / "migrations" / "versions" / f"{DDL_HYGIENE_REVISION}.py"

#: 修订前（0001/0002 时代）的压缩 orderby：仅用于回滚（降级恢复旧压缩键）
_LEGACY_COMPRESSION_ORDER: dict[str, str] = {
    "cn_equity.daily_bar": "trade_date",
    "cn_equity.adj_factor": "trade_date",
    "cn_equity.index_weight": "trade_date",
    "cn_equity.financials.balance_sheet": "end_date",
    "cn_fund.nav": "date",
}


def _dictionary() -> dict:
    from fin_data_platform.dictionary import load_all

    return load_all()


def _domain_schemas() -> list[str]:
    """字典数据域 schema（含 ``ref``：字典条目优先，列类型同样由字典决定）。"""
    return sorted({spec.domain for spec in _dictionary().values()})


def _in_list(values: list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _decompress_statements() -> list[str]:
    """解压全部已压缩 chunk（改压缩键前必须解压，否则 TimescaleDB 拒绝 ALTER）。"""
    schemas = _in_list(_domain_schemas())
    return [
        f"""DO $$
DECLARE rec record;
BEGIN
    FOR rec IN
        SELECT chunk_schema, chunk_name
        FROM timescaledb_information.chunks
        WHERE hypertable_schema IN ({schemas}) AND is_compressed
    LOOP
        CALL decompress_chunk(format('%I.%I', rec.chunk_schema, rec.chunk_name)::regclass, true);
    END LOOP;
END $$;"""
    ]


def _text_type_statements() -> list[str]:
    """canonical 表的 varchar → text（枚举长度约束由字典 CI 承担）。"""
    schemas = _in_list(_domain_schemas())
    return [
        f"""DO $$
DECLARE rec record;
BEGIN
    FOR rec IN
        SELECT c.table_schema, c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE t.table_type = 'BASE TABLE'
          AND c.data_type = 'character varying'
          AND c.table_schema IN ({schemas})
    LOOP
        EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I TYPE text',
                       rec.table_schema, rec.table_name, rec.column_name);
    END LOOP;
END $$;"""
    ]


def compression_statements(order_by_override: dict[str, str] | None = None) -> list[str]:
    """按字典生成压缩设置语句（``order_by`` 可覆盖，用于回滚旧键）。"""
    override = order_by_override or {}
    statements: list[str] = []
    for dataset, spec in sorted(_dictionary().items()):
        compression = spec.storage.compression
        if compression is None:
            continue
        order_by = override.get(dataset, compression.order_by)
        statements.append(
            f"ALTER TABLE {spec.storage.canonical_table} SET ("
            "timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{compression.segment_by}', "
            f"timescaledb.compress_orderby = '{order_by}');"
        )
    return statements


def ddl_hygiene_statements() -> tuple[list[str], list[str]]:
    """返回修订 0003 的 ``(upgrade, downgrade)`` 清单。

    upgrade：解压 → 删读模型（视图依赖列，否则类型变更被拒）→ varchar→text →
    重建读模型 → 压缩键覆盖物理键（segment_by + order_by）；
    downgrade：解压 → 删读模型 → 恢复旧压缩键 → 重建读模型
    （类型不回退：text→varchar 可能因数据超长失败）。
    """
    upgrade = [
        *_decompress_statements(),
        *entity_read_model_drop_statements(),
        *_text_type_statements(),
        *entity_read_model_statements(),
        *compression_statements(),
    ]
    downgrade = [
        *_decompress_statements(),
        *entity_read_model_drop_statements(),
        *compression_statements(_LEGACY_COMPRESSION_ORDER),
        *entity_read_model_statements(),
    ]
    return upgrade, downgrade


def render_ddl_hygiene_revision() -> str:
    """渲染修订 0003 源码（DDL 卫生；由字典生成，请勿手改）。"""
    upgrade, downgrade = ddl_hygiene_statements()
    lines = [
        '"""DDL 卫生（TASK-3.20）：canonical varchar→text；压缩键覆盖物理键。',
        "",
        "由 :func:`ddl_hygiene_statements` 生成，请勿手改；",
        "漂移校验：``tests/test_platform_migrations.py``。",
        "",
        "Revision ID: 0003_ddl_hygiene",
        "Revises: 0002_runtime_meta",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from alembic import op",
        "",
        'revision = "0003_ddl_hygiene"',
        'down_revision = "0002_runtime_meta"',
        "branch_labels = None",
        "depends_on = None",
        "",
        "",
        "UPGRADE_STATEMENTS = [",
        *_statement_literals(upgrade),
        "]",
        "",
        "",
        "DOWNGRADE_STATEMENTS = [",
        *_statement_literals(downgrade),
        "]",
        "",
        "",
        "def upgrade() -> None:",
        "    for statement in UPGRADE_STATEMENTS:",
        "        op.execute(statement)",
        "",
        "",
        "def downgrade() -> None:",
        "    for statement in DOWNGRADE_STATEMENTS:",
        "        op.execute(statement)",
        "",
    ]
    return "\n".join(lines)


def write_ddl_hygiene_revision(path: Path | None = None) -> Path:
    """写入/刷新修订 0003（开发者操作；CI 校验生成结果与文件一致）。"""
    target = path or DDL_HYGIENE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_ddl_hygiene_revision(), encoding="utf-8")
    return target


# ---------------------------------------------------------------- 修订 0004（派生引擎 meta）
ALGORITHM_META_REVISION = "0004_algorithm_meta"
ALGORITHM_META_PATH = (
    REPO_ROOT / "migrations" / "versions" / f"{ALGORITHM_META_REVISION}.py"
)


def algorithm_meta_statements() -> tuple[list[str], list[str]]:
    """返回派生引擎控制面（算法登记 / 升级台账 / 代次）的 ``(upgrade, downgrade)``。"""
    from fin_data_platform.derived.schema import metadata as derived_metadata

    upgrade = schema_sql(derived_metadata, dialect="postgresql", if_not_exists=True)
    downgrade = [
        f"DROP TABLE IF EXISTS {table.key};"
        for table in reversed(derived_metadata.sorted_tables)
    ]
    return upgrade, downgrade


def render_algorithm_meta_revision() -> str:
    """渲染修订 0004 源码（由 ``derived/schema.py`` 生成，请勿手改）。"""
    upgrade, downgrade = algorithm_meta_statements()
    lines = [
        '"""派生引擎 schema（meta.algorithm_registry / algorithm_events / data_generation）。',
        "",
        "由 derived/schema.py 生成，请勿手改；漂移校验：``tests/test_platform_migrations.py``。",
        "",
        "Revision ID: 0004_algorithm_meta",
        "Revises: 0003_ddl_hygiene",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from alembic import op",
        "",
        'revision = "0004_algorithm_meta"',
        'down_revision = "0003_ddl_hygiene"',
        "branch_labels = None",
        "depends_on = None",
        "",
        "",
        "UPGRADE_STATEMENTS = [",
        *_statement_literals(upgrade),
        "]",
        "",
        "",
        "DOWNGRADE_STATEMENTS = [",
        *_statement_literals(downgrade),
        "]",
        "",
        "",
        "def upgrade() -> None:",
        "    for statement in UPGRADE_STATEMENTS:",
        "        op.execute(statement)",
        "",
        "",
        "def downgrade() -> None:",
        "    for statement in DOWNGRADE_STATEMENTS:",
        "        op.execute(statement)",
        "",
    ]
    return "\n".join(lines)


def write_algorithm_meta_revision(path: Path | None = None) -> Path:
    """写入/刷新修订 0004（开发者操作；CI 校验生成结果与文件一致）。"""
    target = path or ALGORITHM_META_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_algorithm_meta_revision(), encoding="utf-8")
    return target


# ---------------------------------------------------------------- 版本查询
def expected_head_revision(dsn: str | None = None) -> str | None:
    """迁移脚本目录中的 head 修订（不连库）。"""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config(dsn))
    return script.get_current_head()


def current_revision(engine: Engine) -> str | None:
    """数据库当前修订（无版本表返回 ``None``）。"""
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _resolve_dsn(dsn: str | None) -> str:
    if dsn:
        return dsn
    return StorageConfig.from_env(
        host_override=os.environ.get("FDP_DATABASE_HOST")
    ).write_dsn


def _scaffold() -> tuple[Path, Path]:
    """定位 ``alembic.ini`` 与 ``migrations/``（部署可经环境变量覆盖）。

    注意：``pip install`` 不打包迁移脚本；容器/部署需显式携带这两个路径，
    或用 ``FDP_ALEMBIC_INI`` / ``FDP_ALEMBIC_SCRIPT_LOCATION`` 指定。
    """
    ini = Path(os.environ.get("FDP_ALEMBIC_INI", REPO_ROOT / "alembic.ini"))
    script = Path(
        os.environ.get("FDP_ALEMBIC_SCRIPT_LOCATION", ini.parent / "migrations")
    )
    if not ini.is_file():
        raise FileNotFoundError(
            f"找不到 alembic.ini: {ini}"
            "（部署需随包携带 alembic.ini 与 migrations/，或设置 FDP_ALEMBIC_INI）"
        )
    if not script.is_dir():
        raise FileNotFoundError(
            f"找不到迁移脚本目录: {script}（可用 FDP_ALEMBIC_SCRIPT_LOCATION 指定）"
        )
    return ini, script


def alembic_config(dsn: str | None = None) -> Config:
    """构建 Alembic 配置（仓库内 ``alembic.ini`` + ``migrations/``）。"""
    ini, script = _scaffold()
    config = Config(str(ini))
    config.set_main_option("script_location", str(script))
    # configparser 插值：DSN 中的 % 需转义
    config.set_main_option("sqlalchemy.url", _resolve_dsn(dsn).replace("%", "%%"))
    return config


def upgrade(dsn: str | None = None, revision: str = "head") -> None:
    """升级到指定修订（默认 head）；重复执行幂等。"""
    command.upgrade(alembic_config(dsn), revision)


def downgrade(dsn: str | None = None, revision: str = "base") -> None:
    """回滚到指定修订（默认 base）；重复执行幂等。"""
    command.downgrade(alembic_config(dsn), revision)
