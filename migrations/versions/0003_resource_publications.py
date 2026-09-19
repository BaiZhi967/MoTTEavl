"""Append-only resource publication audit records.

Revision ID: 0003_resource_publications
Revises: 0002_platform_integrity
"""
from alembic import op

revision = "0003_resource_publications"
down_revision = "0002_platform_integrity"
branch_labels = None
depends_on = None

UP_STATEMENTS = (
    """
    CREATE TABLE resource_publications (
        id TEXT PRIMARY KEY,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX resource_publications_created_idx ON resource_publications (created_at, id)",
)

DOWN_STATEMENTS = (
    "DROP TABLE IF EXISTS resource_publications",
)


def upgrade() -> None:
    for statement in UP_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
