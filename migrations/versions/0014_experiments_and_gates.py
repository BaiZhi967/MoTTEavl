"""M6-T03/T05：Experiment、Gate 政策/结果、Baseline v2 与默认指针表。

experiment_specs / experiment_cells / gate_policies / gate_results /
m6_baselines / default_baselines / default_baseline_history 与 SQLite 侧
`motte_storage.run_store._SCHEMA` 的同名表同构：payload JSONB + 语义由
repository 保证（不可变、同内容幂等、异内容冲突；cell 分配状态机与 baseline
默认指针 CAS 均在 repository 层实现）。

降级安全：这些表保存的是冻结实验证据与门禁结论。普通 downgrade 只在
没有任何行时执行；否则具名拒绝，绝不静默删除证据。

Revision ID: 0014_experiments_and_gates
Revises: 0013_scenario_skill_resources
Create Date: 2026-09-21
"""
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

revision = "0014_experiments_and_gates"
down_revision = "0013_scenario_skill_resources"
branch_labels = None
depends_on = None

#: 与 downgrade() 内联语句同序（测试夹具逆序清理时使用）。
DOWN_STATEMENTS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS default_baseline_history",
    "DROP TABLE IF EXISTS default_baselines",
    "DROP TABLE IF EXISTS m6_baselines",
    "DROP INDEX IF EXISTS gate_results_policy_idx",
    "DROP TABLE IF EXISTS gate_results",
    "DROP TABLE IF EXISTS gate_policies",
    "DROP INDEX IF EXISTS experiment_cells_experiment_idx",
    "DROP TABLE IF EXISTS experiment_cells",
    "DROP TABLE IF EXISTS experiment_specs",
)

#: 每张证据表的计数语句（完整字面量，表名不拼接）。
_COUNT_SQL: dict[str, str] = {
    "experiment_specs": "SELECT count(*) FROM experiment_specs",
    "experiment_cells": "SELECT count(*) FROM experiment_cells",
    "gate_policies": "SELECT count(*) FROM gate_policies",
    "gate_results": "SELECT count(*) FROM gate_results",
    "m6_baselines": "SELECT count(*) FROM m6_baselines",
    "default_baselines": "SELECT count(*) FROM default_baselines",
    "default_baseline_history": "SELECT count(*) FROM default_baseline_history",
}


def downgrade_blockers(bind: Any) -> list[str]:
    """列出阻止降级的 M6 证据行；空列表表示可以安全降级。"""
    inspector = inspect(bind)
    blockers: list[str] = []
    for table, count_sql in _COUNT_SQL.items():
        if inspector.has_table(table):
            rows = int(bind.execute(text(count_sql)).scalar() or 0)
            if rows:
                blockers.append(table + " holds " + str(rows) + " row(s)")
    return blockers


def _refusal(blockers: list[str]) -> RuntimeError:
    return RuntimeError(
        "refusing to downgrade 0014_experiments_and_gates: "
        + "; ".join(blockers)
        + "; M6 experiment/gate/baseline evidence is frozen; delete explicitly"
        " if you really want to discard it"
    )


def upgrade() -> None:
    # 与 0007 的 baseline_snapshots 同款：position identity 提供稳定排序；
    # DDL 逐条内联（与 SQLite 侧 _SCHEMA 同构，payload 语义由 repository 保证）。
    op.execute(
        "CREATE TABLE experiment_specs (experiment_id TEXT NOT NULL,"
        " version TEXT NOT NULL, payload JSONB NOT NULL,"
        " PRIMARY KEY (experiment_id, version))"
    )
    op.execute(
        "CREATE TABLE experiment_cells (position BIGINT GENERATED ALWAYS AS IDENTITY,"
        " cell_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,"
        " experiment_version TEXT NOT NULL, allocation_status TEXT NOT NULL,"
        " payload JSONB NOT NULL)"
    )
    op.execute(
        "CREATE INDEX experiment_cells_experiment_idx ON experiment_cells"
        " (experiment_id, experiment_version)"
    )
    op.execute(
        "CREATE TABLE gate_policies (policy_id TEXT NOT NULL, version TEXT NOT NULL,"
        " payload JSONB NOT NULL, PRIMARY KEY (policy_id, version))"
    )
    op.execute(
        "CREATE TABLE gate_results (position BIGINT GENERATED ALWAYS AS IDENTITY,"
        " gate_result_id TEXT PRIMARY KEY, policy_id TEXT NOT NULL,"
        " payload JSONB NOT NULL)"
    )
    op.execute("CREATE INDEX gate_results_policy_idx ON gate_results (policy_id)")
    op.execute(
        "CREATE TABLE m6_baselines (position BIGINT GENERATED ALWAYS AS IDENTITY,"
        " baseline_id TEXT PRIMARY KEY, payload JSONB NOT NULL)"
    )
    op.execute(
        "CREATE TABLE default_baselines (scope TEXT PRIMARY KEY,"
        " baseline_id TEXT NOT NULL, position BIGINT NOT NULL,"
        " payload JSONB NOT NULL)"
    )
    op.execute(
        "CREATE TABLE default_baseline_history (scope TEXT NOT NULL,"
        " position BIGINT NOT NULL, payload JSONB NOT NULL,"
        " PRIMARY KEY (scope, position))"
    )


def downgrade() -> None:
    bind = op.get_bind()
    blockers = downgrade_blockers(bind)
    if blockers:
        raise _refusal(blockers)
    # 逐条内联执行（与 DOWN_STATEMENTS 同序）；语句不经变量传递。
    op.execute("DROP TABLE IF EXISTS default_baseline_history")
    op.execute("DROP TABLE IF EXISTS default_baselines")
    op.execute("DROP TABLE IF EXISTS m6_baselines")
    op.execute("DROP INDEX IF EXISTS gate_results_policy_idx")
    op.execute("DROP TABLE IF EXISTS gate_results")
    op.execute("DROP TABLE IF EXISTS gate_policies")
    op.execute("DROP INDEX IF EXISTS experiment_cells_experiment_idx")
    op.execute("DROP TABLE IF EXISTS experiment_cells")
    op.execute("DROP TABLE IF EXISTS experiment_specs")
