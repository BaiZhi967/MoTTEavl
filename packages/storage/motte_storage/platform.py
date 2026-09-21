"""M7 平台级存储：幂等请求注册表、元数据 KV、导入账本与 GC tombstone。

所有表都由 run_store 的 SQLite DDL 与 Alembic 0015（PG）创建；本模块只提供
三后端（SQLite / memory / PostgreSQL）仓库与 `platform_for(store)` 挂载工厂，
模式与 scoring_jobs_for 相同：按 store.dsn / store.runs._path 判定后端。

语义（协议 docs/protocols/sdk-and-migration.md frozen@1）：

- RequestRegistry.bind 是创建幂等的**唯一权威**：同 key 同 canonical hash →
  replayed（返回原 run_id）；同 key 不同 hash → conflict；新 key → created。
  事务内完成检查与写入，跨进程持久。
- MetaKV 维护 maintenance / restored_from_backup 等平台标志。
- ImportLedger 以 mapping_key 为主键做幂等映射账本（M7-T06..T08 的 checkpoint）。
- GCTombstones 记录 GC 删除审计（hash/bytes/reason/时间）。
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from contextlib import closing
from threading import RLock
from typing import Any

from .sqlite_schema import create_and_upgrade

PLATFORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS motte_request_keys (
  request_key TEXT PRIMARY KEY,
  canonical_hash TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS motte_meta (
  meta_key TEXT PRIMARY KEY,
  meta_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS motte_imports (
  import_id TEXT PRIMARY KEY,
  manifest_sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS motte_import_mappings (
  mapping_key TEXT PRIMARY KEY,
  import_id TEXT NOT NULL,
  target_type TEXT NOT NULL DEFAULT '',
  target_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS motte_import_mappings_import_idx ON motte_import_mappings(import_id);
CREATE TABLE IF NOT EXISTS motte_gc_tombstones (
  gc_run_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (gc_run_id, artifact_id)
);
"""


class RequestConflict(ValueError):
    """同 idempotency key 绑定了不同 canonical hash。"""

    code = "REQUEST_KEY_CONFLICT"


class MappingConflict(ValueError):
    """同 mapping_key 对应不同来源内容。"""

    code = "MAPPING_CONFLICT"


class ImportConflict(ValueError):
    """同 import_id 对应不同 manifest hash。"""

    code = "IMPORT_CONFLICT"


# --------------------------------------------------------------------- sqlite


class _SQLiteRequestRegistry:
    def __init__(self, path: str) -> None:
        self._path = path

    def bind(self, request_key: str, canonical_hash: str, run_id: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT canonical_hash, run_id FROM motte_request_keys WHERE request_key = ?",
                (request_key,),
            ).fetchone()
            if row is not None:
                connection.execute("COMMIT")
                if row[0] == canonical_hash:
                    return {"outcome": "replayed", "run_id": row[1], "canonical_hash": row[0]}
                raise RequestConflict(
                    f"request key bound to a different canonical hash: {request_key}"
                )
            connection.execute(
                "INSERT INTO motte_request_keys(request_key, canonical_hash, run_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (request_key, canonical_hash, run_id, _utc_now()),
            )
            connection.execute("COMMIT")
        return {"outcome": "created", "run_id": run_id, "canonical_hash": canonical_hash}

    def get(self, request_key: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            row = connection.execute(
                "SELECT canonical_hash, run_id, created_at FROM motte_request_keys"
                " WHERE request_key = ?",
                (request_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_key": request_key, "canonical_hash": row[0],
            "run_id": row[1], "created_at": row[2],
        }


class _SQLiteMetaKV:
    def __init__(self, path: str) -> None:
        self._path = path

    def get(self, key: str) -> str | None:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            row = connection.execute(
                "SELECT meta_value FROM motte_meta WHERE meta_key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO motte_meta(meta_key, meta_value) VALUES (?, ?)"
                " ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value",
                (key, value),
            )
            connection.execute("COMMIT")

    def delete(self, key: str) -> None:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("DELETE FROM motte_meta WHERE meta_key = ?", (key,))


class _SQLiteImportLedger:
    def __init__(self, path: str) -> None:
        self._path = path

    def begin_import(self, record: dict[str, Any]) -> dict[str, Any]:
        import_id = record["import_id"]
        manifest_sha256 = record["manifest_sha256"]
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT manifest_sha256, payload FROM motte_imports WHERE import_id = ?",
                (import_id,),
            ).fetchone()
            if row is not None:
                connection.execute("COMMIT")
                if row[0] != manifest_sha256:
                    raise ImportConflict(
                        f"import id bound to a different manifest hash: {import_id}"
                    )
                return json.loads(row[1])
            payload = json.dumps(record, sort_keys=True)
            connection.execute(
                "INSERT INTO motte_imports(import_id, manifest_sha256, status, payload)"
                " VALUES (?, ?, ?, ?)",
                (import_id, manifest_sha256, record.get("status", "planned"), payload),
            )
            connection.execute("COMMIT")
        return deepcopy(record)

    def get_import(self, import_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            row = connection.execute(
                "SELECT payload FROM motte_imports WHERE import_id = ?", (import_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_imports(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            rows = connection.execute(
                "SELECT payload FROM motte_imports ORDER BY rowid"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def set_status(self, import_id: str, status: str) -> None:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM motte_imports WHERE import_id = ?", (import_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"import not found: {import_id}")
            record = json.loads(row[0])
            record["status"] = status
            connection.execute(
                "UPDATE motte_imports SET status = ?, payload = ? WHERE import_id = ?",
                (status, json.dumps(record, sort_keys=True), import_id),
            )
            connection.execute("COMMIT")

    def put_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        mapping_key = mapping["mapping_key"]
        source_hash = mapping.get("source", {}).get("content_hash", "")
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM motte_import_mappings WHERE mapping_key = ?",
                (mapping_key,),
            ).fetchone()
            if row is not None:
                connection.execute("COMMIT")
                existing = json.loads(row[0])
                if existing.get("source", {}).get("content_hash", "") == source_hash:
                    return existing
                raise MappingConflict(f"mapping key bound to different source content: {mapping_key}")
            connection.execute(
                "INSERT INTO motte_import_mappings(mapping_key, import_id, target_type,"
                " target_id, status, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    mapping_key, mapping.get("import_id", ""), mapping.get("target_type", ""),
                    mapping.get("target_id", ""), mapping.get("status", "planned"),
                    json.dumps(mapping, sort_keys=True),
                ),
            )
            connection.execute("COMMIT")
        return deepcopy(mapping)

    def get_mapping(self, mapping_key: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            row = connection.execute(
                "SELECT payload FROM motte_import_mappings WHERE mapping_key = ?", (mapping_key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def mappings_for(self, import_id: str) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            rows = connection.execute(
                "SELECT payload FROM motte_import_mappings WHERE import_id = ? ORDER BY rowid",
                (import_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


class _SQLiteGCTombstones:
    def __init__(self, path: str) -> None:
        self._path = path

    def append(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT OR REPLACE INTO motte_gc_tombstones(gc_run_id, artifact_id, payload)"
                " VALUES (?, ?, ?)",
                [
                    (entry["gc_run_id"], entry["artifact_id"], json.dumps(entry, sort_keys=True))
                    for entry in entries
                ],
            )
            connection.execute("COMMIT")

    def list(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10.0)) as connection:
            rows = connection.execute(
                "SELECT payload FROM motte_gc_tombstones ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


# --------------------------------------------------------------------- memory


class _MemoryRequestRegistry:
    def __init__(self, lock: RLock) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = lock

    def bind(self, request_key: str, canonical_hash: str, run_id: str) -> dict[str, Any]:
        with self._lock:
            existing = self._rows.get(request_key)
            if existing is not None:
                if existing["canonical_hash"] == canonical_hash:
                    return {"outcome": "replayed", **deepcopy(existing)}
                raise RequestConflict(
                    f"request key bound to a different canonical hash: {request_key}"
                )
            record = {
                "request_key": request_key, "canonical_hash": canonical_hash,
                "run_id": run_id, "created_at": _utc_now(),
            }
            self._rows[request_key] = record
            return {"outcome": "created", **deepcopy(record)}

    def get(self, request_key: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._rows.get(request_key)
            return deepcopy(record) if record else None


class _MemoryMetaKV:
    def __init__(self, lock: RLock) -> None:
        self._rows: dict[str, str] = {}
        self._lock = lock

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._rows.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._rows[key] = value

    def delete(self, key: str) -> None:
        with self._lock:
            self._rows.pop(key, None)


class _MemoryImportLedger:
    def __init__(self, lock: RLock) -> None:
        self._imports: dict[str, dict[str, Any]] = {}
        self._mappings: dict[str, dict[str, Any]] = {}
        self._lock = lock

    def begin_import(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            existing = self._imports.get(record["import_id"])
            if existing is not None:
                if existing["manifest_sha256"] != record["manifest_sha256"]:
                    raise ImportConflict(
                        f"import id bound to a different manifest hash: {record['import_id']}"
                    )
                return deepcopy(existing)
            stored = deepcopy(record)
            self._imports[stored["import_id"]] = stored
            return deepcopy(stored)

    def get_import(self, import_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._imports.get(import_id)
            return deepcopy(record) if record else None

    def list_imports(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(list(self._imports.values()))

    def set_status(self, import_id: str, status: str) -> None:
        with self._lock:
            if import_id not in self._imports:
                raise KeyError(f"import not found: {import_id}")
            self._imports[import_id]["status"] = status

    def put_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            existing = self._mappings.get(mapping["mapping_key"])
            if existing is not None:
                if existing.get("source", {}).get("content_hash", "") == mapping.get(
                    "source", {}
                ).get("content_hash", ""):
                    return deepcopy(existing)
                raise MappingConflict(
                    f"mapping key bound to different source content: {mapping['mapping_key']}"
                )
            stored = deepcopy(mapping)
            self._mappings[stored["mapping_key"]] = stored
            return deepcopy(stored)

    def get_mapping(self, mapping_key: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._mappings.get(mapping_key)
            return deepcopy(record) if record else None

    def mappings_for(self, import_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(
                [m for m in self._mappings.values() if m.get("import_id") == import_id]
            )


class _MemoryGCTombstones:
    def __init__(self, lock: RLock) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = lock

    def append(self, entries: list[dict[str, Any]]) -> None:
        with self._lock:
            for entry in entries:
                self._rows[(entry["gc_run_id"], entry["artifact_id"])] = deepcopy(entry)

    def list(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(list(self._rows.values())[:limit])


# ------------------------------------------------------------------ postgres


class _PgRequestRegistry:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def bind(self, request_key: str, canonical_hash: str, run_id: str) -> dict[str, Any]:
        from psycopg import errors as pg_errors

        with closing(_pg_connect(self._dsn)) as connection:
            try:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT canonical_hash, run_id FROM motte_request_keys"
                            " WHERE request_key = %s",
                            (request_key,),
                        )
                        row = cursor.fetchone()
                        if row is not None:
                            if row[0] == canonical_hash:
                                return {
                                    "outcome": "replayed", "run_id": row[1],
                                    "canonical_hash": row[0],
                                }
                            raise RequestConflict(
                                f"request key bound to a different canonical hash: {request_key}"
                            )
                        cursor.execute(
                            "INSERT INTO motte_request_keys(request_key, canonical_hash, run_id,"
                            " created_at) VALUES (%s, %s, %s, %s)",
                            (request_key, canonical_hash, run_id, _utc_now()),
                        )
            except pg_errors.UniqueViolation:
                # 并发同 key 插入：读回已有绑定按 hash 分类
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT canonical_hash, run_id FROM motte_request_keys"
                        " WHERE request_key = %s",
                        (request_key,),
                    )
                    row = cursor.fetchone()
                if row and row[0] == canonical_hash:
                    return {"outcome": "replayed", "run_id": row[1], "canonical_hash": row[0]}
                raise RequestConflict(
                    f"request key bound to a different canonical hash: {request_key}"
                ) from None
        return {"outcome": "created", "run_id": run_id, "canonical_hash": canonical_hash}

    def get(self, request_key: str) -> dict[str, Any] | None:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT canonical_hash, run_id, created_at FROM motte_request_keys"
                " WHERE request_key = %s",
                (request_key,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "request_key": request_key, "canonical_hash": row[0],
            "run_id": row[1], "created_at": row[2],
        }


class _PgMetaKV:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def get(self, key: str) -> str | None:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT meta_value FROM motte_meta WHERE meta_key = %s", (key,))
            row = cursor.fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO motte_meta(meta_key, meta_value) VALUES (%s, %s)"
                " ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value",
                (key, value),
            )
            connection.commit()

    def delete(self, key: str) -> None:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM motte_meta WHERE meta_key = %s", (key,))
            connection.commit()


class _PgImportLedger:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def begin_import(self, record: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Json

        with closing(_pg_connect(self._dsn)) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT manifest_sha256, payload FROM motte_imports WHERE import_id = %s",
                    (record["import_id"],),
                )
                row = cursor.fetchone()
                if row is not None:
                    if row[0] != record["manifest_sha256"]:
                        raise ImportConflict(
                            f"import id bound to a different manifest hash: {record['import_id']}"
                        )
                    return row[1]
                cursor.execute(
                    "INSERT INTO motte_imports(import_id, manifest_sha256, status, payload)"
                    " VALUES (%s, %s, %s, %s)",
                    (
                        record["import_id"], record["manifest_sha256"],
                        record.get("status", "planned"), Json(record),
                    ),
                )
                connection.commit()
        return deepcopy(record)

    def get_import(self, import_id: str) -> dict[str, Any] | None:
        from psycopg.types.json import Json  # noqa: F401  （保持导入风格一致）

        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM motte_imports WHERE import_id = %s", (import_id,))
            row = cursor.fetchone()
        return row[0] if row else None

    def list_imports(self) -> list[dict[str, Any]]:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM motte_imports ORDER BY position")
            return [row[0] for row in cursor.fetchall()]

    def set_status(self, import_id: str, status: str) -> None:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM motte_imports WHERE import_id = %s", (import_id,)
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"import not found: {import_id}")
            record = row[0]
            record["status"] = status
            from psycopg.types.json import Json

            cursor.execute(
                "UPDATE motte_imports SET status = %s, payload = %s WHERE import_id = %s",
                (status, Json(record), import_id),
            )
            connection.commit()

    def put_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Json

        mapping_key = mapping["mapping_key"]
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM motte_import_mappings WHERE mapping_key = %s", (mapping_key,)
            )
            row = cursor.fetchone()
            if row is not None:
                existing = row[0]
                if existing.get("source", {}).get("content_hash", "") == mapping.get(
                    "source", {}
                ).get("content_hash", ""):
                    return existing
                raise MappingConflict(
                    f"mapping key bound to different source content: {mapping_key}"
                )
            cursor.execute(
                "INSERT INTO motte_import_mappings(mapping_key, import_id, target_type,"
                " target_id, status, payload) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    mapping_key, mapping.get("import_id", ""), mapping.get("target_type", ""),
                    mapping.get("target_id", ""), mapping.get("status", "planned"),
                    Json(mapping),
                ),
            )
            connection.commit()
        return deepcopy(mapping)

    def get_mapping(self, mapping_key: str) -> dict[str, Any] | None:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM motte_import_mappings WHERE mapping_key = %s", (mapping_key,)
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def mappings_for(self, import_id: str) -> list[dict[str, Any]]:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM motte_import_mappings WHERE import_id = %s"
                " ORDER BY position",
                (import_id,),
            )
            return [row[0] for row in cursor.fetchall()]


class _PgGCTombstones:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def append(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        from psycopg.types.json import Json

        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            for entry in entries:
                cursor.execute(
                    "INSERT INTO motte_gc_tombstones(gc_run_id, artifact_id, payload)"
                    " VALUES (%s, %s, %s) ON CONFLICT(gc_run_id, artifact_id)"
                    " DO UPDATE SET payload = excluded.payload",
                    (entry["gc_run_id"], entry["artifact_id"], Json(entry)),
                )
            connection.commit()

    def list(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with closing(_pg_connect(self._dsn)) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM motte_gc_tombstones ORDER BY position DESC LIMIT %s",
                (limit,),
            )
            return [row[0] for row in cursor.fetchall()]


def _pg_connect(dsn: str):
    from psycopg import connect

    return connect(dsn)


# ------------------------------------------------------------------- factory


class PlatformStores:
    """一次挂载全部平台仓库；字段与 RunStore 其他组件同风格。"""

    def __init__(self, requests: Any, meta: Any, imports: Any, tombstones: Any) -> None:
        self.requests = requests
        self.meta = meta
        self.imports = imports
        self.tombstones = tombstones


def ensure_sqlite_platform_tables(path: str) -> None:
    """在既有 SQLite 库上补齐平台表（幂等）。"""
    import sqlite3 as _sqlite3

    with closing(_sqlite3.connect(path, isolation_level=None, timeout=10.0)) as connection:
        create_and_upgrade(connection, PLATFORM_SCHEMA)


def platform_for(store: Any) -> PlatformStores:
    """把平台仓库挂到既有 RunStore（SQLite / memory / PostgreSQL）。"""
    dsn = getattr(store, "dsn", None)
    if dsn:
        return PlatformStores(
            requests=_PgRequestRegistry(dsn),
            meta=_PgMetaKV(dsn),
            imports=_PgImportLedger(dsn),
            tombstones=_PgGCTombstones(dsn),
        )
    path = getattr(getattr(store, "runs", None), "_path", None)
    if path:
        path = str(path)
        ensure_sqlite_platform_tables(path)
        return PlatformStores(
            requests=_SQLiteRequestRegistry(path),
            meta=_SQLiteMetaKV(path),
            imports=_SQLiteImportLedger(path),
            tombstones=_SQLiteGCTombstones(path),
        )
    if all(hasattr(store, name) for name in ("runs", "events")):
        lock = RLock()
        return PlatformStores(
            requests=_MemoryRequestRegistry(lock),
            meta=_MemoryMetaKV(lock),
            imports=_MemoryImportLedger(lock),
            tombstones=_MemoryGCTombstones(lock),
        )
    raise ValueError("unsupported storage backend for platform stores")


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
