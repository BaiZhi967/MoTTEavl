"""实体级 Run 存储：runs / case_runs / trace_events / scores。

RunService 只依赖这里的 RunStore 接口；SQLite 用于本地开发，InMemory 用于测试。
唯一约束保证幂等：(run_id, seq) 与 (run_id, case_id)。
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
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
"""

# Worker 崩溃后卡住的中间态；重启时回收回 queued。
INTERRUPTED_STATES = ("preparing", "running", "collecting", "scoring")


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


class _SQLiteRuns:
    def __init__(self, path: str) -> None:
        self._path = path

    def save(self, run: dict[str, Any]) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs(id, payload) VALUES (?, ?)",
                (run["id"], json.dumps(run, sort_keys=True)),
            )
        return deepcopy(run)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute("SELECT payload FROM runs ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def next_run_id(self) -> str:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute("SELECT id FROM runs").fetchall()
        numbers = [int(row[0].split("-")[-1]) for row in rows if row[0].startswith("run-")]
        return f"run-{max(numbers, default=0) + 1}"

    def claim_next_queued(self) -> dict[str, Any] | None:
        """原子抢占最早创建的 queued Run（置为 preparing），用于 Worker 轮询调度。"""
        connection = _connect(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT id, payload FROM runs ORDER BY rowid").fetchall()
            claimed = None
            for _, payload in rows:
                run = json.loads(payload)
                if run.get("status") == "queued":
                    claimed = run
                    break
            if claimed is not None:
                claimed["status"] = "preparing"
                connection.execute(
                    "UPDATE runs SET payload = ? WHERE id = ?",
                    (json.dumps(claimed, sort_keys=True), claimed["id"]),
                )
            connection.execute("COMMIT")
        finally:
            connection.close()
        return deepcopy(claimed) if claimed is not None else None

    def requeue_interrupted(self) -> list[str]:
        """把卡在中间态的 Run 回收为 queued（Worker 重启恢复）。"""
        connection = _connect(self._path)
        requeued: list[str] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            for run_id, payload in connection.execute("SELECT id, payload FROM runs ORDER BY rowid").fetchall():
                run = json.loads(payload)
                if run.get("status") in INTERRUPTED_STATES:
                    run["status"] = "queued"
                    connection.execute(
                        "UPDATE runs SET payload = ? WHERE id = ?", (json.dumps(run, sort_keys=True), run_id)
                    )
                    requeued.append(run_id)
            connection.execute("COMMIT")
        finally:
            connection.close()
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
    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

    def save(self, run: dict[str, Any]) -> dict[str, Any]:
        self._runs[run["id"]] = deepcopy(run)
        return deepcopy(run)

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        return deepcopy(run) if run else None

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(run) for run in self._runs.values()]

    def next_run_id(self) -> str:
        numbers = [int(key.split("-")[-1]) for key in self._runs if key.startswith("run-")]
        return f"run-{max(numbers, default=0) + 1}"

    def claim_next_queued(self) -> dict[str, Any] | None:
        for run_id in self._runs:
            run = self._runs[run_id]
            if run.get("status") == "queued":
                claimed = deepcopy(run)
                claimed["status"] = "preparing"
                self._runs[run_id] = claimed
                return deepcopy(claimed)
        return None

    def requeue_interrupted(self) -> list[str]:
        requeued = []
        for run_id in self._runs:
            if self._runs[run_id].get("status") in INTERRUPTED_STATES:
                self._runs[run_id]["status"] = "queued"
                requeued.append(run_id)
        return requeued


class _InMemoryCaseRuns:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}

    def upsert(self, case_run: dict[str, Any]) -> dict[str, Any]:
        key = (case_run["run_id"], case_run["case_id"])
        if key not in self._rows:
            ordinal = max((value[0] for value in self._rows.values() if value[1]["run_id"] == case_run["run_id"]), default=0) + 1
            self._rows[key] = (ordinal, deepcopy(case_run))
        return deepcopy(self._rows[key][1])

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = sorted(
            (ordinal, row) for (row_run, _), (ordinal, row) in self._rows.items() if row_run == run_id
        )
        return [deepcopy(row) for _, row in rows]

    def get(self, run_id: str, case_id: str) -> dict[str, Any] | None:
        entry = self._rows.get((run_id, case_id))
        return deepcopy(entry[1]) if entry else None


class _InMemoryTraceEvents:
    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        events = self._events.setdefault(event["run_id"], [])
        stored = {**deepcopy(event), "seq": len(events) + 1}
        events.append(stored)
        return deepcopy(stored)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return deepcopy(self._events.get(run_id, []))

    def list_after(self, run_id: str, seq: int) -> list[dict[str, Any]]:
        return deepcopy([event for event in self._events.get(run_id, []) if event["seq"] > seq])


class _InMemoryScores:
    def __init__(self) -> None:
        self._scores: dict[str, list[dict[str, Any]]] = {}

    def replace_for_run(self, run_id: str, scores: list[dict[str, Any]]) -> None:
        self._scores[run_id] = deepcopy(scores)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return deepcopy(self._scores.get(run_id, []))


@dataclass
class RunStore:
    runs: Any
    case_runs: Any
    events: Any
    scores: Any


def SQLiteRunStore(path: str | Path) -> RunStore:
    path = str(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        connection.executescript(_SCHEMA)
    return RunStore(
        runs=_SQLiteRuns(path),
        case_runs=_SQLiteCaseRuns(path),
        events=_SQLiteTraceEvents(path),
        scores=_SQLiteScores(path),
    )


def InMemoryRunStore() -> RunStore:
    return RunStore(
        runs=_InMemoryRuns(),
        case_runs=_InMemoryCaseRuns(),
        events=_InMemoryTraceEvents(),
        scores=_InMemoryScores(),
    )
