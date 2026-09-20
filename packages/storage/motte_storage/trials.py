"""Trial 计划与结果存储（M3-T02，需求 4.2/4.3）。

三种后端（Memory / SQLite / PostgreSQL）语义一致：``store.trials`` 由
``RunStore`` 装配，测试既覆盖孤立 repository 也覆盖生产 factory 装配路径。

身份与幂等（与 M2 的 external job import 同一套原则）：

- ``create_plans``：同 ``trial_id`` 同内容 = ``identical``（重复创建是
  no-op，不产生第二个 Trial）；同 ``trial_id`` 异内容 = ``conflict``，
  **保留先写入的计划**，两份摘要都返回；新建 = ``created``。
- ``put_result``：终态结果不可覆盖。同 ``trial_id`` 同 ``source_hash``
  同内容 = ``identical``；同 ``trial_id`` 但 ``source_hash`` 或内容不同 =
  ``conflict``（M3-A10：原始证据变化/重复采集必须显式失败，不覆盖旧证据）。
  写入前先核对身份：``result["trial_id"]`` 必须等于目标 Trial，结果里若带
  ``run_id`` / ``task_key`` / ``repeat_index`` 必须与冻结计划一致，错配抛
  ``ValueError`` 且不改动旧记录（review R26）。
- ``list_for_run`` 返回同一 Run 的全部计划单元（包括尚无结果的），因此
  "全部计划单元都有 disposition" 可以被外部检查，而不是靠缺行推断。

每条 SQL 都是固定字面语句、值全部走参数绑定；表名与列名从不来自变量。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Protocol

from .run_store import _connect

#: 结果未落盘时 Trial 的占位状态；结果落盘后为 ``result`` 载荷里的 disposition。
PENDING_STATUS = "pending"

_PLAN_FIELDS = (
    "trial_id", "run_id", "task_key", "repeat_index",
    "agent_config_hash", "environment_hash",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TrialRepository(Protocol):
    """Trial 持久化接口（生产 factory 装配 ``store.trials``）。"""

    def create_plans(self, plans: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]: ...

    def list_for_task(self, run_id: str, task_key: str) -> list[dict[str, Any]]: ...

    def get(self, trial_id: str) -> dict[str, Any] | None: ...

    def put_result(
        self, trial_id: str, result: dict[str, Any], *,
        source_hash: str, parser_version: str,
    ) -> dict[str, Any]: ...


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """计划必须带完整身份字段；``repeat_index`` 非负整数、``trial_id`` 非空。"""
    stored = deepcopy(plan)
    for field in _PLAN_FIELDS:
        if field == "repeat_index":
            value = stored.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("trial plan requires a non-negative integer repeat_index")
            continue
        if not isinstance(stored.get(field), str) or not stored[field]:
            raise ValueError(f"trial plan requires a nonempty string {field}")
    return stored


def validate_result(result: dict[str, Any]) -> dict[str, Any]:
    """结果必须带 trial_id 与 disposition；其余载荷原样保存。"""
    stored = deepcopy(result)
    if not isinstance(stored.get("trial_id"), str) or not stored["trial_id"]:
        raise ValueError("trial result requires a nonempty string trial_id")
    disposition = stored.get("disposition")
    if not isinstance(disposition, str) or not disposition:
        raise ValueError("trial result requires a disposition")
    return stored


def _plan_conflict(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    # 出口一律深拷贝：existing 在 Memory 后端就是内部记录，调用者不能借返回值改动证据。
    return {
        "status": "conflict",
        "trial_id": incoming["trial_id"],
        "existing": {
            "plan_hash": existing.get("plan_hash"),
            "plan": deepcopy(existing.get("plan")),
        },
        "incoming": {
            "plan_hash": incoming.get("plan_hash"),
            "plan": deepcopy(incoming.get("plan")),
        },
    }


def _result_conflict(
    existing: dict[str, Any], incoming: dict[str, Any], *, source_hash: str,
    parser_version: str,
) -> dict[str, Any]:
    return {
        "status": "conflict",
        "trial_id": incoming["trial_id"],
        "existing": {
            "result_hash": existing.get("result_hash"),
            "source_hash": existing.get("source_hash"),
            "parser_version": existing.get("parser_version"),
        },
        "incoming": {
            "result_hash": _canonical(incoming),
            "source_hash": source_hash,
            "parser_version": parser_version,
        },
    }


#: TrialResult 里允许出现的归属字段：出现时必须与目标 Trial 的冻结计划一致（R26）。
_RESULT_IDENTITY_FIELDS = ("run_id", "task_key", "repeat_index")


def _check_result_identity(
    record: dict[str, Any], result: dict[str, Any], trial_id: str,
) -> None:
    """拒绝把别的 Trial/Run/Task/repeat 的结果写进目标 Trial（review R26）。

    ``result["trial_id"]`` 是必需匹配项；``run_id`` / ``task_key`` /
    ``repeat_index`` 出现时必须与冻结计划逐项一致。错配抛 ``ValueError``，
    调用方必须在任何写入之前调用本函数，旧记录因此不会被改动。
    """
    foreign = result.get("trial_id")
    if foreign != trial_id:
        raise ValueError(
            f"trial result identity mismatch: trial_id {foreign!r} != target {trial_id!r}"
        )
    for field in _RESULT_IDENTITY_FIELDS:
        if field not in result:
            continue
        expected = record.get(field)
        if result[field] != expected:
            raise ValueError(
                f"trial result identity mismatch: {field} {result[field]!r} "
                f"!= frozen plan {expected!r}"
            )


def _plan_record(plan: dict[str, Any]) -> dict[str, Any]:
    stored = validate_plan(plan)
    stored.update({
        "plan": deepcopy(plan),
        "plan_hash": _canonical(plan),
        "status": PENDING_STATUS,
        "created_at": _now_iso(),
        "result": None,
        "result_hash": None,
        "source_hash": None,
        "parser_version": None,
        "finished_at": None,
    })
    return stored


def is_synthetic_result(result: Any) -> bool:
    """结果是否只是**平台补出的处置占位**（不是来自 Runner 的证据）。

    取消/超时终态化时，平台会给"还没有结果"的计划单元补一个 disposition
    （``synthesized_by`` 标记），让覆盖分母不因缺行而虚高。占位不是证据，
    因此它可以被随后到达的真实冻结结果替换（review R2-01）；真实证据之间
    仍然严格冲突、永不覆盖。
    """
    return isinstance(result, dict) and bool(result.get("synthesized_by"))


def _placeholder_replacement(
    existing: dict[str, Any], incoming: dict[str, Any],
) -> dict[str, Any] | None:
    """真实证据替换平台占位时返回替换审计摘要（不是证据冲突）。"""
    if not is_synthetic_result(existing.get("result")) or is_synthetic_result(incoming):
        return None
    previous = existing.get("result") or {}
    return {
        "previous": {
            "disposition": previous.get("disposition"),
            "synthesized_by": previous.get("synthesized_by"),
            "result_hash": existing.get("result_hash"),
        },
    }


def _apply_result(
    row: dict[str, Any], stored: dict[str, Any], *,
    source_hash: str, parser_version: str, result_hash: str,
) -> dict[str, Any]:
    """写入结果（含"占位可被真实证据替换"规则）；返回平台侧结论。"""
    replacement = _placeholder_replacement(row, stored)
    row.update({
        "result": stored,
        "result_hash": result_hash,
        "source_hash": source_hash,
        "parser_version": parser_version,
        "status": str(stored.get("disposition") or "unknown"),
        "finished_at": _now_iso(),
    })
    if replacement is not None:
        return {
            "status": "replaced_placeholder",
            "trial_id": stored["trial_id"],
            "previous": replacement["previous"],
            "result": deepcopy(stored),
        }
    return {"status": "stored", "trial_id": stored["trial_id"], "result": deepcopy(stored)}


def _apply_plan(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """同 trial_id 同计划：idempotent no-op；异计划保留先写入者并报冲突。"""
    if existing.get("plan_hash") == incoming.get("plan_hash"):
        return {
            "status": "identical", "trial_id": incoming["trial_id"],
            "plan": deepcopy(incoming),
        }
    return _plan_conflict(existing, incoming)


class MemoryTrials:
    """进程内 Trial 存储（与 SQLite/PG 同语义）。"""

    def __init__(self, lock: RLock | None = None) -> None:
        self._lock = lock or RLock()
        self._rows: dict[str, dict[str, Any]] = {}

    def create_plans(self, plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with self._lock:
            for plan in plans:
                incoming = _plan_record(plan)
                existing = self._rows.get(incoming["trial_id"])
                if existing is None:
                    self._rows[incoming["trial_id"]] = incoming
                    # 存入内部字典的是 incoming 本身，返回值必须是副本（review R25）。
                    results.append({
                        "status": "created", "trial_id": incoming["trial_id"],
                        "plan": deepcopy(incoming),
                    })
                else:
                    results.append(_apply_plan(existing, incoming))
        return results

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(sorted(
                (row for row in self._rows.values() if row["run_id"] == run_id),
                key=lambda row: (row["task_key"], row["repeat_index"], row["trial_id"]),
            ))

    def list_for_task(self, run_id: str, task_key: str) -> list[dict[str, Any]]:
        return [row for row in self.list_for_run(run_id) if row["task_key"] == task_key]

    def get(self, trial_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows.get(trial_id)
            return deepcopy(row) if row is not None else None

    def put_result(
        self, trial_id: str, result: dict[str, Any], *,
        source_hash: str, parser_version: str,
    ) -> dict[str, Any]:
        stored = validate_result(result)
        with self._lock:
            row = self._rows.get(trial_id)
            if row is None:
                return {"status": "unknown_trial", "trial_id": trial_id}
            _check_result_identity(row, stored, trial_id)
            result_hash = _canonical(stored)
            if row.get("result") is not None:
                if row.get("source_hash") == source_hash and row.get("result_hash") == result_hash:
                    return {
                        "status": "identical", "trial_id": trial_id,
                        "result": deepcopy(row["result"]),
                    }
                if _placeholder_replacement(row, stored) is None:
                    return _result_conflict(
                        row, stored, source_hash=source_hash, parser_version=parser_version,
                    )
            return _apply_result(
                row, stored, source_hash=source_hash,
                parser_version=parser_version, result_hash=result_hash,
            )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """建表 + 幂等补列（旧库升级只加列，不重建、不丢既有计划）。"""
    connection.executescript("""CREATE TABLE IF NOT EXISTS trials (
  trial_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_key TEXT NOT NULL,
  repeat_index INTEGER NOT NULL,
  status TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  payload TEXT NOT NULL,
  result_payload TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS trials_run_idx ON trials(run_id);
CREATE INDEX IF NOT EXISTS trials_task_idx ON trials(run_id, task_key);
""")
    existing = {row[1] for row in connection.execute("PRAGMA table_info(trials)").fetchall()}
    if "result_payload" not in existing:
        connection.execute("ALTER TABLE trials ADD COLUMN result_payload TEXT")
    if "finished_at" not in existing:
        connection.execute("ALTER TABLE trials ADD COLUMN finished_at TEXT")


class SQLiteTrials:
    def __init__(self, path: str) -> None:
        self._path = path
        with closing(_connect(path)) as connection, connection:
            _ensure_schema(connection)

    def _row(self, record: dict[str, Any]) -> dict[str, Any]:
        result = record.get("result")
        return {
            "trial_id": record["trial_id"],
            "run_id": record["run_id"],
            "task_key": record["task_key"],
            "repeat_index": int(record["repeat_index"]),
            "status": str(record.get("status") or PENDING_STATUS),
            "plan_hash": record["plan_hash"],
            "payload": json.dumps(record, ensure_ascii=False, sort_keys=True),
            "result_payload": (
                json.dumps(result, ensure_ascii=False, sort_keys=True)
                if result is not None else None
            ),
            "created_at": record.get("created_at") or _now_iso(),
            "finished_at": record.get("finished_at"),
        }

    def create_plans(self, plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for plan in plans:
                incoming = _plan_record(plan)
                row = connection.execute("SELECT payload FROM trials WHERE trial_id = ?", (incoming["trial_id"],)).fetchone()
                if row is None:
                    connection.execute("INSERT INTO trials (trial_id, run_id, task_key, repeat_index, status, plan_hash, payload, result_payload, created_at, finished_at) VALUES (:trial_id, :run_id, :task_key, :repeat_index, :status, :plan_hash, :payload, :result_payload, :created_at, :finished_at)", self._row(incoming))
                    results.append({
                        "status": "created", "trial_id": incoming["trial_id"],
                        "plan": deepcopy(incoming),
                    })
                    continue
                results.append(_apply_plan(json.loads(row[0]), incoming))
        return results

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute("SELECT payload FROM trials WHERE run_id = ? ORDER BY task_key, repeat_index, trial_id", (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_for_task(self, run_id: str, task_key: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute("SELECT payload FROM trials WHERE run_id = ? AND task_key = ? ORDER BY repeat_index, trial_id", (run_id, task_key)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, trial_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute("SELECT payload FROM trials WHERE trial_id = ?", (trial_id,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def put_result(
        self, trial_id: str, result: dict[str, Any], *,
        source_hash: str, parser_version: str,
    ) -> dict[str, Any]:
        stored = validate_result(result)
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload, result_payload FROM trials WHERE trial_id = ?", (trial_id,)).fetchone()
            if row is None:
                return {"status": "unknown_trial", "trial_id": trial_id}
            current = json.loads(row[0])
            _check_result_identity(current, stored, trial_id)
            result_hash = _canonical(stored)
            if row[1] is not None:
                if current.get("source_hash") == source_hash and current.get("result_hash") == result_hash:
                    return {"status": "identical", "trial_id": trial_id, "result": json.loads(row[1])}
                if _placeholder_replacement(current, stored) is None:
                    return _result_conflict(current, stored, source_hash=source_hash, parser_version=parser_version)
            outcome = _apply_result(
                current, stored, source_hash=source_hash,
                parser_version=parser_version, result_hash=result_hash,
            )
            connection.execute("UPDATE trials SET status = :status, payload = :payload, result_payload = :result_payload, finished_at = :finished_at WHERE trial_id = :trial_id", self._row(current))
            return outcome
