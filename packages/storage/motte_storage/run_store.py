"""Run persistence for SQLite and in-memory execution.

New writes use insert-only create and revision/status-checked updates. The legacy
save and scores repositories remain available until their callers migrate.
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from .integrity import RunConflictError, new_run, next_run, stored_run, validate_event, validate_scores

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS case_runs (
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (run_id, case_id)
);
CREATE TABLE IF NOT EXISTS trace_events (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS scores (
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (run_id, case_id)
);
CREATE TABLE IF NOT EXISTS case_attempts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  status TEXT NOT NULL,
  revision INTEGER NOT NULL,
  payload TEXT NOT NULL,
  trial_id TEXT NOT NULL DEFAULT '',
  UNIQUE (run_id, case_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS case_attempts_run_idx ON case_attempts(run_id);
CREATE TABLE IF NOT EXISTS scoring_passes (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scoring_passes_run_idx ON scoring_passes(run_id);
CREATE TABLE IF NOT EXISTS score_sets (
  scoring_pass_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  trial_id TEXT NOT NULL DEFAULT '',
  metric_id TEXT NOT NULL DEFAULT '',
  evaluator_id TEXT NOT NULL DEFAULT '',
  evaluator_version TEXT NOT NULL DEFAULT '',
  ordinal INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (scoring_pass_id, case_id, trial_id, metric_id, evaluator_id, evaluator_version),
  FOREIGN KEY (scoring_pass_id) REFERENCES scoring_passes(id)
);
CREATE TABLE IF NOT EXISTS run_commands (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  status TEXT NOT NULL,
  revision INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_commands_run_idx ON run_commands(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS run_commands_dedupe_idx
ON run_commands(run_id, json_extract(payload, '$.dedupe_key'))
WHERE json_extract(payload, '$.dedupe_key') IS NOT NULL;
CREATE TABLE IF NOT EXISTS runtime_sessions (
  session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL,
  revision INTEGER NOT NULL, payload TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS runtime_sessions_run_idx ON runtime_sessions(run_id);
CREATE TABLE IF NOT EXISTS agent_invocations (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  step INTEGER NOT NULL,
  status TEXT NOT NULL,
  revision INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_invocations_run_idx ON agent_invocations(run_id);
"""

# Worker 崩溃后卡住的中间态；重启时回收回 queued。
INTERRUPTED_STATES = ("preparing", "running", "collecting", "scoring")


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _append_event(connection: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    seq = connection.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM trace_events WHERE run_id = ?", (event["run_id"],)
    ).fetchone()[0]
    stored = {**deepcopy(event), "seq": seq}
    connection.execute(
        "INSERT INTO trace_events(run_id, seq, payload) VALUES (?, ?, ?)",
        (event["run_id"], seq, json.dumps(stored, sort_keys=True)),
    )
    return stored


class _SQLiteRuns:
    def __init__(self, path: str) -> None:
        self._path = path

    def create(self, run: dict[str, Any], *, event: dict[str, Any] | None = None) -> dict[str, Any]:
        stored = new_run(run)
        pending = validate_event(event, stored["id"])
        try:
            with closing(_connect(self._path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO runs(id, payload, revision) VALUES (?, ?, ?)",
                    (stored["id"], json.dumps(stored, sort_keys=True), 1),
                )
                if pending is not None:
                    _append_event(connection, pending)
        except sqlite3.IntegrityError as error:
            raise RunConflictError(f"run already exists: {stored['id']}") from error
        return deepcopy(stored)

    def update(
        self, run: dict[str, Any], *, expected_revision: int,
        expected_status: str | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = validate_event(event, run["id"])
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, revision FROM runs WHERE id = ?", (run["id"],)
            ).fetchone()
            if row is None or row[1] != expected_revision:
                raise RunConflictError(f"run revision changed: {run['id']}")
            previous = stored_run(json.loads(row[0]), row[1])
            if expected_status is not None and previous.get("status") != expected_status:
                raise RunConflictError(f"run status changed: {run['id']}")
            if expected_status is None and previous.get("status") != run.get("status"):
                raise ValueError("state changes require expected_status; use transition")
            stored = next_run(run, expected_revision)
            if connection.execute(
                "UPDATE runs SET payload = ?, revision = ? WHERE id = ? AND revision = ?",
                (json.dumps(stored, sort_keys=True), stored["revision"], run["id"], expected_revision),
            ).rowcount != 1:
                raise RunConflictError(f"run revision changed: {run['id']}")
            if pending is not None:
                _append_event(connection, pending)
        return deepcopy(stored)

    def transition(
        self, run_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = validate_event(event, run_id)
        changes = deepcopy(changes or {})
        if {"id", "revision", "schema_version", "status"}.intersection(changes):
            raise ValueError("transition changes cannot overwrite identity, revision or status")
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, revision FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None or row[1] != expected_revision:
                raise RunConflictError(f"run revision changed: {run_id}")
            previous = stored_run(json.loads(row[0]), row[1])
            if previous.get("status") != expected_status:
                raise RunConflictError(f"run status changed: {run_id}")
            stored = next_run({**previous, **changes, "status": status}, expected_revision)
            if connection.execute(
                "UPDATE runs SET payload = ?, revision = ? WHERE id = ? AND revision = ? "
                "AND json_extract(payload, '$.status') = ?",
                (json.dumps(stored, sort_keys=True), stored["revision"], run_id,
                 expected_revision, expected_status),
            ).rowcount != 1:
                raise RunConflictError(f"run revision or status changed: {run_id}")
            if pending is not None:
                _append_event(connection, pending)
        return deepcopy(stored)

    def save(self, run: dict[str, Any]) -> dict[str, Any]:
        """Legacy blind upsert; only callers of create/update/transition get CAS safety."""
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision FROM runs WHERE id = ?", (run["id"],)).fetchone()
            stored = stored_run(run, row[0] + 1 if row else 1)
            connection.execute(
                "INSERT INTO runs(id, payload, revision) VALUES (?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET payload = excluded.payload, revision = excluded.revision",
                (stored["id"], json.dumps(stored, sort_keys=True), stored["revision"]),
            )
        return deepcopy(stored)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute("SELECT payload, revision FROM runs WHERE id = ?", (run_id,)).fetchone()
        return stored_run(json.loads(row[0]), row[1]) if row else None

    def list(self) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute("SELECT payload, revision FROM runs ORDER BY rowid").fetchall()
        return [stored_run(json.loads(payload), revision) for payload, revision in rows]

    def next_run_id(self) -> str:
        """Legacy numeric ID allocator; new callers use new_run_id instead."""
        with closing(_connect(self._path)) as connection:
            rows = connection.execute("SELECT id FROM runs").fetchall()
        numbers = [int(value[4:]) for (value,) in rows if value.startswith("run-") and value[4:].isascii()
                   and value[4:].isdigit()]
        return f"run-{max(numbers, default=0) + 1}"

    def claim(self, run_id: str) -> dict[str, Any] | None:
        """Claim exactly this queued run, including its preparing event."""
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, revision FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            run = stored_run(json.loads(row[0]), row[1])
            if run.get("status") != "queued":
                raise RunConflictError(f"run is not queued: {run_id}")
            claimed = next_run({**run, "status": "preparing"}, row[1])
            if connection.execute(
                "UPDATE runs SET payload = ?, revision = ? WHERE id = ? AND revision = ? "
                "AND json_extract(payload, '$.status') = 'queued'",
                (json.dumps(claimed, sort_keys=True), claimed["revision"], run_id, row[1]),
            ).rowcount != 1:
                raise RunConflictError(f"run is not queued: {run_id}")
            _append_event(connection, {"run_id": run_id, "type": "preparing", "status": "preparing"})
        return claimed

    def claim_next_queued(self) -> dict[str, Any] | None:
        """Claim the first queued run; the caller still needs an execution lock."""
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = None
            for run_id, payload, revision in connection.execute(
                "SELECT id, payload, revision FROM runs ORDER BY rowid"
            ).fetchall():
                run = stored_run(json.loads(payload), revision)
                if run.get("status") == "queued":
                    claimed = next_run({**run, "status": "preparing"}, revision)
                    connection.execute(
                        "UPDATE runs SET payload = ?, revision = ? WHERE id = ?",
                        (json.dumps(claimed, sort_keys=True), claimed["revision"], run_id),
                    )
                    _append_event(connection, {"run_id": run_id, "type": "preparing", "status": "preparing"})
                    break
        return claimed

    def requeue_interrupted(self) -> list[str]:
        """Legacy recovery; the caller must hold the single-executor lock."""
        requeued: list[str] = []
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for run_id, payload, revision in connection.execute(
                "SELECT id, payload, revision FROM runs ORDER BY rowid"
            ).fetchall():
                run = stored_run(json.loads(payload), revision)
                if run.get("status") in INTERRUPTED_STATES:
                    queued = next_run({**run, "status": "queued"}, revision)
                    connection.execute(
                        "UPDATE runs SET payload = ?, revision = ? WHERE id = ?",
                        (json.dumps(queued, sort_keys=True), queued["revision"], run_id),
                    )
                    requeued.append(run_id)
        return requeued


class _SQLiteCaseRuns:
    def __init__(self, path: str) -> None:
        self._path = path

    def upsert(self, case_run: dict[str, Any]) -> dict[str, Any]:
        """按 (run_id, case_id) 幂等写入；冲突时返回已存在的行。"""
        run_id = case_run["run_id"]
        case_id = case_run["case_id"]
        payload = json.dumps(case_run, sort_keys=True)
        connection = _connect(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM case_runs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO case_runs(run_id, case_id, ordinal, payload) VALUES (?, ?, ?, ?)",
                (run_id, case_id, ordinal, payload),
            )
            row = connection.execute(
                "SELECT payload FROM case_runs WHERE run_id = ? AND case_id = ?", (run_id, case_id)
            ).fetchone()
            connection.execute("COMMIT")
        finally:
            connection.close()
        return json.loads(row[0])

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM case_runs WHERE run_id = ? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, run_id: str, case_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM case_runs WHERE run_id = ? AND case_id = ?", (run_id, case_id)
            ).fetchone()
        return json.loads(row[0]) if row else None


class _SQLiteTraceEvents:
    def __init__(self, path: str) -> None:
        self._path = path

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """分配持久化的单调 seq；唯一约束 (run_id, seq) 由主键保证。"""
        run_id = event["run_id"]
        connection = _connect(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM trace_events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            stored = {**deepcopy(event), "seq": seq}
            connection.execute(
                "INSERT INTO trace_events(run_id, seq, payload) VALUES (?, ?, ?)",
                (run_id, seq, json.dumps(stored, sort_keys=True)),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()
        return stored

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM trace_events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_after(self, run_id: str, seq: int) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM trace_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, seq),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


class _SQLiteScores:
    def __init__(self, path: str) -> None:
        self._path = path

    def replace_for_run(self, run_id: str, scores: list[dict[str, Any]]) -> None:
        scores = validate_scores(scores)
        connection = _connect(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM scores WHERE run_id = ?", (run_id,))
            connection.executemany(
                "INSERT INTO scores(run_id, case_id, ordinal, payload) VALUES (?, ?, ?, ?)",
                [
                    (run_id, score["case_id"], index, json.dumps(score, sort_keys=True))
                    for index, score in enumerate(scores)
                ],
            )
            connection.execute("COMMIT")
        finally:
            connection.close()

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM scores WHERE run_id = ? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


class _InMemoryRuns:
    def __init__(self, events: _InMemoryTraceEvents, lock: RLock) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._events = events
        self._lock = lock

    def _publish(self, run: dict[str, Any], event: dict[str, Any] | None) -> dict[str, Any]:
        if event is not None:
            self._events.append(event)
        self._runs[run["id"]] = deepcopy(run)
        return deepcopy(run)

    def create(self, run: dict[str, Any], *, event: dict[str, Any] | None = None) -> dict[str, Any]:
        stored = new_run(run)
        pending = validate_event(event, stored["id"])
        with self._lock:
            if stored["id"] in self._runs:
                raise RunConflictError(f"run already exists: {stored['id']}")
            return self._publish(stored, pending)

    def update(
        self, run: dict[str, Any], *, expected_revision: int,
        expected_status: str | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = validate_event(event, run["id"])
        with self._lock:
            previous = self._runs.get(run["id"])
            if previous is None or previous["revision"] != expected_revision:
                raise RunConflictError(f"run revision changed: {run['id']}")
            if expected_status is not None and previous.get("status") != expected_status:
                raise RunConflictError(f"run status changed: {run['id']}")
            if expected_status is None and previous.get("status") != run.get("status"):
                raise ValueError("state changes require expected_status; use transition")
            return self._publish(next_run(run, expected_revision), pending)

    def transition(
        self, run_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = validate_event(event, run_id)
        changes = deepcopy(changes or {})
        if {"id", "revision", "schema_version", "status"}.intersection(changes):
            raise ValueError("transition changes cannot overwrite identity, revision or status")
        with self._lock:
            previous = self._runs.get(run_id)
            if previous is None or previous["revision"] != expected_revision:
                raise RunConflictError(f"run revision changed: {run_id}")
            if previous.get("status") != expected_status:
                raise RunConflictError(f"run status changed: {run_id}")
            return self._publish(
                next_run({**previous, **changes, "status": status}, expected_revision), pending
            )

    def save(self, run: dict[str, Any]) -> dict[str, Any]:
        """Legacy blind upsert; only the new methods provide conflict safety."""
        with self._lock:
            previous = self._runs.get(run["id"])
            stored = stored_run(run, previous["revision"] + 1 if previous else 1)
            self._runs[run["id"]] = stored
            return deepcopy(stored)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return deepcopy(run) if run else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(list(self._runs.values()))

    def next_run_id(self) -> str:
        with self._lock:
            numbers = [int(key[4:]) for key in self._runs if key.startswith("run-")
                       and key[4:].isascii() and key[4:].isdigit()]
        return f"run-{max(numbers, default=0) + 1}"

    def claim(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            if run.get("status") != "queued":
                raise RunConflictError(f"run is not queued: {run_id}")
            claimed = next_run({**run, "status": "preparing"}, run["revision"])
            return self._publish(claimed, {"run_id": run_id, "type": "preparing", "status": "preparing"})

    def claim_next_queued(self) -> dict[str, Any] | None:
        with self._lock:
            for run_id, run in self._runs.items():
                if run.get("status") == "queued":
                    claimed = next_run({**run, "status": "preparing"}, run["revision"])
                    return self._publish(claimed, {
                        "run_id": run_id, "type": "preparing", "status": "preparing"
                    })
        return None

    def requeue_interrupted(self) -> list[str]:
        requeued = []
        with self._lock:
            for run_id, run in self._runs.items():
                if run.get("status") in INTERRUPTED_STATES:
                    self._runs[run_id] = next_run({**run, "status": "queued"}, run["revision"])
                    requeued.append(run_id)
        return requeued


class _InMemoryCaseRuns:
    def __init__(self, lock: RLock) -> None:
        self._rows: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        self._lock = lock

    def upsert(self, case_run: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            key = (case_run["run_id"], case_run["case_id"])
            if key not in self._rows:
                ordinal = max(
                    (value[0] for value in self._rows.values()
                     if value[1]["run_id"] == case_run["run_id"]), default=0
                ) + 1
                self._rows[key] = (ordinal, deepcopy(case_run))
            return deepcopy(self._rows[key][1])

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = sorted(
                (ordinal, row) for (row_run, _), (ordinal, row) in self._rows.items()
                if row_run == run_id
            )
            return [deepcopy(row) for _, row in rows]

    def get(self, run_id: str, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._rows.get((run_id, case_id))
            return deepcopy(entry[1]) if entry else None


class _InMemoryTraceEvents:
    def __init__(self, lock: RLock) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._lock = lock

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            events = self._events.setdefault(event["run_id"], [])
            stored = {**deepcopy(event), "seq": len(events) + 1}
            events.append(stored)
            return deepcopy(stored)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._events.get(run_id, []))

    def list_after(self, run_id: str, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([event for event in self._events.get(run_id, []) if event["seq"] > seq])


class _InMemoryScores:
    def __init__(self, lock: RLock) -> None:
        self._scores: dict[str, list[dict[str, Any]]] = {}
        self._lock = lock

    def replace_for_run(self, run_id: str, scores: list[dict[str, Any]]) -> None:
        scores = validate_scores(scores)
        with self._lock:
            self._scores[run_id] = deepcopy(scores)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._scores.get(run_id, []))


@dataclass
class RunStore:
    runs: Any
    case_runs: Any
    events: Any
    scores: Any
    attempts: Any = None
    scoring_passes: Any = None
    score_sets: Any = None
    commands: Any = None
    invocations: Any = None
    external_jobs: Any = None
    benchmark_datasets: Any = None
    baselines: Any = None
    trials: Any = None
    runtime_sessions: Any = None

    def __post_init__(self) -> None:
        if self.commands is not None:
            from .runtime_sessions import attach_interactive_repositories
            attach_interactive_repositories(self)


def SQLiteRunStore(path: str | Path) -> RunStore:
    from .audit_store import SQLiteAttempts, SQLiteCommands, SQLiteScoreSets, SQLiteScoringPasses
    from .sqlite_schema import create_and_upgrade

    path = str(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        # 建缺失表，并把旧库对齐到当前 schema（验收 F-01）：只缺列就补列，主键
        # 不同就按当前 DDL 重建并保留共有列。旧实现在 metric_id 已存在时提前返回，
        # 于是"有 metric_id、没有 trial_id"的中间形状永远补不上，结算期缺列失败。
        create_and_upgrade(connection, _SCHEMA)
    from .baselines import SQLiteBaselines
    from .benchmark_datasets import SQLiteBenchmarkDatasets
    from .external_jobs import SQLiteExternalJobs
    from .invocations import SQLiteInvocations
    from .trials import SQLiteTrials

    return RunStore(
        runs=_SQLiteRuns(path),
        case_runs=_SQLiteCaseRuns(path),
        events=_SQLiteTraceEvents(path),
        scores=_SQLiteScores(path),
        attempts=SQLiteAttempts(path),
        scoring_passes=SQLiteScoringPasses(path),
        score_sets=SQLiteScoreSets(path),
        commands=SQLiteCommands(path),
        invocations=SQLiteInvocations(path),
        external_jobs=SQLiteExternalJobs(path),
        benchmark_datasets=SQLiteBenchmarkDatasets(path),
        baselines=SQLiteBaselines(path),
        trials=SQLiteTrials(path),
    )


def InMemoryRunStore() -> RunStore:
    from .audit_store import MemoryAttempts, MemoryCommands, MemoryScoreSets, MemoryScoringPasses

    lock = RLock()
    events = _InMemoryTraceEvents(lock)
    runs = _InMemoryRuns(events, lock)
    cases = _InMemoryCaseRuns(lock)
    score_sets = MemoryScoreSets(lock)
    attempts = MemoryAttempts(runs, cases, events, lock)
    from .baselines import MemoryBaselines
    from .benchmark_datasets import MemoryBenchmarkDatasets
    from .external_jobs import MemoryExternalJobs
    from .invocations import MemoryInvocations
    from .trials import MemoryTrials

    return RunStore(
        runs=runs,
        case_runs=cases,
        events=events,
        scores=_InMemoryScores(lock),
        attempts=attempts,
        scoring_passes=MemoryScoringPasses(runs, events, score_sets, attempts, cases, lock),
        score_sets=score_sets,
        commands=MemoryCommands(lock),
        invocations=MemoryInvocations(lock),
        external_jobs=MemoryExternalJobs(lock),
        benchmark_datasets=MemoryBenchmarkDatasets(lock),
        baselines=MemoryBaselines(lock),
        trials=MemoryTrials(lock),
    )
