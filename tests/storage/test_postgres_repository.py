import pytest

from motte_storage.factory import SUPPORTED_BACKENDS, create_run_store
from motte_storage.migrations import migration_manifest, migration_sql, revisions
from motte_storage.postgres import UnsupportedStorageError, normalize_dsn
from motte_storage.run_store import RunStore


def test_dsn_is_validated_and_normalized():
    assert normalize_dsn("postgres://h:5432/db") == "postgresql://h:5432/db"
    assert normalize_dsn("postgresql+asyncpg://motte@localhost/db") == "postgresql://motte@localhost/db"
    with pytest.raises(ValueError, match="postgres"):
        normalize_dsn("sqlite:///runs.db")
    with pytest.raises(ValueError, match="hostname"):
        normalize_dsn("postgresql:///db")


def test_migration_registry_is_an_ordered_linear_chain():
    chain = revisions()
    assert chain[0].down_revision is None
    assert chain[0].revision == "0001_initial"
    for parent, child in zip(chain, chain[1:]):
        assert child.down_revision == parent.revision
        assert child.up and child.down, "每个迁移必须同时声明 up 与 down"


def test_initial_migration_covers_all_entities():
    sql = "\n".join(migration_sql()[0])
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
    assert "PRIMARY KEY (run_id, seq)" in sql
    assert "PRIMARY KEY (run_id, case_id)" in sql
    assert "0001_initial" in migration_manifest()


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
