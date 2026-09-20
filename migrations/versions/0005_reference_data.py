"""参考数据表（ref.market / ref.trade_calendar）：字典落库（含 hypertable/压缩）。

由字典生成，请勿手改；漂移校验：``tests/test_platform_migrations.py``。

Revision ID: 0005_reference_data
Revises: 0004_algorithm_meta
"""

from __future__ import annotations

from alembic import op

revision = "0005_reference_data"
down_revision = "0004_algorithm_meta"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    """CREATE SCHEMA IF NOT EXISTS ref""",
    """CREATE TABLE IF NOT EXISTS ref.market (
	exchange_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	market TEXT NOT NULL, 
	timezone TEXT NOT NULL, 
	currency TEXT NOT NULL, 
	valid_from DATE NOT NULL, 
	valid_to DATE, 
	knowledge_time TIMESTAMP WITH TIME ZONE NOT NULL, 
	version BIGINT NOT NULL, 
	PRIMARY KEY (exchange_id, valid_from, knowledge_time, version)
)""",
    """CREATE INDEX IF NOT EXISTS ix_market_business ON ref.market (exchange_id, valid_from)""",
    """CREATE TABLE IF NOT EXISTS ref.trade_calendar (
	exchange_id TEXT NOT NULL, 
	trade_date DATE NOT NULL, 
	is_open BOOLEAN NOT NULL, 
	pretrade_date DATE, 
	knowledge_time TIMESTAMP WITH TIME ZONE NOT NULL, 
	version BIGINT NOT NULL, 
	PRIMARY KEY (exchange_id, trade_date, knowledge_time, version)
)""",
    """CREATE INDEX IF NOT EXISTS ix_trade_calendar_business ON ref.trade_calendar (exchange_id, trade_date)""",
    """SELECT create_hypertable('ref.trade_calendar', 'trade_date', chunk_time_interval => INTERVAL '1 year', migrate_data => TRUE, if_not_exists => TRUE);""",
    """ALTER TABLE ref.trade_calendar SET (timescaledb.compress, timescaledb.compress_segmentby = 'exchange_id', timescaledb.compress_orderby = 'trade_date, knowledge_time, version');""",
    """SELECT add_compression_policy('ref.trade_calendar', INTERVAL '7 days', if_not_exists => TRUE);""",
]


DOWNGRADE_STATEMENTS = [
    """DROP TABLE IF EXISTS ref.trade_calendar;""",
    """DROP TABLE IF EXISTS ref.market;""",
]


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
