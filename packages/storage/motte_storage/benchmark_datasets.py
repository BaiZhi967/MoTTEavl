"""外部 Benchmark 数据准备结果的持久化（review M2-R13）。

准备结果（样本清单、逐行内容、来源治理与许可证据）以
``(benchmark_id, dataset_revision)`` 为主键落库：API/CLI/Worker 共享同一
存储，重启或第二个 API 实例不再把 ready 数据降回 unprepared。payload 由
SDK 侧的序列化函数生成（``PreparedBenchmarkDataset`` 的完整 dump），本
模块只做哑 JSON 存取。

SQL 约定：语句一律为调用点的单行字符串字面量并以占位符绑定；绑定值只
使用普通局部量（``.get()``/``str()``/``json.dumps()``/``row[i]``）。
"""
from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from .run_store import _connect

_SCHEMA_DATASETS = """
    CREATE TABLE IF NOT EXISTS benchmark_datasets (
      benchmark_id TEXT NOT NULL,
      dataset_revision TEXT NOT NULL,
      payload TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (benchmark_id, dataset_revision)
    );
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _validate(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("benchmark dataset record must be an object")
    for field in ("benchmark_id", "dataset_revision", "state"):
        if not isinstance(record.get(field), str) or not record.get(field):
            raise ValueError("benchmark dataset record requires a nonempty string " + field)
    return deepcopy(record)


def _record_id(benchmark_id: str, dataset_revision: str) -> str:
    return benchmark_id + "@" + dataset_revision


class MemoryBenchmarkDatasets:
    def __init__(self, lock: RLock | None = None) -> None:
        self._lock = lock or RLock()
        self._records: dict[tuple[str, str], dict[str, Any]] = {}

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = _validate(record)
        benchmark_id = str(stored.get("benchmark_id"))
        dataset_revision = str(stored.get("dataset_revision"))
        stored = {**stored, "id": _record_id(benchmark_id, dataset_revision)}
        with self._lock:
            self._records[(benchmark_id, dataset_revision)] = stored
        return deepcopy(stored)

    def get(self, benchmark_id: str, dataset_revision: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get((benchmark_id, dataset_revision))
        return deepcopy(record) if record is not None else None

    def latest(self, benchmark_id: str) -> dict[str, Any] | None:
        with self._lock:
            matches = [
                record for record in self._records.values()
                if record.get("benchmark_id") == benchmark_id
            ]
        if not matches:
            return None
        return deepcopy(max(matches, key=lambda item: str(item.get("created_at") or "")))

    def list(self, benchmark_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            matches = [
                record for record in self._records.values()
                if benchmark_id is None or record.get("benchmark_id") == benchmark_id
            ]
        return deepcopy(sorted(
            matches, key=lambda item: str(item.get("created_at") or ""),
        ))


class SQLiteBenchmarkDatasets:
    def __init__(self, path: str) -> None:
        self._path = path
        with closing(_connect(path)) as connection:
            connection.executescript(_SCHEMA_DATASETS)

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = _validate(record)
        stored = {
            **stored,
            "id": _record_id(str(stored.get("benchmark_id")), str(stored.get("dataset_revision"))),
            "created_at": str(stored.get("created_at") or _now_iso()),
        }
        benchmark_id = str(stored.get("benchmark_id"))
        dataset_revision = str(stored.get("dataset_revision"))
        payload_json = json.dumps(stored, sort_keys=True)
        created_at = str(stored.get("created_at"))
        with closing(_connect(self._path)) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO benchmark_datasets(benchmark_id, dataset_revision, payload, created_at) VALUES (?, ?, ?, ?)",  # noqa: E501
                (benchmark_id, dataset_revision, payload_json, created_at),
            )
        return deepcopy(stored)

    def get(self, benchmark_id: str, dataset_revision: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM benchmark_datasets WHERE benchmark_id = ? AND dataset_revision = ?",  # noqa: E501
                (benchmark_id, dataset_revision),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def latest(self, benchmark_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM benchmark_datasets WHERE benchmark_id = ? ORDER BY rowid DESC LIMIT 1",  # noqa: E501
                (benchmark_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def list(self, benchmark_id: str | None = None) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            if benchmark_id is None:
                rows = connection.execute(
                    "SELECT payload FROM benchmark_datasets ORDER BY rowid",
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM benchmark_datasets WHERE benchmark_id = ? ORDER BY rowid",  # noqa: E501
                    (benchmark_id,),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]


def dataset_record_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("benchmark dataset payload must be an object")
    return deepcopy(payload)


__all__ = [
    "MemoryBenchmarkDatasets",
    "SQLiteBenchmarkDatasets",
    "dataset_record_from_payload",
]
