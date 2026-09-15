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
from psycopg.types.json import Json

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


class _PgRuns:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def save(self, run: dict[str, Any]) -> dict[str, Any]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO runs(id, payload) VALUES (%s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload",
                    (run["id"], Json(run)),
                )
        return deepcopy(run)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM runs WHERE id = %s", (run_id,))
                row = cursor.fetchone()
        return deepcopy(row[0]) if row else None

    def list(self) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM runs ORDER BY position")
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def next_run_id(self) -> str:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM runs")
                rows = cursor.fetchall()
        numbers = [int(row[0].split("-")[-1]) for row in rows if row[0].startswith("run-")]
        return f"run-{max(numbers, default=0) + 1}"

    def claim_next_queued(self) -> dict[str, Any] | None:
        """乐观原子抢占：UPDATE ... WHERE status='queued'，竞争者 rowcount=0。"""
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM runs ORDER BY position")
                candidates = [row[0] for row in cursor.fetchall()]
                for run_id in candidates:
                    cursor.execute(
                        "UPDATE runs SET payload = jsonb_set(payload, '{status}', to_jsonb('preparing'::text)) "
                        "WHERE id = %s AND payload->>'status' = 'queued'",
                        (run_id,),
                    )
                    if cursor.rowcount == 1:
                        cursor.execute("SELECT payload FROM runs WHERE id = %s", (run_id,))
                        return deepcopy(cursor.fetchone()[0])
        return None

    def requeue_interrupted(self) -> list[str]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE runs SET payload = jsonb_set(payload, '{status}', to_jsonb('queued'::text)) "
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
        """run 级 advisory lock 内分配单调 seq（写入列与 payload），保证 (run_id, seq) 唯一。"""
        run_id = event["run_id"]
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
                cursor.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM trace_events WHERE run_id = %s", (run_id,)
                )
                seq = cursor.fetchone()[0] + 1
                stored = {**deepcopy(event), "seq": seq}
                cursor.execute(
                    "INSERT INTO trace_events(run_id, seq, payload) VALUES (%s, %s, %s)",
                    (run_id, seq, Json(stored)),
                )
                return deepcopy(stored)

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
    dsn: str


def create_postgres_run_store(dsn: str, *, migrate: bool = False) -> PostgresRunStore:
    """构造 PG RunStore；migrate=True 时先执行 alembic upgrade（空库一步到位）。"""
    normalized = normalize_dsn(dsn)
    if migrate:
        upgrade_migrations(normalized)
    return PostgresRunStore(
        dsn=normalized,
        runs=_PgRuns(normalized),
        case_runs=_PgCaseRuns(normalized),
        events=_PgTraceEvents(normalized),
        scores=_PgScores(normalized),
    )
