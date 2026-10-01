"""Insert-only statistical publications with identical, canonical TEXT storage.

Repository construction never creates tables. Run-store startup (SQLite) and
Alembic (PostgreSQL) own the schema, so maintenance sees it before publication.
The PostgreSQL driver is imported only when that backend performs an operation.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from motte_contracts.hashing import canonical_json
from motte_contracts.statistical_reports import statistical_report_id, validate_statistical_report

__all__ = [
    "MemoryStatisticalReports", "SQLiteStatisticalReports", "PgStatisticalReports",
    "StatisticalReportConflict", "StatisticalReportCorrupt", "statistical_publication_guard",
]


class StatisticalReportConflict(ValueError):
    """A caller claimed an identity for different canonical content."""


class StatisticalReportCorrupt(ValueError):
    """Persisted evidence failed validation; it must never be silently repaired."""


# The same detached representation is used in memory and both SQL backends.
_Row = tuple[str, str, str]
# Both SQL engines bind LIMIT as a signed 64-bit integer. Larger Python limits
# are equivalent for a realizable table and must not break backend parity.
_MAX_SQL_LIMIT = 2 ** 63 - 1


def _proposal(report_id: str, body: dict[str, Any]) -> _Row:
    if statistical_report_id(body) != report_id:
        raise StatisticalReportConflict("statistical report ID does not match canonical body")
    envelope = validate_statistical_report({
        "report_id": report_id,
        "published_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "body": body,
    })
    return report_id, canonical_json(envelope["body"]), envelope["published_at"]


def _stored(row: _Row, *, expected_id: str | None = None) -> dict[str, Any]:
    try:
        if expected_id is not None and row[0] != expected_id:
            raise ValueError("stored report ID does not match repository lookup key")
        envelope = validate_statistical_report({
            "report_id": row[0], "body": json.loads(row[1]), "published_at": row[2],
        })
        if canonical_json(envelope["body"]) != row[1]:
            raise ValueError("stored body is not canonical JSON text")
        return envelope
    except (ValueError, TypeError, RecursionError) as error:
        raise StatisticalReportCorrupt(f"corrupt statistical report: {row[0]}") from error


def _winner(row: _Row, proposed: _Row) -> dict[str, Any]:
    # Always validate the old row first: an identical replay must not repair it.
    envelope = _stored(row, expected_id=proposed[0])
    if row[:2] != proposed[:2]:
        raise StatisticalReportConflict("statistical reports are immutable: " + proposed[0])
    return envelope


def _limit(value: int | None) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("limit must be a nonnegative integer or None")


class MemoryStatisticalReports:
    def __init__(self, lock: RLock) -> None:
        self._lock = lock
        self._rows: dict[str, _Row] = {}

    def put(self, report_id: str, body: dict[str, Any]) -> dict[str, Any]:
        proposed = _proposal(report_id, body)
        with self._lock:
            row = self._rows.get(report_id)
            if row is None:
                self._rows[report_id] = proposed
                row = proposed
            return _winner(row, proposed)

    def get(self, report_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows.get(report_id)
            return _stored(row, expected_id=report_id) if row is not None else None

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        _limit(limit)
        with self._lock:
            return [_stored(self._rows[key], expected_id=key)
                    for key in sorted(self._rows)[:limit]]


class SQLiteStatisticalReports:
    def __init__(self, path: str) -> None:
        self._path = path

    def put(self, report_id: str, body: dict[str, Any]) -> dict[str, Any]:
        proposed = _proposal(report_id, body)
        with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO statistical_reports(report_id, body, published_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (report_id) DO NOTHING", proposed,
                )
                row = connection.execute(
                    "SELECT report_id, body, published_at FROM statistical_reports WHERE report_id = ?",
                    (report_id,),
                ).fetchone()
                return _winner(row, proposed)

    def get(self, report_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self._path, timeout=10)) as connection:
            row = connection.execute(
                "SELECT report_id, body, published_at FROM statistical_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return _stored(row, expected_id=report_id) if row is not None else None

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        _limit(limit)
        with closing(sqlite3.connect(self._path, timeout=10)) as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT report_id, body, published_at FROM statistical_reports ORDER BY report_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT report_id, body, published_at FROM statistical_reports "
                    "ORDER BY report_id LIMIT ?", (min(limit, _MAX_SQL_LIMIT),),
                ).fetchall()
        return [_stored(row) for row in rows]


class PgStatisticalReports:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def put(self, report_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from .postgres import _connect

        proposed = _proposal(report_id, body)
        with _connect(self._dsn) as connection:
            connection.execute(
                "INSERT INTO statistical_reports(report_id, body, published_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (report_id) DO NOTHING", proposed,
            )
            # At READ COMMITTED this separate statement sees the winner after
            # ON CONFLICT has waited for its transaction to commit.
            row = connection.execute(
                "SELECT report_id, body, published_at FROM statistical_reports WHERE report_id = %s",
                (report_id,),
            ).fetchone()
            return _winner(row, proposed)

    def get(self, report_id: str) -> dict[str, Any] | None:
        from .postgres import _connect

        with _connect(self._dsn) as connection:
            row = connection.execute(
                "SELECT report_id, body, published_at FROM statistical_reports WHERE report_id = %s",
                (report_id,),
            ).fetchone()
        return _stored(row, expected_id=report_id) if row is not None else None

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        from .postgres import _connect

        _limit(limit)
        with _connect(self._dsn) as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT report_id, body, published_at FROM statistical_reports ORDER BY report_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT report_id, body, published_at FROM statistical_reports "
                    "ORDER BY report_id LIMIT %s", (min(limit, _MAX_SQL_LIMIT),),
                ).fetchall()
        return [_stored(row) for row in rows]


@contextmanager
def statistical_publication_guard(store: Any) -> Iterator[None]:
    """Exclude maintenance until the captured evidence has a persistent pin.

    Publishers share the same operation lock that maintenance acquires
    exclusively. PostgreSQL holds a dedicated *session* connection throughout
    calculation and repository insertion, which use their own connections.
    Closing that connection releases its advisory lock even on exceptions.
    An abandoned persisted maintenance flag still refuses publication.
    """
    from .maintenance import maintenance_status
    from .operation_locks import MaintenanceConflict, file_lock

    with ExitStack() as locks:
        dsn = getattr(store, "dsn", None)
        path = getattr(getattr(store, "runs", None), "_path", None)
        if dsn:
            from .postgres import _connect

            connection = _connect(dsn)
            locks.callback(connection.close)
            connection.autocommit = True
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock_shared(hashtext(%s))", ("motteavl:maintenance",),
            ).fetchone()[0]
            if not acquired:
                raise MaintenanceConflict("maintenance is held by another operation")
        elif path:
            locks.enter_context(file_lock(
                str(Path(path).resolve()) + ".maintenance.lock",
                label="statistical publication", shared=True,
            ))
        else:
            # All in-memory Run/Pass/repository access already uses this RLock.
            lock = getattr(getattr(store, "runs", None), "_lock", None)
            if lock is None:
                raise TypeError("unsupported storage backend for statistical publication")
            locks.enter_context(lock)
        if maintenance_status(store)["active"]:
            raise MaintenanceConflict("maintenance mode active")
        yield
