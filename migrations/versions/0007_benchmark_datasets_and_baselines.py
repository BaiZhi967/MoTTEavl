"""Benchmark dataset preparation + baseline snapshots (review M2-R13/R12).

外部 Benchmark 数据准备结果（清单/逐行内容/治理证据）与 Gate baseline
快照的持久化；契约见 ``motte_storage.benchmark_datasets`` 与
``motte_storage.baselines``。

``DOWN_STATEMENTS`` 供测试夹具按 revision 逆序清理全部表使用；它与
``downgrade()`` 必须保持同一组表名（新增表时两处一起改）。

Revision ID: 0007_benchmark_datasets
Revises: 0006_external_jobs
"""
from alembic import op

revision = "0007_benchmark_datasets"
down_revision = "0006_external_jobs"
branch_labels = None
depends_on = None

#: 与 ``downgrade()`` 对应的删除语句（测试夹具逆序清理时使用）。
DOWN_STATEMENTS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS baseline_snapshots",
    "DROP TABLE IF EXISTS benchmark_datasets",
)


def upgrade() -> None:
    op.execute(
        "CREATE TABLE benchmark_datasets (benchmark_id TEXT NOT NULL, dataset_revision TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (benchmark_id, dataset_revision))",  # noqa: E501
    )
    op.execute(
        "CREATE TABLE baseline_snapshots (position BIGINT GENERATED ALWAYS AS IDENTITY, id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scoring_pass_id TEXT NOT NULL, metrics JSONB NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",  # noqa: E501
    )
    op.execute("CREATE INDEX baseline_run_idx ON baseline_snapshots (run_id, position)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS baseline_snapshots")
    op.execute("DROP TABLE IF EXISTS benchmark_datasets")
