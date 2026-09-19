"""外部 Job 持久化：Job 记录、幂等导入与冲突账本。

Job 状态描述外部作业子过程（launching/active/collecting/settled/...），
不替代 Run 状态。导入幂等键 = job_id + source_record_key + parser_version
（需求 4.3）：同键同内容重复导入 no-op；同键不同内容 conflict——已落库
记录保持不变，incoming 摘要写入冲突账本，两份都保留。记录与 checkpoint
在同一事务提交。

SQL 约定：语句一律为调用点的单行字符串字面量并以占位符绑定；绑定值只
使用普通局部量（``.get()``/``str()``/``json.dumps()``/``row[i]``）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from .integrity import RunConflictError
from .run_store import _connect

RECOVERABLE_STATUSES = ("launching", "active", "collecting")

_SCHEMA_JOBS = """
    CREATE TABLE IF NOT EXISTS external_jobs (
      job_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      status TEXT NOT NULL,
      launch_token TEXT NOT NULL,
      payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS external_jobs_run_idx ON external_jobs(run_id);
    CREATE TABLE IF NOT EXISTS external_job_records (
      job_id TEXT NOT NULL,
      source_record_key TEXT NOT NULL,
      parser_version TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      payload TEXT NOT NULL,
      imported_at TEXT NOT NULL,
      PRIMARY KEY (job_id, source_record_key, parser_version)
    );
    CREATE TABLE IF NOT EXISTS external_job_conflicts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id TEXT NOT NULL,
      source_record_key TEXT NOT NULL,
      parser_version TEXT NOT NULL,
      existing_hash TEXT NOT NULL,
      incoming_hash TEXT NOT NULL,
      incoming_payload TEXT NOT NULL,
      detected_at TEXT NOT NULL
    );
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _validate_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise ValueError("job must be an object")
    for field in ("job_id", "run_id", "status", "launch_token"):
        if not isinstance(job.get(field), str) or not job.get(field):
            raise ValueError("job requires a nonempty string " + field)
    return deepcopy(job)


def _validate_import_args(
    job_id: str, source_record_key: str, parser_version: str, content_hash: str,
) -> None:
    for name, value in (
        ("job_id", job_id), ("source_record_key", source_record_key),
        ("parser_version", parser_version), ("content_hash", content_hash),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError("import requires a nonempty string " + name)


def _job_defaults(stored: dict[str, Any]) -> dict[str, Any]:
    stored.setdefault("spec", {})
    stored.setdefault("handle", {})
    stored.setdefault("checkpoint", {})
    now = _now_iso()
    stored.setdefault("created_at", now)
    stored["updated_at"] = now
    return stored


class MemoryExternalJobs:
    def __init__(self, lock: RLock | None = None) -> None:
        self._lock = lock or RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._records: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._conflicts: list[dict[str, Any]] = []

    def begin_job(self, job: dict[str, Any]) -> dict[str, Any]:
        stored = _job_defaults(_validate_job(job))
        job_id = str(stored.get("job_id"))
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                if existing.get("launch_token") != stored.get("launch_token"):
                    raise RunConflictError(
                        "job " + job_id + " already exists with a different "
                        "launch token; a job is never started twice"
                    )
                return deepcopy(existing)
            self._jobs[job_id] = stored
            return deepcopy(stored)

    def update_job(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                raise KeyError(job_id)
            updated = {**deepcopy(current), **deepcopy(changes), "updated_at": _now_iso()}
            self._jobs[job_id] = updated
            return deepcopy(updated)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job is not None else None

    def jobs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                job for job in self._jobs.values() if job.get("run_id") == run_id
            ])

    def recoverable_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                job for job in self._jobs.values()
                if job.get("run_id") == run_id
                and job.get("status") in RECOVERABLE_STATUSES
            ])

    def import_record(
        self,
        job_id: str,
        source_record_key: str,
        parser_version: str,
        content_hash: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_import_args(job_id, source_record_key, parser_version, content_hash)
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            key = (job_id, source_record_key, parser_version)
            existing = self._records.get(key)
            if existing is not None:
                if existing.get("content_hash") == content_hash:
                    return {"status": "noop", "record": deepcopy(existing)}
                conflict = {
                    "job_id": job_id,
                    "source_record_key": source_record_key,
                    "parser_version": parser_version,
                    "existing_hash": existing.get("content_hash"),
                    "incoming_hash": content_hash,
                    "incoming_payload": deepcopy(payload),
                    "detected_at": _now_iso(),
                }
                self._conflicts.append(conflict)
                return {
                    "status": "conflict",
                    "existing": deepcopy(existing),
                    "incoming": {
                        "content_hash": content_hash, "payload": deepcopy(payload),
                    },
                }
            record = {
                "job_id": job_id,
                "source_record_key": source_record_key,
                "parser_version": parser_version,
                "content_hash": content_hash,
                "payload": deepcopy(payload),
                "imported_at": _now_iso(),
            }
            self._records[key] = record
            # 记录与 checkpoint 同一临界区（内存实现的事务等价物）。
            job = self._jobs[job_id]
            checkpoint = dict(job.get("checkpoint") or {})
            checkpoint["records_consumed"] = int(checkpoint.get("records_consumed", 0)) + 1
            job["checkpoint"] = checkpoint
            job["updated_at"] = _now_iso()
            return {"status": "imported", "record": deepcopy(record)}

    def list_records(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                record for record in self._records.values()
                if record.get("job_id") == job_id
            ])

    def list_conflicts(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                conflict for conflict in self._conflicts
                if job_id is None or conflict.get("job_id") == job_id
            ])


class SQLiteExternalJobs:
    def __init__(self, path: str) -> None:
        self._path = path
        with closing(_connect(path)) as connection:
            connection.executescript(_SCHEMA_JOBS)

    def begin_job(self, job: dict[str, Any]) -> dict[str, Any]:
        stored = _job_defaults(_validate_job(job))
        job_id = str(stored.get("job_id"))
        run_id = str(stored.get("run_id"))
        status = str(stored.get("status"))
        launch_token = str(stored.get("launch_token"))
        payload_json = json.dumps(stored, sort_keys=True)
        message = (
            "job already exists with a different launch token; "
            "a job is never started twice: " + job_id
        )
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM external_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                existing = json.loads(row[0])
                if str(existing.get("launch_token")) != launch_token:
                    raise RunConflictError(message)
                return deepcopy(existing)
            connection.execute(
                "INSERT INTO external_jobs(job_id, run_id, status, launch_token, payload) VALUES (?, ?, ?, ?, ?)",  # noqa: E501
                (job_id, run_id, status, launch_token, payload_json),
            )
        return deepcopy(stored)

    def update_job(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM external_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = json.loads(row[0])
            updated = {**job, **deepcopy(changes), "updated_at": _now_iso()}
            new_status = str(updated.get("status") or job.get("status"))
            new_payload = json.dumps(updated, sort_keys=True)
            connection.execute(
                "UPDATE external_jobs SET status = ?, payload = ? WHERE job_id = ?",
                (new_status, new_payload, job_id),
            )
            return deepcopy(updated)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM external_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def jobs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM external_jobs WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recoverable_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [
            job for job in self.jobs_for_run(run_id)
            if job.get("status") in RECOVERABLE_STATUSES
        ]

    def import_record(
        self,
        job_id: str,
        source_record_key: str,
        parser_version: str,
        content_hash: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_import_args(job_id, source_record_key, parser_version, content_hash)
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            job_row = connection.execute(
                "SELECT payload FROM external_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            existing = connection.execute(
                "SELECT content_hash, payload FROM external_job_records WHERE job_id = ? AND source_record_key = ? AND parser_version = ?",  # noqa: E501
                (job_id, source_record_key, parser_version),
            ).fetchone()
            if existing is not None:
                existing_hash = existing[0]
                existing_payload = json.loads(existing[1])
                if existing_hash == content_hash:
                    record = {
                        "job_id": job_id,
                        "source_record_key": source_record_key,
                        "parser_version": parser_version,
                        "content_hash": existing_hash,
                        "payload": existing_payload,
                    }
                    return {"status": "noop", "record": record}
                incoming_json = json.dumps(payload, sort_keys=True)
                detected_at = _now_iso()
                connection.execute(
                    "INSERT INTO external_job_conflicts(job_id, source_record_key, parser_version, existing_hash, incoming_hash, incoming_payload, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?)",  # noqa: E501
                    (job_id, source_record_key, parser_version, existing_hash, content_hash, incoming_json, detected_at),  # noqa: E501
                )
                return {
                    "status": "conflict",
                    "existing": {
                        "job_id": job_id,
                        "source_record_key": source_record_key,
                        "parser_version": parser_version,
                        "content_hash": existing_hash,
                        "payload": existing_payload,
                    },
                    "incoming": {
                        "content_hash": content_hash, "payload": deepcopy(payload),
                    },
                }
            imported_at = _now_iso()
            record = {
                "job_id": job_id,
                "source_record_key": source_record_key,
                "parser_version": parser_version,
                "content_hash": content_hash,
                "payload": deepcopy(payload),
                "imported_at": imported_at,
            }
            payload_json = json.dumps(record.get("payload"), sort_keys=True)
            connection.execute(
                "INSERT INTO external_job_records(job_id, source_record_key, parser_version, content_hash, payload, imported_at) VALUES (?, ?, ?, ?, ?, ?)",  # noqa: E501
                (job_id, source_record_key, parser_version, content_hash, payload_json, imported_at),  # noqa: E501
            )
            # checkpoint 与记录同事务提交。
            job = json.loads(job_row[0])
            checkpoint = dict(job.get("checkpoint") or {})
            checkpoint["records_consumed"] = int(checkpoint.get("records_consumed", 0)) + 1
            job["checkpoint"] = checkpoint
            job["updated_at"] = _now_iso()
            job_json = json.dumps(job, sort_keys=True)
            connection.execute(
                "UPDATE external_jobs SET status = ?, payload = ? WHERE job_id = ?",
                (job.get("status"), job_json, job_id),
            )
            return {"status": "imported", "record": record}

    def list_records(self, job_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT job_id, source_record_key, parser_version, content_hash, payload FROM external_job_records WHERE job_id = ? ORDER BY imported_at, rowid",  # noqa: E501
                (job_id,),
            ).fetchall()
        return [
            {
                "job_id": row[0], "source_record_key": row[1],
                "parser_version": row[2], "content_hash": row[3],
                "payload": json.loads(row[4]),
            }
            for row in rows
        ]

    def list_conflicts(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            if job_id is None:
                rows = connection.execute(
                    "SELECT job_id, source_record_key, parser_version, existing_hash, incoming_hash, incoming_payload, detected_at FROM external_job_conflicts ORDER BY id",  # noqa: E501
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT job_id, source_record_key, parser_version, existing_hash, incoming_hash, incoming_payload, detected_at FROM external_job_conflicts WHERE job_id = ? ORDER BY id",  # noqa: E501
                    (job_id,),
                ).fetchall()
        return [
            {
                "job_id": row[0], "source_record_key": row[1],
                "parser_version": row[2], "existing_hash": row[3],
                "incoming_hash": row[4], "incoming_payload": json.loads(row[5]),
                "detected_at": row[6],
            }
            for row in rows
        ]
