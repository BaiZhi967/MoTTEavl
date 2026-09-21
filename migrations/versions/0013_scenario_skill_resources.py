"""M5-T02/T06：Fixture 与 Skill 版本资源表（不可变版本资源）。

fixture_versions / skill_versions 与 workflow_versions 同构：
复合主键 + payload JSONB，语义由 motte_storage.resource_store 的
VERSIONED_TABLES 保证（同内容幂等、异内容冲突、不可删除）。Skill 额外允许
published→deprecated 这一种受控转换，其余变化一律冲突。

降级安全：已发布 Fixture/Skill 与引用它们的 Run 都是冻结证据。普通
downgrade 只在没有任何行时执行；否则具名拒绝，绝不静默删除证据。

Revision ID: 0013_scenario_skill_resources
Revises: 0012_scoring_jobs
Create Date: 2026-09-21
"""
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

revision = "0013_scenario_skill_resources"
down_revision = "0012_scoring_jobs"
branch_labels = None
depends_on = None

#: 与 downgrade() 对应的删除语句（测试夹具逆序清理时使用）。
DOWN_STATEMENTS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS skill_versions",
    "DROP TABLE IF EXISTS fixture_versions",
)

_EVIDENCE_KEYS = ("fixture_snapshot", "skill_snapshot")


def downgrade_blockers(bind: Any) -> list[str]:
    """列出阻止降级的 Fixture/Skill 证据；空列表表示可以安全降级。"""
    inspector = inspect(bind)
    blockers: list[str] = []
    for table in ("fixture_versions", "skill_versions"):
        if inspector.has_table(table):
            rows = int(bind.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)
            if rows:
                blockers.append(f"{table} holds {rows} published version row(s)")
    if inspector.has_table("runs"):
        columns = {column["name"] for column in inspector.get_columns("runs")}
        if "manifest" in columns:
            for key in _EVIDENCE_KEYS:
                rows = int(bind.execute(text(
                    "SELECT count(*) FROM runs WHERE CAST(manifest AS TEXT) LIKE "
                    f"'%\"{key}\"%'"
                )).scalar() or 0)
                if rows:
                    blockers.append(
                        f"runs holds {rows} run manifest(s) referencing {key}"
                    )
    return blockers


def _refusal(blockers: list[str]) -> RuntimeError:
    return RuntimeError(
        "refusing to downgrade 0013_scenario_skill_resources: "
        + "; ".join(blockers)
        + ". Export the fixture/skill evidence first (pg_dump -t fixture_versions "
        "-t skill_versions plus the referencing runs), then delete it explicitly. "
        "Test fixtures that only need to reset the schema must replay DOWN_STATEMENTS "
        "instead of calling downgrade()."
    )


def upgrade() -> None:
    op.execute(
        "CREATE TABLE fixture_versions ("
        " fixture_id TEXT NOT NULL,"
        " version TEXT NOT NULL,"
        " payload JSONB NOT NULL,"
        " PRIMARY KEY (fixture_id, version))"
    )
    op.execute(
        "CREATE TABLE skill_versions ("
        " skill_id TEXT NOT NULL,"
        " version TEXT NOT NULL,"
        " payload JSONB NOT NULL,"
        " PRIMARY KEY (skill_id, version))"
    )


def downgrade() -> None:
    blockers = downgrade_blockers(op.get_bind())
    if blockers:
        raise _refusal(blockers)
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
