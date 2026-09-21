"""M4-T01：runtime 版本与 profile 资源表（不可变版本资源）。

runtime_versions / runtime_profiles 与 dataset_versions 同构：
(name, version) 复合主键 + payload JSONB，语义由 motte_storage.resource_store
的 VERSIONED_TABLES 保证（同内容幂等、异内容冲突、不可删除）。
DDL 为静态字面量，与 0001_initial 的资源表同构。

Revision ID: 0009_runtime_resources
Revises: 0008_trials
Create Date: 2026-09-20
"""
from alembic import op

revision = "0009_runtime_resources"
down_revision = "0008_trials"
branch_labels = None
depends_on = None

# Shared with the PostgreSQL fixture's reverse-order schema reset.
DOWN_STATEMENTS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS runtime_profiles",
    "DROP TABLE IF EXISTS runtime_versions",
)


def upgrade() -> None:
    op.execute(
        "CREATE TABLE runtime_versions ("
        " name TEXT NOT NULL,"
        " version TEXT NOT NULL,"
        " payload JSONB NOT NULL,"
        " PRIMARY KEY (name, version))"
    )
    op.execute(
        "CREATE TABLE runtime_profiles ("
        " name TEXT NOT NULL,"
        " version TEXT NOT NULL,"
        " payload JSONB NOT NULL,"
        " PRIMARY KEY (name, version))"
    )


def downgrade() -> None:
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
