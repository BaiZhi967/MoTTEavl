"""Resource repositories with immutable dataset, scenario and price versions.

Providers and model drafts retain mutable put semantics. PostgreSQL tables are
created by Alembic; local SQLite tables are initialized on demand.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

# 资源名 → (表名, 键字段)；与 0001_initial/0009_runtime_resources 迁移保持一致。
RESOURCE_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "providers": ("provider_connections", ("name",)),
    "models": ("model_profiles", ("id",)),
    "price_tables": ("price_tables", ("model_id", "version")),
    "datasets": ("dataset_versions", ("name", "version")),
    "scenarios": ("scenario_versions", ("name", "version")),
    "publications": ("resource_publications", ("id",)),
    "runtimes": ("runtime_versions", ("name", "version")),
    "runtime_profiles": ("runtime_profiles", ("name", "version")),
}


class UnknownResourceError(ValueError):
    pass


class ResourceConflictError(ValueError):
    """A resource version already exists with different content or cannot be deleted."""


VERSIONED_TABLES = frozenset({
    "price_tables", "dataset_versions", "scenario_versions", "resource_publications",
    "runtime_versions", "runtime_profiles",
})


def _validate_client_record(record: dict[str, Any]) -> None:
    if "_deleted" in record:
        raise ValueError("_deleted is reserved for repository tombstones")


_PUBLICATION_FIELDS = frozenset({
    "id", "dataset", "scenario", "dataset_fingerprint", "receipt", "receipt_sha256",
    "actor", "entrypoint", "published_at",
})
_PUBLICATION_ID = re.compile(r"publication-[0-9a-f]{64}")
_CANONICAL_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_RECEIPT_HASH = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_RECEIPT_FIELDS = frozenset({"artifact_paths", "body", "secret", "secrets"})


def _forbidden_receipt_path(value: Any, path: str = "receipt") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_RECEIPT_FIELDS:
                return f"{path}.{key}"
            nested = _forbidden_receipt_path(item, f"{path}.{key}")
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested = _forbidden_receipt_path(item, f"{path}[{index}]")
            if nested is not None:
                return nested
    return None


def _canonical_publication_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("publication audit published_at must be non-empty")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValueError("publication audit published_at must be timezone-aware ISO") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("publication audit published_at must be timezone-aware ISO")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _publication_identity(record: dict[str, Any]) -> str:
    from motte_contracts.identity import canonical_sha256

    identity = canonical_sha256({
        "dataset": record["dataset"],
        "scenario": record["scenario"],
        "dataset_fingerprint": record["dataset_fingerprint"],
        "receipt_sha256": f"sha256:{record['receipt_sha256']}",
        "actor": record["actor"],
        "entrypoint": record["entrypoint"],
    })
    return f"publication-{identity.removeprefix('sha256:')}"


def _validate_publication(record: dict[str, Any]) -> None:
    if set(record) != _PUBLICATION_FIELDS:
        missing = sorted(_PUBLICATION_FIELDS - record.keys())
        extra = sorted(record.keys() - _PUBLICATION_FIELDS)
        raise ValueError(f"publication audit fields are not canonical: missing={missing}, extra={extra}")
    for field in ("dataset", "scenario"):
        value = record[field]
        if not isinstance(value, str) or value != value.strip():
            raise ValueError(f"publication audit {field} must be canonical")
        name, separator, version = value.rpartition("@")
        if not separator or not name or not version:
            raise ValueError(f"publication audit {field} must be a name@version reference")
    fingerprint = record["dataset_fingerprint"]
    if not isinstance(fingerprint, str) or _CANONICAL_FINGERPRINT.fullmatch(fingerprint) is None:
        raise ValueError("publication audit dataset_fingerprint must be a canonical sha256")
    receipt_sha256 = record["receipt_sha256"]
    if not isinstance(receipt_sha256, str) or _RECEIPT_HASH.fullmatch(receipt_sha256) is None:
        raise ValueError("publication audit receipt_sha256 must be lowercase hexadecimal")
    receipt = record["receipt"]
    if not isinstance(receipt, dict):
        raise ValueError("publication audit receipt must be a portable object")
    forbidden_path = _forbidden_receipt_path(receipt)
    if forbidden_path is not None:
        raise ValueError(
            f"publication audit receipt contains non-portable field: {forbidden_path}"
        )
    if receipt.get("dataset_fingerprint") not in (None, fingerprint):
        raise ValueError("publication audit receipt fingerprint does not match the dataset")
    try:
        json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("publication audit receipt must contain canonical JSON values") from error
    from motte_contracts.identity import canonical_sha256

    if canonical_sha256(receipt).removeprefix("sha256:") != receipt_sha256:
        raise ValueError("publication audit receipt_sha256 does not match receipt")
    for field in ("actor", "entrypoint"):
        value = record[field]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"publication audit {field} must be canonical")
    if record["published_at"] != _canonical_publication_time(record["published_at"]):
        raise ValueError("publication audit published_at must be canonical UTC RFC3339")
    publication_id = record["id"]
    if not isinstance(publication_id, str) or _PUBLICATION_ID.fullmatch(publication_id) is None:
        raise ValueError("publication audit id must be a canonical publication identity")
    if publication_id != _publication_identity(record):
        raise ValueError("publication audit id does not match its immutable fields")


def _validate_managed(table: str, record: dict[str, Any]) -> None:
    """Keep managed suite schema checks independent of version immutability."""
    if table == "resource_publications":
        _validate_publication(record)
        return
    if table == "runtime_versions":
        from motte_contracts.runtime import RuntimeVersion

        RuntimeVersion.model_validate(record)
        return
    if table == "runtime_profiles":
        from motte_contracts.runtime import RuntimeProfileVersion

        RuntimeProfileVersion.model_validate(record)
        return
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


PairPublisher = Callable[
    [dict[str, Any], dict[str, Any], dict[str, Any] | None],
    tuple[dict[str, Any], dict[str, Any]],
]


def _validate_bundle_relationship(
    dataset: dict[str, Any], scenario: dict[str, Any], *, audited: bool,
) -> None:
    from motte_contracts.suites import suite_of

    dataset_suite = suite_of(dataset)
    scenario_suite = suite_of(scenario)
    managed = dataset_suite is not None or scenario_suite is not None
    if managed and dataset_suite != scenario_suite:
        raise ValueError("dataset and scenario suites do not match")
    dataset_ref = f"{dataset['name']}@{dataset['version']}"
    if managed or audited or "dataset" in scenario:
        if scenario.get("dataset") != dataset_ref:
            raise ValueError("scenario dataset reference does not match the bundled dataset")
    if not managed:
        return
    if dataset_suite == "direct-llm":
        if dataset.get("eval") != scenario.get("eval"):
            raise ValueError("dataset and scenario eval contracts do not match")
        eval_spec = dataset.get("eval") or {}
        eval_version = eval_spec.get("version")
        contract_version = dataset.get("contract_version")
        plugin_version = scenario.get("plugin_version")
        if eval_version == 2:
            if contract_version != 2 or plugin_version != "2":
                raise ValueError("direct-llm v2 contract and plugin versions do not match")
        elif contract_version not in (None, 1) or plugin_version not in (None, "1"):
            raise ValueError("direct-llm v1 contract and plugin versions do not match")
    elif dataset_suite == "gsm8k" and dataset.get("benchmark") != scenario.get("benchmark"):
        raise ValueError("dataset and scenario benchmark contracts do not match")


def _validated_bundle(
    dataset: dict[str, Any], scenario: dict[str, Any], publication: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    dataset = deepcopy(dataset)
    scenario = deepcopy(scenario)
    publication = deepcopy(publication)
    _validate_client_record(dataset)
    _validate_client_record(scenario)
    _validate_managed("dataset_versions", dataset)
    _validate_managed("scenario_versions", scenario)
    _validate_bundle_relationship(dataset, scenario, audited=publication is not None)
    if publication is not None:
        _validate_client_record(publication)
        _validate_managed("resource_publications", publication)
        dataset_ref = f"{dataset['name']}@{dataset['version']}"
        scenario_ref = f"{scenario['name']}@{scenario['version']}"
        if publication["dataset"] != dataset_ref or publication["scenario"] != scenario_ref:
            raise ValueError("publication audit resource references do not match the bundle")
        if publication["dataset_fingerprint"] != dataset.get("dataset_fingerprint"):
            raise ValueError("publication audit fingerprint does not match the dataset")
    return dataset, scenario, publication


def _sqlite_pair_publisher(path: str) -> PairPublisher:
    def publish(
        dataset: dict[str, Any], scenario: dict[str, Any],
        publication: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        dataset, scenario, publication = _validated_bundle(dataset, scenario, publication)
        resources = [
            ("dataset_versions", ("name", "version"), dataset),
            ("scenario_versions", ("name", "version"), scenario),
        ]
        if publication is not None:
            resources.append(("resource_publications", ("id",), publication))
        with closing(sqlite3.connect(path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                missing: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []
                for table, keys, record in resources:
                    values = [str(record[key]) for key in keys]
                    where = " AND ".join(f"{key} = ?" for key in keys)
                    existing = connection.execute(
                        f"SELECT payload FROM {table} WHERE {where}", values
                    ).fetchone()
                    existing_record = json.loads(existing[0]) if existing else None
                    if not _check_version(table, existing_record, record):
                        missing.append((table, keys, record))
                for table, keys, record in missing:
                    fields = ", ".join([*keys, "payload"])
                    placeholders = ", ".join("?" for _ in range(len(keys) + 1))
                    connection.execute(
                        f"INSERT INTO {table}({fields}) VALUES ({placeholders})",
                        [*[str(record[key]) for key in keys], json.dumps(record, sort_keys=True)],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return deepcopy(dataset), deepcopy(scenario)

    return publish


def _postgres_pair_publisher(dsn: str) -> PairPublisher:
    def publish(
        dataset: dict[str, Any], scenario: dict[str, Any],
        publication: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from psycopg import connect
        from psycopg.types.json import Json

        dataset, scenario, publication = _validated_bundle(dataset, scenario, publication)
        resources = [
            ("dataset_versions", ("name", "version"), dataset),
            ("scenario_versions", ("name", "version"), scenario),
        ]
        if publication is not None:
            resources.append(("resource_publications", ("id",), publication))
        with connect(dsn) as connection:
            with connection.cursor() as cursor:
                for table, keys, record in resources:
                    values = [str(record[key]) for key in keys]
                    fields = ", ".join([*keys, "payload"])
                    placeholders = ", ".join(["%s"] * (len(keys) + 1))
                    conflict_keys = ", ".join(keys)
                    cursor.execute(
                        f"INSERT INTO {table}({fields}) VALUES ({placeholders}) "
                        f"ON CONFLICT ({conflict_keys}) DO NOTHING RETURNING payload",
                        [*values, Json(record)],
                    )
                    if cursor.fetchone() is not None:
                        continue
                    where = " AND ".join(f"{key} = %s" for key in keys)
                    cursor.execute(
                        f"SELECT payload FROM {table} WHERE {where} FOR UPDATE", values,
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        raise ResourceConflictError("resource version vanished while publishing")
                    _check_version(table, existing[0], record)
        return deepcopy(dataset), deepcopy(scenario)

    return publish


def _memory_pair_publisher(
    datasets: _InMemoryResourceRepository, scenarios: _InMemoryResourceRepository,
    publications: _InMemoryResourceRepository,
) -> PairPublisher:
    def publish(
        dataset: dict[str, Any], scenario: dict[str, Any],
        publication: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        dataset, scenario, publication = _validated_bundle(dataset, scenario, publication)
        resources = [(datasets, dataset), (scenarios, scenario)]
        if publication is not None:
            resources.append((publications, publication))
        with datasets._lock, scenarios._lock, publications._lock:
            missing: list[tuple[_InMemoryResourceRepository, tuple[str, ...], dict[str, Any]]] = []
            for repository, record in resources:
                key = tuple(str(record[field]) for field in repository._keys)
                existing = repository._rows.get(key)
                if not _check_version(repository._table, existing, record):
                    missing.append((repository, key, record))
            for repository, key, record in missing:
                repository._rows[key] = deepcopy(record)
        return deepcopy(dataset), deepcopy(scenario)

    return publish


@dataclass
class ResourceStore:
    providers: Any
    models: Any
    price_tables: Any
    datasets: Any
    scenarios: Any
    publications: Any
    runtimes: Any
    runtime_profiles: Any
    _pair_publisher: PairPublisher

    def publish_dataset_scenario(
        self, dataset: dict[str, Any], scenario: dict[str, Any], *,
        publication: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Publish an immutable dataset, scenario, and optional audit in one transaction."""
        return self._pair_publisher(dataset, scenario, publication)


def _build(builder, publisher_builder) -> ResourceStore:
    providers = builder(*RESOURCE_TABLES["providers"])
    models = builder(*RESOURCE_TABLES["models"])
    price_tables = builder(*RESOURCE_TABLES["price_tables"])
    datasets = builder(*RESOURCE_TABLES["datasets"])
    scenarios = builder(*RESOURCE_TABLES["scenarios"])
    publications = builder(*RESOURCE_TABLES["publications"])
    runtimes = builder(*RESOURCE_TABLES["runtimes"])
    runtime_profiles = builder(*RESOURCE_TABLES["runtime_profiles"])
    return ResourceStore(
        providers=providers,
        models=models,
        price_tables=price_tables,
        datasets=datasets,
        scenarios=scenarios,
        publications=publications,
        runtimes=runtimes,
        runtime_profiles=runtime_profiles,
        _pair_publisher=publisher_builder(datasets, scenarios, publications),
    )


def SQLiteResourceStore(path: str | Path) -> ResourceStore:
    path = str(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    def builder(table: str, keys: tuple[str, ...]):
        return _SQLiteResourceRepository(path, table, keys)

    return _build(
        builder, lambda _datasets, _scenarios, _publications: _sqlite_pair_publisher(path)
    )


def PostgresResourceStore(dsn: str) -> ResourceStore:
    def builder(table: str, keys: tuple[str, ...]):
        return _PgResourceRepository(dsn, table, keys)

    return _build(
        builder, lambda _datasets, _scenarios, _publications: _postgres_pair_publisher(dsn)
    )


def InMemoryResourceStore() -> ResourceStore:
    def builder(table: str, keys: tuple[str, ...]):
        return _InMemoryResourceRepository(keys, table)

    return _build(builder, _memory_pair_publisher)
