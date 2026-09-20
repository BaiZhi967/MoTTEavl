"""外部 Benchmark 数据准备结果的持久化（review M2-R13）。

准备结果（样本清单、逐行内容、来源治理与许可证据）以
``(benchmark_id, dataset_revision)`` 为主键落库：API/CLI/Worker 共享同一
存储，重启或第二个 API 实例不再把 ready 数据降回 unprepared。payload 由
SDK 侧的序列化函数生成（``PreparedBenchmarkDataset`` 的完整 dump）；
不可变写入比较完整准备语义，而不只比较原始文件 hash。

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


class RevisionConflictError(ValueError):
    """同 revision 不同内容的并发/顺序覆盖尝试（review R3-11）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _content_identity(record: dict[str, Any]) -> str:
    """冻结准备结果的完整语义身份，排除存储生成的 ID 和时间戳。

    原文件 hash 无法区分 default_split、解析版本与归一化行的变化。
    仅文件清单顺序无语义；样本/行顺序参与选择，必须保留。
    """
    semantic = {key: value for key, value in record.items() if key not in {"id", "created_at"}}
    # 旧记录无该字段；反序列化后的 {} 同样表示未知，不能因往返新增空值而冲突。
    # 未知仍不等同于已冻结的 {"default_split": ...}，不猜测旧准备参数。
    semantic.setdefault("preparation", {})
    semantic["files"] = sorted(
        record.get("files") or [], key=lambda item: json.dumps(item, sort_keys=True),
    )
    return json.dumps(semantic, sort_keys=True, separators=(",", ":"), allow_nan=False)


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

    def put_immutable(self, record: dict[str, Any]) -> dict[str, Any]:
        """同 revision 同内容幂等、异内容冲突（review R3-11，事务语义）。

        返回 ``{"status": "created"|"identical", "record": ...}``；异内容
        抛 :class:`RevisionConflictError`（不写入，既有记录不被覆盖）。
        """
        stored = _validate(record)
        benchmark_id = str(stored.get("benchmark_id"))
        dataset_revision = str(stored.get("dataset_revision"))
        stored = {**stored, "id": _record_id(benchmark_id, dataset_revision)}
        identity = _content_identity(stored)
        message = (
            "benchmark dataset revision already exists with different content: "
            + _record_id(benchmark_id, dataset_revision)
        )
        with self._lock:
            existing = self._records.get((benchmark_id, dataset_revision))
            if existing is None:
                self._records[(benchmark_id, dataset_revision)] = stored
                return {"status": "created", "record": deepcopy(stored)}
            if _content_identity(existing) == identity:
                return {"status": "identical", "record": deepcopy(existing)}
            raise RevisionConflictError(message)

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

    def put_immutable(self, record: dict[str, Any]) -> dict[str, Any]:
        """同 revision 同内容幂等、异内容冲突（review R3-11）。

        判定与写入在**同一事务**（BEGIN IMMEDIATE 串行化并发写）：
        两个实例同时"读到不存在"也只有第一个 created，另一个 conflict，
        后写者不能覆盖先写者。
        """
        stored = _validate(record)
        stored = {
            **stored,
            "id": _record_id(str(stored.get("benchmark_id")), str(stored.get("dataset_revision"))),
            "created_at": str(stored.get("created_at") or _now_iso()),
        }
        benchmark_id = str(stored.get("benchmark_id"))
        dataset_revision = str(stored.get("dataset_revision"))
        identity = _content_identity(stored)
        payload_json = json.dumps(stored, sort_keys=True)
        created_at = str(stored.get("created_at"))
        message = (
            "benchmark dataset revision already exists with different content: "
            + _record_id(benchmark_id, dataset_revision)
        )
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM benchmark_datasets WHERE benchmark_id = ? AND dataset_revision = ?",  # noqa: E501
                (benchmark_id, dataset_revision),
            ).fetchone()
            if row is not None:
                existing = json.loads(row[0])
                if _content_identity(existing) == identity:
                    return {"status": "identical", "record": existing}
                raise RevisionConflictError(message)
            connection.execute(
                "INSERT INTO benchmark_datasets(benchmark_id, dataset_revision, payload, created_at) VALUES (?, ?, ?, ?)",  # noqa: E501
                (benchmark_id, dataset_revision, payload_json, created_at),
            )
        return {"status": "created", "record": deepcopy(stored)}

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
    "RevisionConflictError",
    "SQLiteBenchmarkDatasets",
    "dataset_record_from_payload",
]
