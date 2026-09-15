"""存储后端选择：API 与 Worker 只依赖这里，不感知具体实现。

- MOTTE_STORAGE=sqlite（默认，本地开发）：MOTTE_DB_PATH，默认 var/runs.db
- MOTTE_STORAGE=postgres（生产）：MOTTE_PG_DSN 或 DATABASE_URL（兼容 postgresql+asyncpg 前缀）
"""
from __future__ import annotations

import os
from pathlib import Path

from .postgres import create_postgres_run_store
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
