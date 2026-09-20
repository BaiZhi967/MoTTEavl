"""PostgreSQL Run 存储（psycopg 3）。

与 SQLiteRunStore 同构：runs / case_runs / trace_events / scores 四个实体
repository，同一套幂等语义：(run_id, seq) 与 (run_id, case_id) 唯一。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from psycopg import connect
from psycopg.errors import UniqueViolation
from psycopg.types.json import Json

from .integrity import RunConflictError, new_run, next_run, stored_run, validate_event, validate_scores
from .migrations import upgrade as upgrade_migrations
from .run_store import INTERRUPTED_STATES, RunStore


class UnsupportedStorageError(RuntimeError):
    """Raised when PostgreSQL storage cannot be used in the current install."""


def normalize_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    scheme = parsed.scheme.split("+", 1)[0]
    if scheme not in {"postgres", "postgresql"}:
        raise ValueError("PostgreSQL DSN must use postgres:// or postgresql://")
    if not parsed.hostname:
        raise ValueError("PostgreSQL DSN must include a hostname")
    return dsn.replace(f"{parsed.scheme}://", "postgresql://", 1)


def _connect(dsn: str):
    try:
        return connect(dsn)
    except ImportError as error:  # pragma: no cover - 依赖缺失防御
        raise UnsupportedStorageError(
            "PostgreSQL storage requires the 'psycopg' package"
        ) from error


def _append_event(cursor, event: dict[str, Any]) -> dict[str, Any]:
    run_id = event["run_id"]
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
    cursor.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM trace_events WHERE run_id = %s", (run_id,))
    stored = {**deepcopy(event), "seq": cursor.fetchone()[0]}
    cursor.execute(
        "INSERT INTO trace_events(run_id, seq, payload) VALUES (%s, %s, %s)",
        (run_id, stored["seq"], Json(stored)),
    )
    return stored


class _PgRuns:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def create(self, run: dict[str, Any], *, event: dict[str, Any] | None = None) -> dict[str, Any]:
        stored = new_run(run)
        pending = validate_event(event, stored["id"])
        try:
            with _connect(self._dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO runs(id, payload, revision) VALUES (%s, %s, %s)",
                        (stored["id"], Json(stored), 1),
                    )
                    if pending is not None:
                        _append_event(cursor, pending)
        except UniqueViolation as error:
            raise RunConflictError(f"run already exists: {stored['id']}") from error
        return deepcopy(stored)

    def update(
        self, run: dict[str, Any], *, expected_revision: int,
        expected_status: str | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = validate_event(event, run["id"])
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload, revision FROM runs WHERE id = %s FOR UPDATE", (run["id"],))
                row = cursor.fetchone()
                if row is None or row[1] != expected_revision:
                    raise RunConflictError(f"run revision changed: {run['id']}")
                previous = stored_run(row[0], row[1])
                if expected_status is not None and previous.get("status") != expected_status:
                    raise RunConflictError(f"run status changed: {run['id']}")
                if expected_status is None and previous.get("status") != run.get("status"):
                    raise ValueError("state changes require expected_status; use transition")
                stored = next_run(run, expected_revision)
                cursor.execute(
                    "UPDATE runs SET payload = %s, revision = %s WHERE id = %s AND revision = %s",
                    (Json(stored), stored["revision"], run["id"], expected_revision),
                )
                if cursor.rowcount != 1:
                    raise RunConflictError(f"run revision changed: {run['id']}")
                if pending is not None:
                    _append_event(cursor, pending)
        return deepcopy(stored)

    def transition(
        self, run_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = validate_event(event, run_id)
        changes = deepcopy(changes or {})
        if {"id", "revision", "schema_version", "status"}.intersection(changes):
            raise ValueError("transition changes cannot overwrite identity, revision or status")
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload, revision FROM runs WHERE id = %s FOR UPDATE", (run_id,))
                row = cursor.fetchone()
                if row is None or row[1] != expected_revision:
                    raise RunConflictError(f"run revision changed: {run_id}")
                previous = stored_run(row[0], row[1])
                if previous.get("status") != expected_status:
                    raise RunConflictError(f"run status changed: {run_id}")
                stored = next_run({**previous, **changes, "status": status}, expected_revision)
                cursor.execute(
                    "UPDATE runs SET payload = %s, revision = %s WHERE id = %s AND revision = %s "
                    "AND payload->>'status' = %s",
                    (Json(stored), stored["revision"], run_id, expected_revision, expected_status),
                )
                if cursor.rowcount != 1:
                    raise RunConflictError(f"run revision or status changed: {run_id}")
                if pending is not None:
                    _append_event(cursor, pending)
        return deepcopy(stored)

    def save(self, run: dict[str, Any]) -> dict[str, Any]:
        """Legacy blind upsert; callers must use create/update for integrity."""
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT revision FROM runs WHERE id = %s FOR UPDATE", (run["id"],))
                row = cursor.fetchone()
                stored = stored_run(run, row[0] + 1 if row else 1)
                cursor.execute(
                    "INSERT INTO runs(id, payload, revision) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, revision = EXCLUDED.revision",
                    (stored["id"], Json(stored), stored["revision"]),
                )
        return deepcopy(stored)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload, revision FROM runs WHERE id = %s", (run_id,))
                row = cursor.fetchone()
        return stored_run(row[0], row[1]) if row else None

    def list(self) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload, revision FROM runs ORDER BY position")
                rows = cursor.fetchall()
        return [stored_run(payload, revision) for payload, revision in rows]

    def next_run_id(self) -> str:
        """Legacy numeric allocator; new callers use new_run_id instead."""
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM runs")
                rows = cursor.fetchall()
        numbers = [int(value[4:]) for (value,) in rows if value.startswith("run-") and value[4:].isascii()
                   and value[4:].isdigit()]
        return f"run-{max(numbers, default=0) + 1}"

    def claim(self, run_id: str) -> dict[str, Any] | None:
        """Claim exactly this run and append preparing in the same transaction."""
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE runs SET revision = revision + 1, payload = "
                    "jsonb_set(jsonb_set(jsonb_set(payload, '{status}', to_jsonb('preparing'::text)), "
                    "'{revision}', to_jsonb(revision + 1)), '{schema_version}', to_jsonb(2)) "
                    "WHERE id = %s AND payload->>'status' = 'queued' RETURNING payload, revision",
                    (run_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute("SELECT 1 FROM runs WHERE id = %s", (run_id,))
                    if cursor.fetchone() is None:
                        return None
                    raise RunConflictError(f"run is not queued: {run_id}")
                _append_event(cursor, {"run_id": run_id, "type": "preparing", "status": "preparing"})
                return stored_run(row[0], row[1])

    def claim_next_queued(self) -> dict[str, Any] | None:
        """Claim the first queued run; the caller still needs an execution lock."""
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM runs ORDER BY position")
                for (run_id,) in cursor.fetchall():
                    cursor.execute(
                        "UPDATE runs SET revision = revision + 1, payload = "
                        "jsonb_set(jsonb_set(jsonb_set(payload, '{status}', to_jsonb('preparing'::text)), "
                        "'{revision}', to_jsonb(revision + 1)), '{schema_version}', to_jsonb(2)) "
                        "WHERE id = %s AND payload->>'status' = 'queued' RETURNING payload, revision",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        _append_event(cursor, {
                            "run_id": run_id, "type": "preparing", "status": "preparing"
                        })
                        return stored_run(row[0], row[1])
        return None

    def requeue_interrupted(self) -> list[str]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE runs SET revision = revision + 1, payload = "
                    "jsonb_set(jsonb_set(jsonb_set(payload, '{status}', to_jsonb('queued'::text)), "
                    "'{revision}', to_jsonb(revision + 1)), '{schema_version}', to_jsonb(2)) "
                    "WHERE payload->>'status' = ANY(%s) RETURNING id",
                    (list(INTERRUPTED_STATES),),
                )
                return [row[0] for row in cursor.fetchall()]


class _PgCaseRuns:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def upsert(self, case_run: dict[str, Any]) -> dict[str, Any]:
        """按 (run_id, case_id) 幂等写入；冲突时返回已存在的行。"""
        run_id = case_run["run_id"]
        case_id = case_run["case_id"]
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) FROM case_runs WHERE run_id = %s", (run_id,)
                )
                ordinal = cursor.fetchone()[0] + 1
                cursor.execute(
                    "INSERT INTO case_runs(run_id, case_id, ordinal, payload) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (run_id, case_id) DO NOTHING",
                    (run_id, case_id, ordinal, Json(case_run)),
                )
                cursor.execute(
                    "SELECT payload FROM case_runs WHERE run_id = %s AND case_id = %s", (run_id, case_id)
                )
                return deepcopy(cursor.fetchone()[0])

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM case_runs WHERE run_id = %s ORDER BY ordinal", (run_id,)
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def get(self, run_id: str, case_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM case_runs WHERE run_id = %s AND case_id = %s", (run_id, case_id)
                )
                row = cursor.fetchone()
        return deepcopy(row[0]) if row else None


class _PgTraceEvents:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Allocate monotonic per-run sequence numbers inside a transaction."""
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                return _append_event(cursor, event)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM trace_events WHERE run_id = %s ORDER BY seq", (run_id,))
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def list_after(self, run_id: str, seq: int) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM trace_events WHERE run_id = %s AND seq > %s ORDER BY seq",
                    (run_id, seq),
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]


class _PgScores:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def replace_for_run(self, run_id: str, scores: list[dict[str, Any]]) -> None:
        scores = validate_scores(scores)
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM scores WHERE run_id = %s", (run_id,))
                cursor.executemany(
                    "INSERT INTO scores(run_id, case_id, ordinal, payload) VALUES (%s, %s, %s, %s)",
                    [
                        (run_id, score["case_id"], index, Json(score))
                        for index, score in enumerate(scores)
                    ],
                )

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM scores WHERE run_id = %s ORDER BY ordinal", (run_id,))
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]


@dataclass
class PostgresRunStore(RunStore):
    dsn: str = ""


def create_postgres_run_store(dsn: str, *, migrate: bool = False) -> PostgresRunStore:
    """Construct a PostgreSQL store; run Alembic first when migrate=True."""
    from .pg_audit_store import (
        PgAttempts,
        PgBaselines,
        PgBenchmarkDatasets,
        PgCommands,
        PgExternalJobs,
        PgInvocations,
        PgScoreSets,
        PgScoringPasses,
        PgTrials,
    )

    normalized = normalize_dsn(dsn)
    if migrate:
        upgrade_migrations(normalized)
    return PostgresRunStore(
        dsn=normalized,
        runs=_PgRuns(normalized),
        case_runs=_PgCaseRuns(normalized),
        events=_PgTraceEvents(normalized),
        scores=_PgScores(normalized),
        attempts=PgAttempts(normalized),
        scoring_passes=PgScoringPasses(normalized),
        score_sets=PgScoreSets(normalized),
        commands=PgCommands(normalized),
        invocations=PgInvocations(normalized),
        external_jobs=PgExternalJobs(normalized),
        benchmark_datasets=PgBenchmarkDatasets(normalized),
        baselines=PgBaselines(normalized),
        trials=PgTrials(normalized),
    )
