import importlib.util
from pathlib import Path

import pytest

from motte_storage.factory import SUPPORTED_BACKENDS, create_run_store
from motte_storage.migrations import MIGRATIONS_DIR, alembic_config, revision_ids
from motte_storage.postgres import UnsupportedStorageError, normalize_dsn
from motte_storage.run_store import RunStore

VERSION_FILE = MIGRATIONS_DIR / "versions" / "0001_initial.py"


def _load_version_module():
    spec = importlib.util.spec_from_file_location("v0001", VERSION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dsn_is_validated_and_normalized():
    assert normalize_dsn("postgres://h:5432/db") == "postgresql://h:5432/db"
    assert normalize_dsn("postgresql+asyncpg://motte@localhost/db") == "postgresql://motte@localhost/db"
    with pytest.raises(ValueError, match="postgres"):
        normalize_dsn("sqlite:///runs.db")
    with pytest.raises(ValueError, match="hostname"):
        normalize_dsn("postgresql:///db")


def test_alembic_revision_chain_is_linear_and_complete():
    assert revision_ids() == ["0001_initial"]
    config = alembic_config("postgresql://user@localhost/db")
    assert config.get_main_option("script_location") == str(MIGRATIONS_DIR)
    assert "postgresql+psycopg://" in config.get_main_option("sqlalchemy.url")


def test_initial_migration_covers_all_entities_with_downgrade():
    module = _load_version_module()
    assert callable(module.upgrade) and callable(module.downgrade)
    sql = "\n".join(module.UP_STATEMENTS)
    for table in (
        "provider_connections",
        "model_profiles",
        "price_tables",
        "dataset_versions",
        "scenario_versions",
        "runs",
        "case_runs",
        "trace_events",
        "scores",
        "artifacts",
    ):
        assert f"CREATE TABLE {table}" in sql
        assert f"DROP TABLE IF EXISTS {table}" in "\n".join(module.DOWN_STATEMENTS)
    assert "PRIMARY KEY (run_id, seq)" in sql
    assert "PRIMARY KEY (run_id, case_id)" in sql
    assert Path(VERSION_FILE).exists()


def test_factory_selects_backend(monkeypatch, tmp_path):
    store = create_run_store(tmp_path / "runs.db", storage="sqlite")
    assert isinstance(store, RunStore) and not hasattr(store, "dsn")

    monkeypatch.setenv("MOTTE_DB_PATH", str(tmp_path / "env.db"))
    assert isinstance(create_run_store(), RunStore) and not hasattr(create_run_store(), "dsn")

    monkeypatch.setenv("MOTTE_STORAGE", "postgres")
    monkeypatch.setenv("MOTTE_PG_DSN", "postgresql+asyncpg://u@localhost/motte")
    with pytest.raises(Exception, match="psycopg|connect|hostname|Operational|DNS"):
        create_run_store(migrate=True)  # 本地无 PG 服务时失败路径明确
    monkeypatch.delenv("MOTTE_PG_DSN")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="MOTTE_PG_DSN"):
        create_run_store()


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="unsupported storage backend"):
        create_run_store(storage="mysql")
    assert SUPPORTED_BACKENDS == ("sqlite", "postgres")
