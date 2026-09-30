"""Server-owned Trace append time; legacy events remain unknown.

Revision ID: 0018_trace_retention
Revises: 0017_judge_calibrations
"""
from alembic import op
from sqlalchemy import inspect, text

revision = "0018_trace_retention"
down_revision = "0017_judge_calibrations"
branch_labels = None
depends_on = None

# Idempotent disposable-PG reset contract shared by the migration test harness.
DOWN_STATEMENTS = (
    "DROP TABLE IF EXISTS trace_archive_receipts",
    "ALTER TABLE IF EXISTS trace_events DROP COLUMN IF EXISTS stored_at",
)


def upgrade():
    # No DEFAULT and no payload-derived backfill: old timestamps are untrusted.
    op.execute("ALTER TABLE trace_events ADD COLUMN stored_at TEXT")
    payload_type = "JSONB" if op.get_bind().dialect.name == "postgresql" else "TEXT"
    op.execute("CREATE TABLE trace_archive_receipts ("
               "archive_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, run_id TEXT NOT NULL, "
               "first_seq BIGINT NOT NULL, last_seq BIGINT NOT NULL, payload " + payload_type +
               " NOT NULL, UNIQUE(run_id, first_seq, last_seq))")
    op.execute("CREATE INDEX ix_trace_archive_receipts_run ON trace_archive_receipts(run_id)")
    op.execute("CREATE INDEX ix_trace_archive_receipts_plan ON trace_archive_receipts(plan_id)")


def _maintenance_active(bind):
    return bool(inspect(bind).has_table("motte_meta") and bind.execute(text(
        "SELECT 1 FROM motte_meta WHERE meta_key = 'maintenance' AND meta_value = 'active'"
    )).scalar())


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        if not bind.connection.driver_connection.in_transaction:
            bind.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            bind.exec_driver_sql("UPDATE trace_events SET payload = payload WHERE 0")
    elif bind.dialect.name == "postgresql":
        if bind.get_isolation_level() != "READ COMMITTED":
            raise RuntimeError("refusing to downgrade 0018: READ COMMITTED required")
        bind.execute(text("SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))"))
        if _maintenance_active(bind):
            raise RuntimeError("refusing to downgrade 0018: maintenance is active")
        bind.execute(text("LOCK TABLE trace_events, trace_archive_receipts IN ACCESS EXCLUSIVE MODE"))
    if _maintenance_active(bind):
        raise RuntimeError("refusing to downgrade 0018: maintenance is active")
    if bind.execute(text("SELECT 1 FROM trace_archive_receipts LIMIT 1")).scalar():
        raise RuntimeError("refusing to downgrade 0018: Trace archive receipts must be preserved")
    if bind.execute(text("SELECT 1 FROM trace_events WHERE stored_at IS NOT NULL LIMIT 1")).scalar():
        raise RuntimeError("refusing to downgrade 0018: server-owned Trace times must be preserved")
    op.drop_table("trace_archive_receipts")
    op.drop_column("trace_events", "stored_at")
