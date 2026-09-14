import pytest

from motte_storage.migrations import INITIAL_SCHEMA, migration_sql
from motte_storage.postgres import PostgresRepository, UnsupportedStorageError, create_postgres_repository


def test_postgres_dsn_is_validated_before_driver_lookup():
    with pytest.raises(ValueError, match="PostgreSQL DSN"):
        create_postgres_repository("sqlite:///runs.db")


def test_postgres_boundary_fails_clearly_when_not_enabled():
    with pytest.raises(UnsupportedStorageError, match="PostgreSQL storage|foundation"):
        PostgresRepository("postgresql://localhost/motte")


def test_initial_migration_is_exposed_as_versioned_sql():
    assert migration_sql() == (INITIAL_SCHEMA,)
    assert "CREATE TABLE IF NOT EXISTS records" in INITIAL_SCHEMA
    assert "JSONB" in INITIAL_SCHEMA
