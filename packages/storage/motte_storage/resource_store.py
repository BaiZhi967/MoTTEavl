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


def _validate_client_record(record: dict[str, Any]) -> None:
    if "_deleted" in record:
        raise ValueError("_deleted is reserved for repository tombstones")


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


def _is_tombstone(record: dict[str, Any] | None) -> bool:
    return bool(record and record.get("_deleted") is True)


def _visible(record: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if _is_tombstone(record) else record


def _check_mutation(
    table: str, existing: dict[str, Any] | None, record: dict[str, Any],
    expected_generation: int | None,
) -> bool:
    if expected_generation is not None:
        if expected_generation == 0:
            if existing is not None and not _is_tombstone(existing):
                raise ResourceConflictError("resource already exists")
            required_generation = (
                int(existing.get("generation", 1)) + 1 if _is_tombstone(existing) else 1
            )
            if _is_tombstone(existing) and record.get("generation", 1) == 1:
                record["generation"] = required_generation
            if record.get("generation", 1) != required_generation:
                raise ResourceConflictError("resource generation must advance monotonically")
        elif existing is None or _is_tombstone(existing) or existing.get(
            "generation", 1
        ) != expected_generation:
            raise ResourceConflictError("resource generation changed")
        elif record.get("generation") != expected_generation + 1:
            raise ResourceConflictError("resource generation must advance monotonically")
    if table != "model_profiles" or existing is None:
        return False
    lifecycle = existing.get("lifecycle", "draft")
    if lifecycle == "deprecated":
        if existing != record:
            raise ResourceConflictError("deprecated model profiles are immutable")
        return True
    if lifecycle != "published":
        return False
    if record.get("lifecycle") != "deprecated":
        if existing != record:
            raise ResourceConflictError("published model profiles are immutable")
        return True
    ignored = {"generation", "lifecycle", "deprecated_at"}
    before = {key: value for key, value in existing.items() if key not in ignored}
    after = {key: value for key, value in record.items() if key not in ignored}
    if before != after or not record.get("deprecated_at"):
        raise ResourceConflictError("published model profiles may only be deprecated")
    return False


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

    def put(
        self, record: dict[str, Any], *, expected_generation: int | None = None
    ) -> dict[str, Any]:
        record = deepcopy(record)
        _validate_client_record(record)
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
            if _check_mutation(self._table, existing_record, record, expected_generation):
                return deepcopy(existing_record)
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
        return _visible(json.loads(row[0])) if row else None

    def list(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT payload FROM {self._table} ORDER BY {', '.join(self._keys)}"
            ).fetchall()
        return [record for row in rows if (record := _visible(json.loads(row[0]))) is not None]

    def delete(self, *key: str, expected_generation: int | None = None) -> bool:
        where = " AND ".join(f"{field} = ?" for field in self._keys)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT payload FROM {self._table} WHERE {where}", list(key)).fetchone()
            current = json.loads(row[0]) if row else None
            if expected_generation is not None and current is not None and current.get(
                "generation", 1
            ) != expected_generation:
                raise ResourceConflictError("resource generation changed")
            if current is None or _is_tombstone(current):
                return False
            _check_delete(self._table, current)
            if self._table in VERSIONED_TABLES:
                return False
            tombstone = {**current, "_deleted": True}
            connection.execute(
                f"UPDATE {self._table} SET payload = ? WHERE {where}",
                [json.dumps(tombstone, sort_keys=True), *list(key)],
            )
            return True


class _PgResourceRepository:
    def __init__(self, dsn: str, table: str, key_fields: tuple[str, ...]) -> None:
        self._dsn = dsn
        self._table = table
        self._keys = key_fields

    def _connect(self):
        from psycopg import connect

        return connect(self._dsn)

    def put(
        self, record: dict[str, Any], *, expected_generation: int | None = None
    ) -> dict[str, Any]:
        from psycopg.types.json import Json

        record = deepcopy(record)
        _validate_client_record(record)
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
                where = " AND ".join(f"{field} = %s" for field in self._keys)
                if expected_generation == 0:
                    cursor.execute(
                        f"INSERT INTO {self._table}({fields}) VALUES ({placeholders}) "
                        f"ON CONFLICT ({', '.join(self._keys)}) DO NOTHING RETURNING payload",
                        [*values, Json(record)],
                    )
                    inserted = cursor.fetchone()
                    if inserted:
                        _check_mutation(self._table, None, record, expected_generation)
                        return deepcopy(inserted[0])
                    cursor.execute(
                        f"SELECT payload FROM {self._table} WHERE {where} FOR UPDATE", values
                    )
                    existing = cursor.fetchone()
                    existing_record = existing[0] if existing else None
                    if _is_tombstone(existing_record):
                        _check_mutation(self._table, existing_record, record, expected_generation)
                        cursor.execute(
                            f"UPDATE {self._table} SET payload = %s WHERE {where}",
                            [Json(record), *values],
                        )
                        return deepcopy(record)
                    raise ResourceConflictError("resource already exists")
                where = " AND ".join(f"{field} = %s" for field in self._keys)
                cursor.execute(
                    f"SELECT payload FROM {self._table} WHERE {where} FOR UPDATE", values
                )
                existing = cursor.fetchone()
                existing_record = existing[0] if existing else None
                if _check_mutation(
                    self._table, existing_record, record, expected_generation
                ):
                    return deepcopy(existing_record)
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
        return deepcopy(_visible(row[0])) if row and _visible(row[0]) is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT payload FROM {self._table} ORDER BY {', '.join(self._keys)}"
                )
                rows = cursor.fetchall()
        return [
            deepcopy(record) for row in rows
            if (record := _visible(row[0])) is not None
        ]

    def delete(self, *key: str, expected_generation: int | None = None) -> bool:
        where = " AND ".join(f"{field} = %s" for field in self._keys)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT payload FROM {self._table} WHERE {where} FOR UPDATE", list(key)
                )
                row = cursor.fetchone()
                current = row[0] if row else None
                if expected_generation is not None and current is not None and current.get(
                    "generation", 1
                ) != expected_generation:
                    raise ResourceConflictError("resource generation changed")
                if current is None or _is_tombstone(current):
                    return False
                _check_delete(self._table, current)
                if self._table in VERSIONED_TABLES:
                    return False
                from psycopg.types.json import Json

                tombstone = {**current, "_deleted": True}
                cursor.execute(
                    f"UPDATE {self._table} SET payload = %s WHERE {where}",
                    [Json(tombstone), *list(key)],
                )
                return cursor.rowcount > 0


class _InMemoryResourceRepository:
    def __init__(self, key_fields: tuple[str, ...], table: str) -> None:
        self._keys = key_fields
        self._table = table
        self._rows: dict[tuple[str, ...], dict[str, Any]] = {}
        self._lock = RLock()

    def put(
        self, record: dict[str, Any], *, expected_generation: int | None = None
    ) -> dict[str, Any]:
        record = deepcopy(record)
        _validate_client_record(record)
        _validate_managed(self._table, record)
        key = tuple(str(record[field]) for field in self._keys)
        with self._lock:
            existing = self._rows.get(key)
            if _check_version(self._table, existing, record):
                return deepcopy(existing)
            if _check_mutation(self._table, existing, record, expected_generation):
                return deepcopy(existing)
            self._rows[key] = deepcopy(record)
            return deepcopy(record)

    def get(self, *key: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(_visible(self._rows.get(tuple(str(value) for value in key))))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                row for _, row in sorted(self._rows.items()) if not _is_tombstone(row)
            ])

    def delete(self, *key: str, expected_generation: int | None = None) -> bool:
        lookup = tuple(str(value) for value in key)
        with self._lock:
            current = self._rows.get(lookup)
            if expected_generation is not None and current is not None and current.get(
                "generation", 1
            ) != expected_generation:
                raise ResourceConflictError("resource generation changed")
            if current is None or _is_tombstone(current):
                return False
            _check_delete(self._table, current)
            if self._table in VERSIONED_TABLES:
                return False
            self._rows[lookup] = {**current, "_deleted": True}
            return True


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
