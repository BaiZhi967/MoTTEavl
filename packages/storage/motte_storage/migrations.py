"""Alembic 迁移封装。

CLI：`uv run alembic upgrade head` / `uv run alembic downgrade -1`（DSN 来自
MOTTE_PG_DSN / DATABASE_URL）。这里提供编程入口，供 create_postgres_run_store
与测试使用；版本登记在 alembic_version 表。
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _psycopg_url(dsn: str) -> str:
    from .postgres import normalize_dsn

    return normalize_dsn(dsn).replace("postgresql://", "postgresql+psycopg://", 1)


def alembic_config(dsn: str | None = None) -> Config:
    # Build the config in memory so Windows locale encodings cannot make
    # reading the UTF-8 ``alembic.ini`` fail before migrations even start.
    # The migration script location and DSN are the only settings required by
    # the programmatic API; the CLI continues to read ``alembic.ini`` itself.
    cfg = Config(file_=None)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if dsn is not None:
        cfg.set_main_option("sqlalchemy.url", _psycopg_url(dsn))
    return cfg


def revision_ids() -> list[str]:
    """按应用顺序返回全部 revision（base → head）。"""
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    return [revision.revision for revision in reversed(list(script.walk_revisions()))]


def upgrade(dsn: str) -> str:
    """升级到 head（幂等），返回当前版本。"""
    command.upgrade(alembic_config(dsn), "head")
    return current(dsn)


def downgrade(dsn: str, steps: int = 1) -> str | None:
    """回退指定步数（默认一步），返回回退后的当前版本。"""
    command.downgrade(alembic_config(dsn), f"-{steps}")
    return current(dsn)


def current(dsn: str) -> str | None:
    """读取 alembic_version；未初始化的库返回 None（连接失败正常抛出）。"""
    engine = create_engine(_psycopg_url(dsn))
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
            return rows[0][0] if rows else None
    except ProgrammingError:
        return None
    finally:
        engine.dispose()
