import importlib.util
from pathlib import Path

import pytest

from motte_storage.factory import SUPPORTED_BACKENDS, create_run_store
from motte_storage.migrations import MIGRATIONS_DIR, alembic_config, revision_ids
from motte_storage.postgres import UnsupportedStorageError, normalize_dsn
from motte_storage.run_store import RunStore

VERSION_FILE = MIGRATIONS_DIR / "versions" / "0001_initial.py"
INTEGRITY_VERSION_FILE = MIGRATIONS_DIR / "versions" / "0002_platform_integrity.py"
PUBLICATION_VERSION_FILE = MIGRATIONS_DIR / "versions" / "0003_resource_publications.py"


def _load_version_module(version_file=VERSION_FILE):
    spec = importlib.util.spec_from_file_location(version_file.stem, version_file)
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
    assert revision_ids() == [
        "0001_initial", "0002_platform_integrity", "0003_resource_publications",
        "0004_multi_metric_score_sets", "0005_agent_invocations",
        "0006_external_jobs", "0007_benchmark_datasets", "0008_trials",
        "0009_runtime_resources",
    ]
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


def test_integrity_migration_preserves_legacy_tables_and_adds_audit_entities():
    module = _load_version_module(INTEGRITY_VERSION_FILE)
    assert module.down_revision == "0001_initial"
    sql = "\n".join(module.UP_STATEMENTS)
    drops = "\n".join(module.DOWN_STATEMENTS)
    assert "ALTER TABLE runs ADD COLUMN revision" in sql
    assert "ALTER TABLE runs DROP COLUMN IF EXISTS revision" in drops
    for table in ("case_attempts", "scoring_passes", "score_sets", "run_commands"):
        assert f"CREATE TABLE {table}" in sql
        assert f"DROP TABLE IF EXISTS {table}" in drops
    assert "UNIQUE (run_id, case_id, attempt_no)" in sql
    assert "PRIMARY KEY (scoring_pass_id, case_id)" in sql
    assert "DROP TABLE IF EXISTS scores" not in drops


def test_publication_migration_adds_append_only_resource_audit():
    module = _load_version_module(PUBLICATION_VERSION_FILE)
    assert module.down_revision == "0002_platform_integrity"
    sql = "\n".join(module.UP_STATEMENTS)
    drops = "\n".join(module.DOWN_STATEMENTS)
    assert "CREATE TABLE resource_publications" in sql
    assert "payload JSONB NOT NULL" in sql
    assert "created_at TIMESTAMPTZ NOT NULL" in sql
    assert "DROP TABLE IF EXISTS resource_publications" in drops


def test_multi_metric_migration_extends_score_set_identity():
    module = _load_version_module(
        MIGRATIONS_DIR / "versions" / "0004_multi_metric_score_sets.py"
    )
    assert module.down_revision == "0003_resource_publications"
    sql = "\n".join(module.UP_STATEMENTS)
    for column in ("trial_id", "metric_id", "evaluator_id", "evaluator_version"):
        assert column in sql
    # 复合主键；'' 作为无 trial/metric 的规范化键，避免 NULL 唯一性差异
    assert "PRIMARY KEY (scoring_pass_id, case_id, trial_id, metric_id, evaluator_id, evaluator_version)" in sql
    assert "SELECT scoring_pass_id, case_id, '', '', '', '', ordinal, payload FROM score_sets" in sql
    assert "to_regclass" in "\n".join(module.DOWN_STATEMENTS)


def test_factory_selects_backend(monkeypatch, tmp_path):
    store = create_run_store(tmp_path / "runs.db", storage="sqlite")
    assert isinstance(store, RunStore) and not hasattr(store, "dsn")

    monkeypatch.setenv("MOTTE_DB_PATH", str(tmp_path / "env.db"))
    assert isinstance(create_run_store(), RunStore) and not hasattr(create_run_store(), "dsn")

    monkeypatch.setenv("MOTTE_STORAGE", "postgres")
    monkeypatch.setenv("MOTTE_PG_DSN", "postgresql+asyncpg://u@localhost/motte")

    def unavailable(_dsn):
        raise OSError("psycopg connect unavailable")

    monkeypatch.setattr("motte_storage.postgres.upgrade_migrations", unavailable)
    with pytest.raises(OSError, match="psycopg connect unavailable"):
        create_run_store(migrate=True)
    monkeypatch.delenv("MOTTE_PG_DSN")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="MOTTE_PG_DSN"):
        create_run_store()


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="unsupported storage backend"):
        create_run_store(storage="mysql")
    assert SUPPORTED_BACKENDS == ("sqlite", "postgres")
