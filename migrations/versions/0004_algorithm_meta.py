"""派生引擎 schema（meta.algorithm_registry / algorithm_events / data_generation）。

由 derived/schema.py 生成，请勿手改；漂移校验：``tests/test_platform_migrations.py``。

Revision ID: 0004_algorithm_meta
Revises: 0003_ddl_hygiene
"""

from __future__ import annotations

from alembic import op

revision = "0004_algorithm_meta"
down_revision = "0003_ddl_hygiene"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    """CREATE SCHEMA IF NOT EXISTS meta""",
    """CREATE TABLE IF NOT EXISTS meta.algorithm_events (
	event_id SERIAL NOT NULL, 
	algorithm_id VARCHAR(64) NOT NULL, 
	effective_from DATE NOT NULL, 
	reason TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (event_id), 
	CONSTRAINT uq_algorithm_events_id_from UNIQUE (algorithm_id, effective_from)
)""",
    """CREATE INDEX IF NOT EXISTS ix_algorithm_events_algorithm ON meta.algorithm_events (algorithm_id)""",
    """CREATE TABLE IF NOT EXISTS meta.algorithm_registry (
	algorithm_id VARCHAR(64) NOT NULL, 
	version INTEGER NOT NULL, 
	owner VARCHAR(64) NOT NULL, 
	implementation VARCHAR(255) NOT NULL, 
	dataset VARCHAR(64), 
	output VARCHAR(64), 
	inputs TEXT, 
	description TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	effective_from DATE, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (algorithm_id)
)""",
    """CREATE TABLE IF NOT EXISTS meta.data_generation (
	read_model VARCHAR(128) NOT NULL, 
	generation VARCHAR(32) NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (read_model)
)""",
]


DOWNGRADE_STATEMENTS = [
    """DROP TABLE IF EXISTS meta.data_generation;""",
    """DROP TABLE IF EXISTS meta.algorithm_registry;""",
    """DROP TABLE IF EXISTS meta.algorithm_events;""",
]


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
