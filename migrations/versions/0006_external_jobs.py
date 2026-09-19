"""External job persistence (M2-T03).

外部 Job 记录、幂等导入记录与冲突账本；契约见
``motte_storage.external_jobs`` 与需求 4.1/4.3。Job 状态是外部作业子过程，
不替代 Run 状态。

Revision ID: 0006_external_jobs
Revises: 0005_agent_invocations
"""
from alembic import op

revision = "0006_external_jobs"
down_revision = "0005_agent_invocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE external_jobs (position BIGINT GENERATED ALWAYS AS IDENTITY, job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL, launch_token TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), payload JSONB NOT NULL)",  # noqa: E501
    )
    op.execute("CREATE INDEX external_jobs_run_idx ON external_jobs (run_id, position)")
    op.execute(
        "CREATE TABLE external_job_records (position BIGINT GENERATED ALWAYS AS IDENTITY, job_id TEXT NOT NULL, source_record_key TEXT NOT NULL, parser_version TEXT NOT NULL, content_hash TEXT NOT NULL, payload JSONB NOT NULL, imported_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (job_id, source_record_key, parser_version))",  # noqa: E501
    )
    op.execute(
        "CREATE TABLE external_job_conflicts (position BIGINT GENERATED ALWAYS AS IDENTITY, job_id TEXT NOT NULL, source_record_key TEXT NOT NULL, parser_version TEXT NOT NULL, existing_hash TEXT NOT NULL, incoming_hash TEXT NOT NULL, incoming_payload JSONB NOT NULL, detected_at TIMESTAMPTZ NOT NULL)",  # noqa: E501
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS external_job_conflicts")
    op.execute("DROP TABLE IF EXISTS external_job_records")
    op.execute("DROP TABLE IF EXISTS external_jobs")
