"""Immutable statistical reports, stored as canonical TEXT on every backend.

Revision ID: 0016_statistical_reports
Revises: 0015_m7_platform_tables
"""
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

revision = "0016_statistical_reports"
down_revision = "0015_m7_platform_tables"
branch_labels = None
depends_on = None

DOWN_STATEMENTS: tuple[str, ...] = ("DROP TABLE IF EXISTS statistical_reports",)


def upgrade() -> None:
    op.execute(
        "CREATE TABLE statistical_reports (report_id TEXT PRIMARY KEY, "
        "body TEXT NOT NULL, published_at TEXT NOT NULL)"
    )


def _maintenance_active(bind: Any) -> bool:
    return bool(inspect(bind).has_table("motte_meta") and bind.execute(text(
        "SELECT 1 FROM motte_meta WHERE meta_key = 'maintenance' AND meta_value = 'active'"
    )).scalar())


def downgrade_blockers(bind: Any) -> list[str]:
    blockers = []
    if _maintenance_active(bind):
        blockers.append("maintenance is active; release its owner before downgrading")
    if inspect(bind).has_table("statistical_reports"):
        count = int(bind.execute(text("SELECT count(*) FROM statistical_reports")).scalar() or 0)
        if count:
            blockers.append(f"statistical_reports holds {count} row(s)")
    return blockers


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLAlchemy's logical transaction need not have issued a native BEGIN:
        # sqlite3's legacy mode leaves SELECT and DDL outside a transaction.
        # Retain the caller's transaction/savepoints and never commit here.
        if not bind.connection.driver_connection.in_transaction:
            bind.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            # Upgrade a deferred/read transaction to writer exclusion without
            # changing any evidence or ending the caller's transaction.
            bind.exec_driver_sql("UPDATE statistical_reports SET body = body WHERE 0")
    elif bind.dialect.name == "postgresql":
        # An earlier transaction snapshot could miss a writer's committed row
        # even after its lock drains. Refuse rather than discard that evidence.
        if bind.get_isolation_level() != "READ COMMITTED":
            raise RuntimeError(
                "refusing to downgrade 0016_statistical_reports: READ COMMITTED "
                "isolation is required to preserve concurrently committed reports"
            )
        # Serialize with maintenance activation/release through commit. Refuse
        # an active owner before taking table locks: its SHARE barrier (and any
        # writer queued behind it) must not strand the owner-release operation.
        bind.execute(text("SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))"))
        if _maintenance_active(bind):
            raise RuntimeError(
                "refusing to downgrade 0016_statistical_reports: maintenance is active; "
                "release its owner before downgrading"
            )
        # Take the eventual DROP lock before counting. Drain prior writers and
        # keep later inserts out until this migration transaction commits.
        bind.execute(text("LOCK TABLE statistical_reports IN ACCESS EXCLUSIVE MODE"))
    blockers = downgrade_blockers(bind)
    if blockers:
        raise RuntimeError(
            "refusing to downgrade 0016_statistical_reports: " + "; ".join(blockers)
            + "; immutable statistical evidence must be preserved"
        )
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
