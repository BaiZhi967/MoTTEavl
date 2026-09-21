"""M5-T01：Workflow 版本资源表（不可变版本资源）。

workflow_versions 与 runtime_versions / dataset_versions 同构：
(workflow_id, version) 复合主键 + payload JSONB，语义由
motte_storage.resource_store 的 VERSIONED_TABLES 保证（同内容幂等、
异内容冲突、不可删除）。DDL 为静态字面量，不复制旧平台结构。

降级安全：已发布 Workflow 快照与引用它的 Run manifest 都是冻结证据。普通
downgrade 只在两者都不存在时执行；否则具名拒绝，绝不静默删除证据。

Revision ID: 0011_workflow_resources
Revises: 0010_interactive_sessions
Create Date: 2026-09-21
"""
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

revision = "0011_workflow_resources"
down_revision = "0010_interactive_sessions"
branch_labels = None
depends_on = None

#: 与 downgrade() 对应的删除语句（测试夹具逆序清理时使用）。
DOWN_STATEMENTS: tuple[str, ...] = ("DROP TABLE IF EXISTS workflow_versions",)

#: Run manifest 里冻结 Workflow 快照的保留键（M5-T05 装配时写入）。
_EVIDENCE_LIKE = "SELECT count(*) FROM runs WHERE CAST(manifest AS TEXT) LIKE '%\"workflow\"%'"


def downgrade_blockers(bind: Any) -> list[str]:
    """列出阻止降级的 Workflow 证据；空列表表示可以安全降级。

    bind 是 SQLAlchemy Connection。表不存在（未迁移的库）不算 blocker；
    runs 不存在时只检查 Workflow 版本表本身。
    """
    inspector = inspect(bind)
    blockers: list[str] = []
    if inspector.has_table("workflow_versions"):
        rows = int(bind.execute(text("SELECT count(*) FROM workflow_versions")).scalar() or 0)
        if rows:
            blockers.append(f"workflow_versions holds {rows} published workflow version row(s)")
    if inspector.has_table("runs"):
        columns = {column["name"] for column in inspector.get_columns("runs")}
        if "manifest" in columns:
            bound = int(bind.execute(text(_EVIDENCE_LIKE)).scalar() or 0)
            if bound:
                blockers.append(
                    f"runs holds {bound} run manifest(s) referencing a frozen workflow snapshot"
                )
    return blockers


def _refusal(blockers: list[str]) -> RuntimeError:
    return RuntimeError(
        "refusing to downgrade 0011_workflow_resources: "
        + "; ".join(blockers)
        + ". Export the workflow evidence first (pg_dump -t workflow_versions, plus the "
        "runs whose manifest carries a workflow snapshot), then delete it explicitly. "
        "Test fixtures that only need to reset the schema must replay DOWN_STATEMENTS "
        "instead of calling downgrade()."
    )


def upgrade() -> None:
    op.execute(
        "CREATE TABLE workflow_versions ("
        " workflow_id TEXT NOT NULL,"
        " version TEXT NOT NULL,"
        " payload JSONB NOT NULL,"
        " PRIMARY KEY (workflow_id, version))"
    )


def downgrade() -> None:
    blockers = downgrade_blockers(op.get_bind())
    if blockers:
        raise _refusal(blockers)
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
