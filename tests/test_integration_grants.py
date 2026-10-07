"""只读角色与读写 DSN 分离集成测试（默认跳过：``pytest -m integration``）。

验证（真实 PostgreSQL + TimescaleDB）：

- 只读登录用户可 ``SELECT``：``mart`` / ``ref`` / 各数据域 canonical；
- 写入被数据库拒绝（不是代码约定）；
- 内部 schema（``meta``）不可见；
- 用后清理临时登录用户。
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL

from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.engine import create_read_engine, create_write_engine
from fin_data_platform.storage.grants import READONLY_ROLE, apply_readonly_roles

pytestmark = pytest.mark.integration


@pytest.fixture()
def writer_engine():
    try:
        config = StorageConfig.from_env(
            host_override=os.environ.get("FDP_DATABASE_HOST")
        )
    except ValueError as exc:
        pytest.skip(f"缺少数据库环境变量: {exc}")
    engine = create_write_engine(config)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 环境缺失则跳过
        pytest.skip(f"数据库不可达: {exc}")
    return engine


def test_reader_cannot_write_and_sees_only_granted_schemas(writer_engine) -> None:
    statements = apply_readonly_roles(writer_engine)  # 幂等授权
    assert statements, "授权脚本应产出语句"

    reader = f"fdp_ro_test_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    with writer_engine.begin() as connection:
        connection.execute(
            text(f'CREATE ROLE "{reader}" LOGIN PASSWORD \'{password}\'')
        )
        connection.execute(text(f'GRANT "{READONLY_ROLE}" TO "{reader}"'))

    writer_config = StorageConfig.from_env(
        host_override=os.environ.get("FDP_DATABASE_HOST")
    )
    writer_url = writer_config.write_dsn  # 仅用于取 host/port/name
    from sqlalchemy.engine import make_url

    base = make_url(writer_url)
    read_config = StorageConfig(
        write_dsn=writer_url,
        read_dsn=URL.create(
            "postgresql+psycopg",
            username=reader,
            password=password,
            host=base.host,
            port=base.port,
            database=base.database,
        ).render_as_string(hide_password=False),
    )
    read_engine = create_read_engine(read_config)

    try:
        # 只读可见：mart / ref / 数据域
        with read_engine.connect() as connection:
            for relation in (
                "mart.entity_latest_v1",
                "ref.entity",
                "cn_equity.daily_bar",
            ):
                connection.execute(text(f"SELECT count(*) FROM {relation}"))

        # 客户端连接兼容校验：只读角色可读迁移修订号（SDK/客户端用）
        with read_engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM public.alembic_version")
            ).scalar_one()
        assert revision

        # 写入被数据库拒绝
        with pytest.raises(Exception) as write_error, read_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO cn_equity.daily_bar "
                    "(entity_id, trade_date, knowledge_time, ingest_time, version, "
                    "provider, close) VALUES "
                    "(1, current_date, now(), now(), 1, 'test', 1.0)"
                )
            )
        assert "permission denied" in str(write_error.value).lower()

        # 内部 schema 不可见
        with pytest.raises(Exception) as meta_error, read_engine.connect() as connection:
            connection.execute(text("SELECT count(*) FROM meta.job_runs"))
        assert "permission denied" in str(meta_error.value).lower()

        # DEFAULT PRIVILEGES：writer 新建的表自动对 reader 可读
        with writer_engine.begin() as connection:
            connection.execute(text("CREATE TABLE cn_equity.fdp_ro_dp_probe (id int)"))
        try:
            with read_engine.connect() as connection:
                connection.execute(
                    text("SELECT count(*) FROM cn_equity.fdp_ro_dp_probe")
                )
        finally:
            with writer_engine.begin() as connection:
                connection.execute(text("DROP TABLE cn_equity.fdp_ro_dp_probe"))

        # TimescaleDB 压缩块：权限由扩展自动传播（存在内部 schema 时校验）
        with writer_engine.connect() as connection:
            has_internal = connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.schemata "
                    "WHERE schema_name = '_timescaledb_internal')"
                )
            ).scalar_one()
        if has_internal:
            with writer_engine.connect() as connection:
                allowed = connection.execute(
                    text(
                        "SELECT has_schema_privilege("
                        f"'{READONLY_ROLE}', '_timescaledb_internal', 'USAGE')"
                    )
                ).scalar_one()
            assert allowed is True
    finally:
        read_engine.dispose()
        writer_engine.dispose()
        with writer_engine.begin() as connection:
            connection.execute(text(f'REVOKE "{READONLY_ROLE}" FROM "{reader}"'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{reader}"'))
