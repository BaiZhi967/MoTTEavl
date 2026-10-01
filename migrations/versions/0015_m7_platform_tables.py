"""M7：平台级表——幂等请求注册表、元数据 KV、导入账本与 GC tombstone。

motte_request_keys / motte_meta / motte_imports / motte_import_mappings /
motte_gc_tombstones 与 SQLite 侧 `motte_storage.platform.PLATFORM_SCHEMA`
的同名表同构（payload 列 PG 用 JSONB），语义由 motte_storage.platform 的
repository 保证（同 key 同内容幂等、异内容具名冲突）。

降级安全：motte_imports / motte_import_mappings 保存导入审计与 checkpoint，
motte_gc_tombstones 保存删除审计；有行时普通 downgrade 具名拒绝。
motte_request_keys / motte_meta 仅为运行时协调数据；维护屏障解除后才可降级。
运行时安装的维护 trigger/function 依赖 motte_meta，必须先移除自有 trigger，
再以 RESTRICT 删除 function，最后删除 metadata，避免遗留守卫破坏旧版本写入。

Revision ID: 0015_m7_platform_tables
Revises: 0014_experiments_and_gates
Create Date: 2026-09-22
"""
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

revision = "0015_m7_platform_tables"
down_revision = "0014_experiments_and_gates"
branch_labels = None
depends_on = None

DOWN_STATEMENTS: tuple[str, ...] = (
    """DO $$
    DECLARE owned_trigger RECORD;
    BEGIN
      FOR owned_trigger IN
        SELECT namespace.nspname AS schema_name, relation.relname AS table_name
        FROM pg_catalog.pg_trigger AS guard
        JOIN pg_catalog.pg_class AS relation ON relation.oid = guard.tgrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND guard.tgname = 'motte_maintenance_write'
          AND guard.tgfoid = to_regprocedure('public.motte_maintenance_guard()')
          AND NOT guard.tgisinternal
      LOOP
        EXECUTE format('DROP TRIGGER %I ON %I.%I', 'motte_maintenance_write',
                       owned_trigger.schema_name, owned_trigger.table_name);
      END LOOP;
    END $$""",
    "DROP FUNCTION IF EXISTS public.motte_maintenance_guard() RESTRICT",
    "DROP TABLE IF EXISTS motte_gc_tombstones",
    "DROP INDEX IF EXISTS motte_import_mappings_import_idx",
    "DROP TABLE IF EXISTS motte_import_mappings",
    "DROP TABLE IF EXISTS motte_imports",
    "DROP TABLE IF EXISTS motte_meta",
    "DROP TABLE IF EXISTS motte_request_keys",
)

#: 保存审计证据、降级时必须为空的表（完整字面量，表名不拼接）。
_COUNT_SQL: dict[str, str] = {
    "motte_imports": "SELECT count(*) FROM motte_imports",
    "motte_import_mappings": "SELECT count(*) FROM motte_import_mappings",
    "motte_gc_tombstones": "SELECT count(*) FROM motte_gc_tombstones",
}


def _maintenance_active(bind: Any) -> bool:
    return bool(inspect(bind).has_table("motte_meta") and bind.execute(text(
        "SELECT 1 FROM motte_meta WHERE meta_key = 'maintenance' AND meta_value = 'active'"
    )).scalar())


def downgrade_blockers(bind: Any) -> list[str]:
    inspector = inspect(bind)
    blockers: list[str] = []
    if _maintenance_active(bind):
        blockers.append("maintenance is active; release its owner before downgrading")
    for table, count_sql in _COUNT_SQL.items():
        if inspector.has_table(table):
            rows = int(bind.execute(text(count_sql)).scalar() or 0)
            if rows:
                blockers.append(table + " holds " + str(rows) + " row(s)")
    return blockers


def upgrade() -> None:
    op.execute(
        "CREATE TABLE motte_request_keys (request_key TEXT PRIMARY KEY,"
        " canonical_hash TEXT NOT NULL, run_id TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    op.execute(
        "CREATE TABLE motte_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)"
    )
    op.execute(
        "CREATE TABLE motte_imports (position BIGINT GENERATED ALWAYS AS IDENTITY,"
        " import_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL,"
        " status TEXT NOT NULL, payload JSONB NOT NULL)"
    )
    op.execute(
        "CREATE TABLE motte_import_mappings (position BIGINT GENERATED ALWAYS AS IDENTITY,"
        " mapping_key TEXT PRIMARY KEY, import_id TEXT NOT NULL,"
        " target_type TEXT NOT NULL DEFAULT '', target_id TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL, payload JSONB NOT NULL)"
    )
    op.execute(
        "CREATE INDEX motte_import_mappings_import_idx ON motte_import_mappings (import_id)"
    )
    op.execute(
        "CREATE TABLE motte_gc_tombstones (position BIGINT GENERATED ALWAYS AS IDENTITY,"
        " gc_run_id TEXT NOT NULL, artifact_id TEXT NOT NULL, payload JSONB NOT NULL,"
        " PRIMARY KEY (gc_run_id, artifact_id))"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # A repeatable snapshot may predate an audit writer's commit even after
        # its table lock drains. Refuse rather than count invisible evidence.
        if bind.get_isolation_level() != "READ COMMITTED":
            raise RuntimeError(
                "refusing to downgrade 0015_m7_platform_tables: READ COMMITTED "
                "isolation is required to preserve concurrently committed audit evidence"
            )
        # Match maintenance activation/release serialization. Hold until the
        # migration transaction commits so a new activation cannot race teardown.
        bind.execute(text("SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))"))
        # Refuse before table locks: an earlier queued writer can make even a
        # compatible SHARE request wait for maintenance's SHARE locks. Holding
        # maintenance-meta during that wait would prevent the owner releasing.
        if _maintenance_active(bind):
            raise RuntimeError(
                "refusing to downgrade 0015_m7_platform_tables: maintenance is active; "
                "release its owner before downgrading"
            )
        # Audit writers do not take the metadata advisory lock. Drain their
        # transactions before counting, then prevent new DML through DROP/commit.
        bind.execute(text(
            "LOCK TABLE motte_imports, motte_import_mappings, motte_gc_tombstones IN SHARE MODE"
        ))
    blockers = downgrade_blockers(bind)
    if blockers:
        raise RuntimeError(
            "refusing to downgrade 0015_m7_platform_tables: "
            + "; ".join(blockers)
            + "; import audit / GC tombstone evidence must be preserved; delete"
            " explicitly if you really want to discard it"
        )
    for statement in DOWN_STATEMENTS:
        op.execute(statement)
