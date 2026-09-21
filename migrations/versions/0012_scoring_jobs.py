"""M5-T09b：durable ScoringJob 表（评分请求 -> job -> 预留 pass id）。

一个评分请求以 request_key（幂等键，与内容 fingerprint 分离）唯一标识；
同键同内容复用，同键异内容由存储层拒绝（409 语义）。job 状态机与 CAS 语义由
motte_storage.scoring_jobs 保证；这里只建立承载它的表。

降级安全：job 记录（尤其是 indeterminate 待复核记录）和由 job 发布的
ScoringPass 都是证据。普通 downgrade 只在两者都不存在时执行；否则具名拒绝，
绝不静默删除证据。

Revision ID: 0012_scoring_jobs
Revises: 0011_workflow_resources
Create Date: 2026-09-21
"""
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

revision = "0012_scoring_jobs"
down_revision = "0011_workflow_resources"
branch_labels = None
depends_on = None

#: 与 downgrade() 对应的删除语句（测试夹具逆序清理时使用）。
DOWN_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS scoring_jobs_status_idx",
    "DROP INDEX IF EXISTS scoring_jobs_run_idx",
    "DROP INDEX IF EXISTS scoring_jobs_request_key_idx",
    "DROP TABLE IF EXISTS scoring_jobs",
)

#: 由评分作业发布的 pass（judge 用途）——历史评分证据。
_JUDGE_PASSES = (
    "SELECT count(*) FROM scoring_passes WHERE payload->>'purpose' = 'judge'"
)


def downgrade_blockers(bind: Any) -> list[str]:
    """列出阻止降级的评分作业证据；空列表表示可以安全降级。"""
    inspector = inspect(bind)
    blockers: list[str] = []
    if inspector.has_table("scoring_jobs"):
        rows = int(bind.execute(text("SELECT count(*) FROM scoring_jobs")).scalar() or 0)
        if rows:
            blockers.append(f"scoring_jobs holds {rows} durable scoring job row(s)")
    if inspector.has_table("scoring_passes"):
        columns = {column["name"] for column in inspector.get_columns("scoring_passes")}
        if "payload" in columns:
            bound = int(bind.execute(text(_JUDGE_PASSES)).scalar() or 0)
            if bound:
                blockers.append(
                    f"scoring_passes holds {bound} judge-published pass row(s)"
                )
    return blockers


def _refusal(blockers: list[str]) -> RuntimeError:
    return RuntimeError(
        "refusing to downgrade 0012_scoring_jobs: "
        + "; ".join(blockers)
        + ". Export the scoring-job evidence first (pg_dump -t scoring_jobs, plus the "
        "scoring_passes whose payload carries purpose=judge), then delete it explicitly. "
        "Test fixtures that only need to reset the schema must replay DOWN_STATEMENTS "
        "instead of calling downgrade()."
    )


def upgrade() -> None:
    op.execute(
        "CREATE TABLE scoring_jobs ("
        " position BIGINT GENERATED ALWAYS AS IDENTITY,"
        " job_id TEXT PRIMARY KEY,"
        " request_key TEXT NOT NULL,"
        " fingerprint TEXT NOT NULL,"
        " owner_kind TEXT NOT NULL,"
        " owner_ref TEXT NOT NULL,"
        " run_id TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL,"
        " revision BIGINT NOT NULL CHECK (revision > 0),"
        " reserved_pass_id TEXT NOT NULL,"
        " payload JSONB NOT NULL)"
    )
    op.execute(
        "CREATE UNIQUE INDEX scoring_jobs_request_key_idx ON scoring_jobs (request_key)"
    )
    op.execute("CREATE INDEX scoring_jobs_run_idx ON scoring_jobs (run_id, position)")
    op.execute("CREATE INDEX scoring_jobs_status_idx ON scoring_jobs (status, position)")


def downgrade() -> None:
    blockers = downgrade_blockers(op.get_bind())
    if blockers:
        raise _refusal(blockers)
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
