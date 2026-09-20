"""Trial 计划/结果与 CaseAttempt 的 Trial 维度（M3-T02）。

需求 4.2/4.3：一 Task 一条逻辑 CaseRun，事前计划的重复单独存 ``trials``；
CaseAttempt 增加可空 ``trial_id``（旧无 Trial 写入保持空串），冲突域因此
从"整个 Case"收窄为"同一 Trial"，一个 Trial 先完成不会写死整个 Task。

降级安全（review R23）：``trials`` 里的计划/结果是冻结证据，
``case_attempts.trial_id`` 是把 attempt 绑定到 Trial 的索引。普通
``downgrade`` 只在两者都没有数据时执行；否则抛 ``RuntimeError`` 说明
要导出/清理什么，绝不静默丢证据。

Revision ID: 0008_trials
Revises: 0007_benchmark_datasets
"""
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

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


def downgrade_blockers(bind: Any) -> list[str]:
    """列出阻止降级的 Trial 证据；空列表表示可以安全降级。

    ``bind`` 是 SQLAlchemy Connection（``op.get_bind()``，测试里可用任意
    引擎的连接）。表不存在（未迁移的库）或没有 trial_id 列时不算 blocker：
    那时不可能存在 Trial 证据。
    """
    inspector = inspect(bind)
    blockers: list[str] = []
    if inspector.has_table("trials"):
        rows = int(bind.execute(text("SELECT count(*) FROM trials")).scalar() or 0)
        if rows:
            blockers.append(f"trials holds {rows} frozen Trial plan/result row(s)")
    if inspector.has_table("case_attempts"):
        columns = {column["name"] for column in inspector.get_columns("case_attempts")}
        if "trial_id" not in columns:
            return blockers
        bound = int(bind.execute(
            text("SELECT count(*) FROM case_attempts WHERE trial_id <> ''"),
        ).scalar() or 0)
        if bound:
            blockers.append(f"case_attempts holds {bound} trial-scoped attempt row(s)")
    return blockers


def _refusal(blockers: list[str]) -> RuntimeError:
    return RuntimeError(
        "refusing to downgrade 0008_trials: "
        + "; ".join(blockers)
        + ". Export the Trial evidence first, then delete it: dump trials "
        "(trial_id, run_id, task_key, repeat_index, payload, result_payload) and the "
        "case_attempts rows whose trial_id <> '' (e.g. pg_dump -t trials -t case_attempts "
        "or COPY ... TO a file outside the database). Run ALTER TABLE case_attempts DROP "
        "COLUMN trial_id / DROP TABLE trials only after trials is empty and every "
        "case_attempts.trial_id is ''. Test fixtures that only need to reset the schema "
        "must replay DOWN_STATEMENTS instead of calling downgrade()."
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
    # 有 Trial 证据时先拒绝：删除是不可逆的，普通降级不能静默丢证据（review R23）。
    blockers = downgrade_blockers(op.get_bind())
    if blockers:
        raise _refusal(blockers)
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
