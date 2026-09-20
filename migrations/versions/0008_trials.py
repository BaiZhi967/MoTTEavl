"""Trial 计划/结果与 CaseAttempt 的 Trial 维度（M3-T02）。

需求 4.2/4.3：一 Task 一条逻辑 CaseRun，事前计划的重复单独存 ``trials``；
CaseAttempt 增加可空 ``trial_id``（旧无 Trial 写入保持空串），冲突域因此
从"整个 Case"收窄为"同一 Trial"，一个 Trial 先完成不会写死整个 Task。

Revision ID: 0008_trials
Revises: 0007_benchmark_datasets
"""
from alembic import op

revision = "0008_trials"
down_revision = "0007_benchmark_datasets"
branch_labels = None
depends_on = None

#: 与 ``downgrade()`` 对应的删除语句（测试夹具逆序清理时使用）。
DOWN_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS case_attempts_trial_idx",
    "ALTER TABLE case_attempts DROP COLUMN IF EXISTS trial_id",
    "DROP TABLE IF EXISTS trials",
)


def upgrade() -> None:
    op.execute(
        "CREATE TABLE trials (trial_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_key TEXT NOT NULL, repeat_index INTEGER NOT NULL, status TEXT NOT NULL, plan_hash TEXT NOT NULL, payload JSONB NOT NULL, result_payload JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ)",  # noqa: E501
    )
    op.execute("CREATE INDEX trials_run_idx ON trials (run_id, task_key, repeat_index)")
    op.execute(
        "ALTER TABLE case_attempts ADD COLUMN IF NOT EXISTS trial_id TEXT NOT NULL DEFAULT ''",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS case_attempts_trial_idx ON case_attempts (run_id, case_id, trial_id)",  # noqa: E501
    )


def downgrade() -> None:
    # 降级只删除 Trial 维度与表；case_attempts 的既有行与评分证据不动。
    op.execute("DROP INDEX IF EXISTS case_attempts_trial_idx")
    op.execute("ALTER TABLE case_attempts DROP COLUMN IF EXISTS trial_id")
    op.execute("DROP TABLE IF EXISTS trials")
