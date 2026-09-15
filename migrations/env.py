"""Alembic migration environment。

DSN 读取顺序：MOTTE_PG_DSN > DATABASE_URL > alembic.ini 的 sqlalchemy.url；
统一归一化为 postgresql+psycopg://（同步驱动，psycopg 3）。
"""
from __future__ import annotations

import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _database_url() -> str:
    from motte_storage.postgres import normalize_dsn

    dsn = (
        os.environ.get("MOTTE_PG_DSN")
        or os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )
    if not dsn:
        raise SystemExit("缺少 DSN：设置 MOTTE_PG_DSN 或 DATABASE_URL，或在 alembic.ini 配置 sqlalchemy.url")
    return normalize_dsn(dsn).replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
