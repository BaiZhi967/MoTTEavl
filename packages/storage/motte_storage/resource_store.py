"""Resource repositories with immutable dataset, scenario and price versions.

Providers and model drafts retain mutable put semantics. PostgreSQL tables are
created by Alembic; local SQLite tables are initialized on demand.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

# 资源名 → (表名, 键字段)；与 0001_initial 迁移保持一致。
RESOURCE_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "providers": ("provider_connections", ("name",)),
    "models": ("model_profiles", ("id",)),
    "price_tables": ("price_tables", ("model_id", "version")),
    "datasets": ("dataset_versions", ("name", "version")),
    "scenarios": ("scenario_versions", ("name", "version")),
}


class UnknownResourceError(ValueError):
    pass


class ResourceConflictError(ValueError):
    """A resource version already exists with different content or cannot be deleted."""


VERSIONED_TABLES = frozenset({"price_tables", "dataset_versions", "scenario_versions"})


def _validate_managed(table: str, record: dict[str, Any]) -> None:
    """Keep managed suite schema checks independent of version immutability."""
    if table not in ("dataset_versions", "scenario_versions"):
        return
    from motte_contracts.suites import is_managed, validate_dataset, validate_scenario

    if is_managed(record):
        (validate_dataset if table == "dataset_versions" else validate_scenario)(record)


def _check_version(table: str, existing: dict[str, Any] | None,
                   record: dict[str, Any]) -> bool:
    if table not in VERSIONED_TABLES or existing is None:
        return False
    if existing != record:
        raise ResourceConflictError("resource version already exists with different content; use a new version")
    return True


def _check_delete(table: str, record: dict[str, Any] | None) -> None:
    if table in VERSIONED_TABLES and record is not None:
        raise ResourceConflictError("resource versions are immutable; use a new version")


class _SQLiteResourceRepository:
    def __init__(self, path: str, table: str, key_fields: tuple[str, ...]) -> None:
        self._path = path
        self._table = table
        self._keys = key_fields
        columns = ", ".join(f"{field} TEXT NOT NULL" for field in key_fields)
        primary = ", ".join(key_fields)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ({columns}, payload TEXT NOT NULL, PRIMARY KEY ({primary}))"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, isolation_level=None, timeout=10.0)

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        _validate_managed(self._table, record)
        values = [str(record[field]) for field in self._keys]
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            where = " AND ".join(f"{field} = ?" for field in self._keys)
            existing = connection.execute(
                f"SELECT payload FROM {self._table} WHERE {where}", values
            ).fetchone()
            existing_record = json.loads(existing[0]) if existing else None
            if _check_version(self._table, existing_record, record):
                return existing_record
            connection.execute(
                f"INSERT INTO {self._table}({', '.join(self._keys)}, payload) "
                f"VALUES ({', '.join('?' for _ in range(len(self._keys) + 1))}) "
                f"ON CONFLICT ({', '.join(self._keys)}) DO UPDATE SET payload = excluded.payload",
                [*values, json.dumps(record, sort_keys=True)],
            )
        return deepcopy(record)

    def get(self, *key: str) -> dict[str, Any] | None:
        where = " AND ".join(f"{field} = ?" for field in self._keys)
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT payload FROM {self._table} WHERE {where}", list(key)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT payload FROM {self._table} ORDER BY {', '.join(self._keys)}"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, *key: str) -> bool:
        where = " AND ".join(f"{field} = ?" for field in self._keys)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT payload FROM {self._table} WHERE {where}", list(key)).fetchone()
            _check_delete(self._table, json.loads(row[0]) if row else None)
            if self._table in VERSIONED_TABLES:
                return False
            cursor = connection.execute(f"DELETE FROM {self._table} WHERE {where}", list(key))
            return cursor.rowcount > 0


class _PgResourceRepository:
    def __init__(self, dsn: str, table: str, key_fields: tuple[str, ...]) -> None:
        self._dsn = dsn
        self._table = table
        self._keys = key_fields

    def _connect(self):
        from psycopg import connect

        return connect(self._dsn)

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Json

        _validate_managed(self._table, record)
        values = [str(record[field]) for field in self._keys]
        fields = ", ".join([*self._keys, "payload"])
        placeholders = ", ".join(["%s"] * (len(self._keys) + 1))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if self._table in VERSIONED_TABLES:
                    cursor.execute(
                        f"INSERT INTO {self._table}({fields}) VALUES ({placeholders}) "
                        f"ON CONFLICT ({', '.join(self._keys)}) DO NOTHING RETURNING payload",
                        [*values, Json(record)],
                    )
                    inserted = cursor.fetchone()
                    if inserted:
                        return deepcopy(inserted[0])
                    where = " AND ".join(f"{field} = %s" for field in self._keys)
                    cursor.execute(f"SELECT payload FROM {self._table} WHERE {where}", values)
                    existing = cursor.fetchone()
                    if existing is None:
                        raise ResourceConflictError("resource version vanished while inserting")
                    _check_version(self._table, existing[0], record)
                    return deepcopy(existing[0])
                cursor.execute(
                    f"INSERT INTO {self._table}({fields}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({', '.join(self._keys)}) DO UPDATE SET payload = EXCLUDED.payload",
                    [*values, Json(record)],
                )
        return deepcopy(record)

    def get(self, *key: str) -> dict[str, Any] | None:
        where = " AND ".join(f"{field} = %s" for field in self._keys)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT payload FROM {self._table} WHERE {where}", list(key))
                row = cursor.fetchone()
        return deepcopy(row[0]) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT payload FROM {self._table} ORDER BY {', '.join(self._keys)}"
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def delete(self, *key: str) -> bool:
        where = " AND ".join(f"{field} = %s" for field in self._keys)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT payload FROM {self._table} WHERE {where}", list(key))
                row = cursor.fetchone()
                _check_delete(self._table, row[0] if row else None)
                if self._table in VERSIONED_TABLES:
                    return False
                cursor.execute(f"DELETE FROM {self._table} WHERE {where}", list(key))
                return cursor.rowcount > 0


class _InMemoryResourceRepository:
    def __init__(self, key_fields: tuple[str, ...], table: str) -> None:
        self._keys = key_fields
        self._table = table
        self._rows: dict[tuple[str, ...], dict[str, Any]] = {}
        self._lock = RLock()

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        _validate_managed(self._table, record)
        key = tuple(str(record[field]) for field in self._keys)
        with self._lock:
            existing = self._rows.get(key)
            if _check_version(self._table, existing, record):
                return deepcopy(existing)
            self._rows[key] = deepcopy(record)
            return deepcopy(record)

    def get(self, *key: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._rows.get(tuple(str(value) for value in key)))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([row for _, row in sorted(self._rows.items())])

    def delete(self, *key: str) -> bool:
        lookup = tuple(str(value) for value in key)
        with self._lock:
            _check_delete(self._table, self._rows.get(lookup))
            return self._rows.pop(lookup, None) is not None


@dataclass
class ResourceStore:
    providers: Any
    models: Any
    price_tables: Any
    datasets: Any
    scenarios: Any


def _build(builder) -> ResourceStore:
    return ResourceStore(
        providers=builder(*RESOURCE_TABLES["providers"]),
        models=builder(*RESOURCE_TABLES["models"]),
        price_tables=builder(*RESOURCE_TABLES["price_tables"]),
        datasets=builder(*RESOURCE_TABLES["datasets"]),
        scenarios=builder(*RESOURCE_TABLES["scenarios"]),
    )


def SQLiteResourceStore(path: str | Path) -> ResourceStore:
    path = str(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    def builder(table: str, keys: tuple[str, ...]):
        return _SQLiteResourceRepository(path, table, keys)

    return _build(builder)


def PostgresResourceStore(dsn: str) -> ResourceStore:
    def builder(table: str, keys: tuple[str, ...]):
        return _PgResourceRepository(dsn, table, keys)

    return _build(builder)


def InMemoryResourceStore() -> ResourceStore:
    def builder(table: str, keys: tuple[str, ...]):
        return _InMemoryResourceRepository(keys, table)

    return _build(builder)
