"""Multi-metric score_sets composite identity.

A case can now carry several metric scores per scoring pass. The storage key
becomes (scoring_pass_id, case_id, trial_id, metric_id, evaluator_id,
evaluator_version) with '' as the canonical no-trial/no-metric value so NULL
uniqueness differences between engines cannot alias rows. trial_id is reserved
for M3 trials; M1 writes always use ''.

SQLite databases upgrade in place at store construction
(``motte_storage.run_store``); this Alembic revision covers PostgreSQL.

Revision ID: 0004_multi_metric_score_sets
Revises: 0003_resource_publications
"""
from alembic import op

revision = "0004_multi_metric_score_sets"
down_revision = "0003_resource_publications"
branch_labels = None
depends_on = None

UP_STATEMENTS = (
    """
    CREATE TABLE score_sets_new (
        scoring_pass_id TEXT NOT NULL REFERENCES scoring_passes(id),
        case_id TEXT NOT NULL,
        trial_id TEXT NOT NULL DEFAULT '',
        metric_id TEXT NOT NULL DEFAULT '',
        evaluator_id TEXT NOT NULL DEFAULT '',
        evaluator_version TEXT NOT NULL DEFAULT '',
        ordinal INTEGER NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (scoring_pass_id, case_id, trial_id, metric_id, evaluator_id, evaluator_version)
    )
    """,
    """
    INSERT INTO score_sets_new
        (scoring_pass_id, case_id, trial_id, metric_id, evaluator_id, evaluator_version, ordinal, payload)
        SELECT scoring_pass_id, case_id, '', '', '', '', ordinal, payload FROM score_sets
    """,
    "DROP TABLE score_sets",
    "ALTER TABLE score_sets_new RENAME TO score_sets",
)

DOWN_STATEMENTS = (
    # Guarded block: the e2e teardown executes downgrade statements directly on
    # databases where score_sets may not exist yet.
    """
    DO $$
    BEGIN
        IF to_regclass('public.score_sets') IS NOT NULL THEN
            CREATE TABLE score_sets_old (
                scoring_pass_id TEXT NOT NULL REFERENCES scoring_passes(id),
                case_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                payload JSONB NOT NULL,
                PRIMARY KEY (scoring_pass_id, case_id)
            );
            -- Downgrade keeps one row per legacy case; metric rows beyond the first are dropped.
            INSERT INTO score_sets_old (scoring_pass_id, case_id, ordinal, payload)
                SELECT DISTINCT ON (scoring_pass_id, case_id)
                       scoring_pass_id, case_id, ordinal, payload
                FROM score_sets ORDER BY scoring_pass_id, case_id, ordinal;
            DROP TABLE score_sets;
            ALTER TABLE score_sets_old RENAME TO score_sets;
        END IF;
    END $$;
    """,
)


def upgrade() -> None:
    for statement in UP_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
