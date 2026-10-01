"""Immutable judge calibration lifecycle, following statistical publications.

Revision ID: 0017_judge_calibrations
Revises: 0016_statistical_reports
"""
from alembic import op
from sqlalchemy import inspect, text

revision = '0017_judge_calibrations'
down_revision = '0016_statistical_reports'
branch_labels = None
depends_on = None

_TABLE_COLUMNS = {
    'judge_calibration_versions': 'calibration_id TEXT NOT NULL, version TEXT NOT NULL, content_sha256 TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (calibration_id, version)',
    'judge_calibration_reviews': 'review_id TEXT PRIMARY KEY, calibration_id TEXT NOT NULL, parent_version TEXT NOT NULL, child_version TEXT NOT NULL, payload TEXT NOT NULL',
    'judge_calibration_executions': 'execution_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, request_fingerprint TEXT NOT NULL, calibration_id TEXT NOT NULL, version TEXT NOT NULL, content_sha256 TEXT NOT NULL, payload TEXT NOT NULL',
    'judge_calibration_reports': 'report_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, content_sha256 TEXT NOT NULL, payload TEXT NOT NULL',
    'judge_calibration_qualifications': 'qualification_id TEXT PRIMARY KEY, report_id TEXT NOT NULL, content_sha256 TEXT NOT NULL, payload TEXT NOT NULL',
}
DOWN_STATEMENTS = tuple('DROP TABLE IF EXISTS ' + name for name in reversed(_TABLE_COLUMNS))


def upgrade():
    for name, columns in _TABLE_COLUMNS.items():
        op.execute('CREATE TABLE ' + name + ' (' + columns + ')')


def _maintenance_active(bind):
    return bool(inspect(bind).has_table('motte_meta') and bind.execute(text(
        "SELECT 1 FROM motte_meta WHERE meta_key = 'maintenance' AND meta_value = 'active'"
    )).scalar())


def downgrade_blockers(bind):
    blockers = []
    if _maintenance_active(bind):
        blockers.append('maintenance is active; release its owner before downgrading')
    for name in _TABLE_COLUMNS:
        if inspect(bind).has_table(name):
            count = int(bind.execute(text('SELECT count(*) FROM ' + name)).scalar() or 0)
            if count:
                blockers.append(f'{name} holds {count} row(s)')
    return blockers


def downgrade():
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
            bind.exec_driver_sql("UPDATE judge_calibration_versions SET payload = payload WHERE 0")
    elif bind.dialect.name == "postgresql":
        if bind.get_isolation_level() != 'READ COMMITTED':
            raise RuntimeError('refusing to downgrade 0017_judge_calibrations: READ COMMITTED required')
        bind.execute(text("SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))"))
        if _maintenance_active(bind):
            raise RuntimeError('refusing to downgrade 0017_judge_calibrations: maintenance is active')
        for name in _TABLE_COLUMNS:
            bind.execute(text('LOCK TABLE ' + name + ' IN ACCESS EXCLUSIVE MODE'))
    blockers = downgrade_blockers(bind)
    if blockers:
        raise RuntimeError('refusing to downgrade 0017_judge_calibrations: ' + '; '.join(blockers)
                           + '; immutable calibration evidence must be preserved')
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
