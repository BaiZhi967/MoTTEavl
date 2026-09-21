"""Durable runtime sessions and command dedupe (duplicates fail migration)."""
from alembic import op

revision = '0010_interactive_sessions'
down_revision = '0009_runtime_resources'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE runtime_sessions (
        session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        state TEXT NOT NULL, revision INTEGER NOT NULL, payload JSONB NOT NULL
    )''')
    op.execute('CREATE INDEX runtime_sessions_run_idx ON runtime_sessions(run_id)')
    op.execute('''CREATE UNIQUE INDEX run_commands_dedupe_idx
        ON run_commands(run_id, (payload->>'dedupe_key'))
        WHERE payload->>'dedupe_key' IS NOT NULL''')


def downgrade():
    op.execute('DROP INDEX run_commands_dedupe_idx')
    op.drop_table('runtime_sessions')
