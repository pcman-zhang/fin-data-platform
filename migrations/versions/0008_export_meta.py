"""导出请求表（meta.export_requests；TASK-3.10）。

由 export/schema.py 生成，请勿手改；漂移校验：``tests/test_platform_migrations.py``。

Revision ID: 0008_export_meta
Revises: 0007_quality_meta
"""

from __future__ import annotations

from alembic import op

revision = "0008_export_meta"
down_revision = "0007_quality_meta"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    """CREATE SCHEMA IF NOT EXISTS meta""",
    """CREATE TABLE IF NOT EXISTS meta.export_requests (
	export_id VARCHAR(32) NOT NULL, 
	dataset VARCHAR(64) NOT NULL, 
	params TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	format VARCHAR(8) NOT NULL, 
	artifact_path VARCHAR(255), 
	rows BIGINT, 
	bytes BIGINT, 
	error TEXT, 
	run_id INTEGER, 
	request_id VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (export_id)
)""",
    """CREATE INDEX IF NOT EXISTS ix_export_requests_status ON meta.export_requests (status, created_at)""",
]


DOWNGRADE_STATEMENTS = [
    """DROP TABLE IF EXISTS meta.export_requests;""",
]


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
