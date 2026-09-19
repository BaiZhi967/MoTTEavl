"""Agent invocation boundary log.

每次模型/工具调用的持久 prepared/dispatching/settled 边界；契约见
``motte_contracts.evaluation.InvocationRecord``。

Revision ID: 0005_agent_invocations
Revises: 0004_multi_metric_score_sets
"""
from alembic import op

revision = "0005_agent_invocations"
down_revision = "0004_multi_metric_score_sets"
branch_labels = None
depends_on = None

UP_STATEMENTS = (
    """
    CREATE TABLE agent_invocations (
        position BIGINT GENERATED ALWAYS AS IDENTITY,
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        case_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        step BIGINT NOT NULL,
        status TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK (revision > 0),
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX agent_invocations_run_idx ON agent_invocations (run_id, position)",
)

DOWN_STATEMENTS = ("DROP TABLE IF EXISTS agent_invocations",)


def upgrade() -> None:
    for statement in UP_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
