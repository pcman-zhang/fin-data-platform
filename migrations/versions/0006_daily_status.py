"""每日状态表（cn_equity.daily_status）：字典落库（含 hypertable/压缩）。

由字典生成，请勿手改；漂移校验：``tests/test_platform_migrations.py``。

Revision ID: 0006_daily_status
Revises: 0005_reference_data
"""

from __future__ import annotations

from alembic import op

revision = "0006_daily_status"
down_revision = "0005_reference_data"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    """CREATE SCHEMA IF NOT EXISTS cn_equity""",
    """CREATE TABLE IF NOT EXISTS cn_equity.daily_status (
	entity_id BIGINT NOT NULL, 
	trade_date DATE NOT NULL, 
	is_suspended BOOLEAN NOT NULL, 
	is_st BOOLEAN NOT NULL, 
	knowledge_time TIMESTAMP WITH TIME ZONE NOT NULL, 
	ingest_time TIMESTAMP WITH TIME ZONE NOT NULL, 
	version BIGINT NOT NULL, 
	provider TEXT NOT NULL, 
	PRIMARY KEY (entity_id, trade_date, knowledge_time, version)
)""",
    """CREATE INDEX IF NOT EXISTS ix_daily_status_business ON cn_equity.daily_status (entity_id, trade_date)""",
    """SELECT create_hypertable('cn_equity.daily_status', 'trade_date', chunk_time_interval => INTERVAL '1 month', migrate_data => TRUE, if_not_exists => TRUE);""",
    """ALTER TABLE cn_equity.daily_status SET (timescaledb.compress, timescaledb.compress_segmentby = 'entity_id', timescaledb.compress_orderby = 'trade_date, knowledge_time, version');""",
    """SELECT add_compression_policy('cn_equity.daily_status', INTERVAL '7 days', if_not_exists => TRUE);""",
]


DOWNGRADE_STATEMENTS = [
    """DROP TABLE IF EXISTS cn_equity.daily_status;""",
]


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
