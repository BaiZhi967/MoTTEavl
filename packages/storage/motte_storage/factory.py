"""存储后端选择：API 与 Worker 只依赖这里，不感知具体实现。

- MOTTE_STORAGE=sqlite（默认，本地开发）：MOTTE_DB_PATH，默认 var/runs.db
- MOTTE_STORAGE=postgres（生产）：MOTTE_PG_DSN 或 DATABASE_URL（兼容 postgresql+asyncpg 前缀）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .postgres import create_postgres_run_store, normalize_dsn
from .resource_store import PostgresResourceStore, ResourceStore, SQLiteResourceStore
from .run_store import RunStore, SQLiteRunStore

SUPPORTED_BACKENDS = ("sqlite", "postgres")


def create_run_store(
    db_path: str | Path | None = None,
    *,
    storage: str | None = None,
    dsn: str | None = None,
    migrate: bool = False,
) -> RunStore:
    backend = storage or os.environ.get("MOTTE_STORAGE", "sqlite")
    if backend == "sqlite":
        path = Path(db_path if db_path is not None else os.environ.get("MOTTE_DB_PATH", "var/runs.db"))
        return SQLiteRunStore(path)
    if backend == "postgres":
        resolved = dsn or os.environ.get("MOTTE_PG_DSN") or os.environ.get("DATABASE_URL")
        if not resolved:
            raise ValueError("postgres storage requires MOTTE_PG_DSN or DATABASE_URL")
        return create_postgres_run_store(resolved, migrate=migrate)
    raise ValueError(f"unsupported storage backend: {backend!r} (expected one of {SUPPORTED_BACKENDS})")


def default_content_store() -> Any | None:
    """Skill 资源字节的持久内容寻址存储（M5-R5）。

    发布资源型 Skill 必须能在落库前按内容地址读回字节，因此资源仓库需要
    一个内容存储。API 与 Worker 必须指向**同一个**目录
    （MOTTE_SKILL_CONTENT_ROOT，默认 var/skill-content），否则发布期核验过的
    字节在 Worker 侧读不到。motte-skill 不可用时返回 None：此时带资源清单的
    Skill 发布会具名拒绝，而不是放行无字节的版本。
    """
    try:
        from motte_skill.content_store import create_content_store
    except ImportError:  # pragma: no cover - 未安装 skill 运行时时保持旧行为
        return None
    return create_content_store()


def create_resource_store(
    db_path: str | Path | None = None,
    *,
    storage: str | None = None,
    dsn: str | None = None,
    content_store: Any | None = None,
) -> ResourceStore:
    """版本化资源（providers/models/datasets/scenarios/price_tables）的后端选择。

    content_store 缺省取 default_content_store()；测试可显式注入内存实现。
    """
    if content_store is None:
        content_store = default_content_store()
    backend = storage or os.environ.get("MOTTE_STORAGE", "sqlite")
    if backend == "sqlite":
        path = Path(db_path if db_path is not None else os.environ.get("MOTTE_DB_PATH", "var/runs.db"))
        return SQLiteResourceStore(path, content_store=content_store)
    if backend == "postgres":
        resolved = dsn or os.environ.get("MOTTE_PG_DSN") or os.environ.get("DATABASE_URL")
        if not resolved:
            raise ValueError("postgres storage requires MOTTE_PG_DSN or DATABASE_URL")
        return PostgresResourceStore(normalize_dsn(resolved), content_store=content_store)
    raise ValueError(f"unsupported storage backend: {backend!r} (expected one of {SUPPORTED_BACKENDS})")
