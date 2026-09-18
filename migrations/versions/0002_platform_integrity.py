"""Revisioned runs and append-only execution/audit records.

Revision ID: 0002_platform_integrity
Revises: 0001_initial
"""
from alembic import op

revision = "0002_platform_integrity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

UP_STATEMENTS = (
    "ALTER TABLE runs ADD COLUMN revision BIGINT NOT NULL DEFAULT 0",
    """
    CREATE TABLE case_attempts (
        position BIGINT GENERATED ALWAYS AS IDENTITY,
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        case_id TEXT NOT NULL,
        attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
        status TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK (revision > 0),
        payload JSONB NOT NULL,
        UNIQUE (run_id, case_id, attempt_no)
    )
    """,
    "CREATE INDEX case_attempts_run_idx ON case_attempts (run_id, position)",
    """
    CREATE TABLE scoring_passes (
        position BIGINT GENERATED ALWAYS AS IDENTITY,
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX scoring_passes_run_idx ON scoring_passes (run_id, position)",
    """
    CREATE TABLE score_sets (
        scoring_pass_id TEXT NOT NULL REFERENCES scoring_passes(id),
        case_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (scoring_pass_id, case_id)
    )
    """,
    """
    CREATE TABLE run_commands (
        position BIGINT GENERATED ALWAYS AS IDENTITY,
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK (revision > 0),
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX run_commands_run_idx ON run_commands (run_id, position)",
)

DOWN_STATEMENTS = (
    "DROP TABLE IF EXISTS run_commands",
    "DROP TABLE IF EXISTS score_sets",
    "DROP TABLE IF EXISTS scoring_passes",
    "DROP TABLE IF EXISTS case_attempts",
    "ALTER TABLE runs DROP COLUMN IF EXISTS revision",
)


def upgrade() -> None:
    for statement in UP_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
