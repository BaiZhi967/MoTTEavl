"""版本化 PostgreSQL 迁移管理。

版本文件位于仓库 `migrations/versions/`（0001_initial 等），每个版本声明
revision / down_revision / up / down。runner 在 schema_migrations 表中记录
已应用版本，按依赖顺序执行，支持按步回退（阶段 6 rollback 的基础）。
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_DIR = REPO_ROOT / "migrations" / "versions"


@dataclass(frozen=True)
class Revision:
    revision: str
    down_revision: str | None
    up: tuple[str, ...]
    down: tuple[str, ...]


def _load_revisions() -> list[Revision]:
    if not VERSIONS_DIR.is_dir():
        raise FileNotFoundError(f"migration versions directory not found: {VERSIONS_DIR}")
    revisions: list[Revision] = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revisions.append(
            Revision(
                revision=module.revision,
                down_revision=module.down_revision,
                up=tuple(module.up),
                down=tuple(module.down),
            )
        )
    return _order(revisions)


def _order(revisions: list[Revision]) -> list[Revision]:
    by_id = {item.revision: item for item in revisions}
    if len(by_id) != len(revisions):
        raise ValueError("duplicate migration revisions detected")
    root = [item for item in revisions if item.down_revision is None]
    if len(root) != 1:
        raise ValueError("exactly one root migration is required")
    ordered: list[Revision] = []
    current: Revision | None = root[0]
    seen: set[str] = set()
    while current is not None:
        if current.revision in seen:
            raise ValueError(f"migration cycle detected at {current.revision}")
        seen.add(current.revision)
        ordered.append(current)
        children = [item for item in revisions if item.down_revision == current.revision]
        if len(children) > 1:
            raise ValueError(f"branching migrations are not supported at {current.revision}")
        current = children[0] if children else None
    if len(ordered) != len(revisions):
        missing = set(by_id) - seen
        raise ValueError(f"unreachable migrations: {sorted(missing)}")
    return ordered


def revisions() -> list[Revision]:
    return _load_revisions()


def _applied(connection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "revision TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cursor.execute("SELECT revision FROM schema_migrations")
        return {row[0] for row in cursor.fetchall()}


def apply_migrations(connection) -> list[str]:
    """应用未执行的迁移，返回本次应用的 revision 列表（幂等）。"""
    applied = _applied(connection)
    freshly: list[str] = []
    with connection.cursor() as cursor:
        for item in _load_revisions():
            if item.revision in applied:
                continue
            for statement in item.up:
                cursor.execute(statement)
            cursor.execute(
                "INSERT INTO schema_migrations(revision) VALUES (%s)", (item.revision,)
            )
            freshly.append(item.revision)
    connection.commit()
    return freshly


def revert_last_migration(connection) -> str | None:
    """回退最近应用的一个迁移，返回其 revision；无可回退时返回 None。"""
    applied = _applied(connection)
    if not applied:
        return None
    chain = _load_revisions()
    latest = next(item for item in reversed(chain) if item.revision in applied)
    with connection.cursor() as cursor:
        for statement in latest.down:
            cursor.execute(statement)
        cursor.execute("DELETE FROM schema_migrations WHERE revision = %s", (latest.revision,))
    connection.commit()
    return latest.revision


def migration_manifest() -> str:
    """导出迁移清单（供运维记录与兼容性检查）。"""
    return json.dumps(
        [
            {
                "revision": item.revision,
                "down_revision": item.down_revision,
                "statements": len(item.up),
            }
            for item in _load_revisions()
        ],
        ensure_ascii=False,
        indent=2,
    )


def migration_sql() -> tuple[tuple[str, ...], ...]:
    """按顺序返回全部 up SQL（外部 runner 兼容入口）。"""
    return tuple(item.up for item in _load_revisions())
