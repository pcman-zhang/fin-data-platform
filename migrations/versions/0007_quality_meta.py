"""质量结果表（meta.quality_results；TASK-3.5）。

由 quality/schema.py 生成，请勿手改；漂移校验：``tests/test_platform_migrations.py``。

Revision ID: 0007_quality_meta
Revises: 0006_daily_status
"""

from __future__ import annotations

from alembic import op

revision = "0007_quality_meta"
down_revision = "0006_daily_status"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    """CREATE SCHEMA IF NOT EXISTS meta""",
    """CREATE TABLE IF NOT EXISTS meta.quality_results (
	run_id INTEGER NOT NULL, 
	dataset VARCHAR(64) NOT NULL, 
	check_id VARCHAR(64) NOT NULL, 
	family VARCHAR(16) NOT NULL, 
	severity VARCHAR(8) NOT NULL, 
	status VARCHAR(8) NOT NULL, 
	window_start DATE, 
	window_end DATE, 
	rows_checked BIGINT, 
	violations BIGINT NOT NULL, 
	samples TEXT, 
	metrics TEXT, 
	message TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (run_id, dataset, check_id)
)""",
    """CREATE INDEX IF NOT EXISTS ix_quality_results_window ON meta.quality_results (window_end, dataset)""",
]


DOWNGRADE_STATEMENTS = [
    """DROP TABLE IF EXISTS meta.quality_results;""",
]


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
