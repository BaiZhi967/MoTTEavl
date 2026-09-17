"""版本化资源存储：provider_connections / model_profiles / price_tables /
dataset_versions / scenario_versions。

与 RunStore 同一套约定：记录为 JSON payload（内含键字段），键列参与主键，
put 幂等（ON CONFLICT 更新）。PG 表结构见 migrations/versions/0001_initial.py；
SQLite 本地表在此处按需创建。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
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
    """A benchmark resource version cannot be overwritten or deleted."""


def _check_benchmark(table, record, existing=None):
    if table not in ("dataset_versions", "scenario_versions"):
        return
    from motte_contracts.gsm8k import is_benchmark, validate_dataset, validate_scenario

    if existing and (is_benchmark(existing) or is_benchmark(record)) and existing != record:
        raise ResourceConflictError("benchmark version already exists with different content; use a new version")
    if is_benchmark(record):
        (validate_dataset if table == "dataset_versions" else validate_scenario)(record)


def _check_delete(record):
    from motte_contracts.gsm8k import is_benchmark

    if record and is_benchmark(record):
        raise ResourceConflictError("benchmark versions are immutable; use a new version")


class _SQLiteResourceRepository:
    def __init__(self, path: str, table: str, key_fields: tuple[str, ...]) -> None:
        self._path = path
        self._table = table
        self._keys = key_fields
        columns = ", ".join(f"{field} TEXT NOT NULL" for field in key_fields)
        primary = ", ".join(key_fields)
        with closing(self._connect()) as connection:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ({columns}, payload TEXT NOT NULL, PRIMARY KEY ({primary}))"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        values = [str(record[field]) for field in self._keys]
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            where = " AND ".join(f"{field} = ?" for field in self._keys)
            existing = connection.execute(
                f"SELECT payload FROM {self._table} WHERE {where}", values
            ).fetchone()
            _check_benchmark(self._table, record, json.loads(existing[0]) if existing else None)
            connection.execute(
                f"INSERT OR REPLACE INTO {self._table}({', '.join(self._keys)}, payload) "
                f"VALUES ({', '.join('?' for _ in range(len(self._keys) + 1))})",
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
            _check_delete(json.loads(row[0]) if row else None)
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

        values = [str(record[field]) for field in self._keys]
        fields = ", ".join([*self._keys, "payload"])
        placeholders = ", ".join(["%s"] * (len(self._keys) + 1))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                # Serialize version checks including absent rows; no schema migration required.
                cursor.execute(f"LOCK TABLE {self._table} IN SHARE ROW EXCLUSIVE MODE")
                where = " AND ".join(f"{field} = %s" for field in self._keys)
                cursor.execute(f"SELECT payload FROM {self._table} WHERE {where}", values)
                existing = cursor.fetchone()
                _check_benchmark(self._table, record, existing[0] if existing else None)
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
                cursor.execute(f"LOCK TABLE {self._table} IN SHARE ROW EXCLUSIVE MODE")
                cursor.execute(f"SELECT payload FROM {self._table} WHERE {where}", list(key))
                row = cursor.fetchone()
                _check_delete(row[0] if row else None)
                cursor.execute(f"DELETE FROM {self._table} WHERE {where}", list(key))
                return cursor.rowcount > 0


class _InMemoryResourceRepository:
    def __init__(self, key_fields: tuple[str, ...], table: str) -> None:
        self._keys = key_fields
        self._table = table
        self._rows: dict[tuple[str, ...], dict[str, Any]] = {}

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        key = tuple(str(record[field]) for field in self._keys)
        _check_benchmark(self._table, record, self._rows.get(key))
        self._rows[key] = deepcopy(record)
        return deepcopy(record)

    def get(self, *key: str) -> dict[str, Any] | None:
        row = self._rows.get(tuple(str(value) for value in key))
        return deepcopy(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for _, row in sorted(self._rows.items())]

    def delete(self, *key: str) -> bool:
        row = self._rows.get(tuple(str(value) for value in key))
        _check_delete(row)
        return self._rows.pop(tuple(str(value) for value in key), None) is not None


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
