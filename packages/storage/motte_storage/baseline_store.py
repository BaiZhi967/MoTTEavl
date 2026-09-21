"""Baseline 快照存储 v2（M6-T03/T07）：分组 entries、资格、CAS 默认指针。

- BaselineSnapshot 契约（motte_contracts.comparison）：entries 固定
  RunReportRef 集合 + comparison_policy_hash + 资格 + 审计字段；写入后不可变
  （同 id 同内容幂等、异内容冲突）。
- 默认 baseline 是**指针操作**（scope → snapshot_id）：``set_default`` 以
  ``expected_current`` 做 CAS（None 表示首次设置），position 递增，历史全留
  （操作者/理由/policy hash 进 payload），绝不覆盖旧指针。
- Memory 与 SQLite 语义一致；PostgreSQL 实现在 pg_audit_store。
- 语句全部内联字面量 + 参数绑定。
"""
from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy
from threading import RLock
from typing import Any

from motte_contracts.comparison import BaselineSnapshot

from .run_store import _connect
from .sqlite_schema import create_and_upgrade


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    # 契约校验（extra=forbid）+ JSON 规范化（tuple→list）：memory 与
    # sqlite/pg 落盘读回的形状一致，"同内容幂等"判定不受容器类型影响。
    return BaselineSnapshot.model_validate(payload).model_dump(mode="json")


class BaselineConflict(ValueError):
    """同 id 异内容 / CAS 期望不符。``code`` 供 API 映射 409。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MemoryBaselineStore:
    def __init__(self, lock: RLock | None = None) -> None:
        self._lock = lock or RLock()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._defaults: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    def put(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = _validate(payload)
        snapshot_id = snapshot["baseline_id"]
        with self._lock:
            existing = self._snapshots.get(snapshot_id)
            if existing is not None:
                if existing != snapshot:
                    raise BaselineConflict(
                        "BASELINE_IMMUTABLE",
                        "baseline snapshots are immutable: " + snapshot_id,
                    )
                return deepcopy(existing)
            self._snapshots[snapshot_id] = snapshot
            return deepcopy(snapshot)

    def get(self, baseline_id: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.get(baseline_id)
            return deepcopy(snapshot) if snapshot is not None else None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            snapshots = [deepcopy(item) for item in self._snapshots.values()]
        snapshots.sort(key=lambda item: item.get("created_at", ""))
        return snapshots[-limit:]

    def set_default(
        self, pointer: dict[str, Any], *, expected_current: str | None = None,
    ) -> dict[str, Any]:
        pointer = deepcopy(pointer)
        scope = pointer["scope"]
        with self._lock:
            current = self._defaults.get(scope)
            if expected_current is not None:
                current_id = current.get("baseline_id") if current else None
                if current_id != expected_current:
                    raise BaselineConflict(
                        "CAS_CONFLICT",
                        "default baseline for scope " + scope + " is "
                        + repr(current_id) + ", expected " + repr(expected_current),
                    )
            elif current is not None:
                raise BaselineConflict(
                    "CAS_CONFLICT",
                    "default baseline for scope " + scope
                    + " already exists; pass expected_current to move it",
                )
            if current is not None and current.get("baseline_id") == pointer["baseline_id"]:
                return deepcopy(current)
            pointer["position"] = (current.get("position", 0) + 1) if current else 1
            self._defaults[scope] = deepcopy(pointer)
            self._history.setdefault(scope, []).append(deepcopy(pointer))
            return deepcopy(pointer)

    def get_default(self, scope: str) -> dict[str, Any] | None:
        with self._lock:
            pointer = self._defaults.get(scope)
            return deepcopy(pointer) if pointer is not None else None

    def default_history(self, scope: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            history = list(self._history.get(scope, ()))
        return deepcopy(history[-limit:])


class SQLiteBaselineStore:
    def __init__(self, path: str) -> None:
        from .run_store import _SCHEMA

        self._path = path
        with closing(_connect(path)) as connection:
            create_and_upgrade(connection, _SCHEMA)

    def put(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = _validate(payload)
        snapshot_id = snapshot["baseline_id"]
        payload_json = _dumps(snapshot)
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM m6_baselines WHERE baseline_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is not None:
                stored = json.loads(row[0])
                if stored != snapshot:
                    raise BaselineConflict(
                        "BASELINE_IMMUTABLE",
                        "baseline snapshots are immutable: " + snapshot_id,
                    )
                connection.rollback()
                return stored
            connection.execute(
                "INSERT INTO m6_baselines(baseline_id, payload) VALUES (?, ?)",
                (snapshot_id, payload_json),
            )
            connection.commit()
        return deepcopy(snapshot)

    def get(self, baseline_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM m6_baselines WHERE baseline_id = ?",
                (baseline_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM m6_baselines ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def set_default(
        self, pointer: dict[str, Any], *, expected_current: str | None = None,
    ) -> dict[str, Any]:
        pointer = deepcopy(pointer)
        scope = pointer["scope"]
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM default_baselines WHERE scope = ?",
                (scope,),
            ).fetchone()
            current = json.loads(row[0]) if row is not None else None
            if expected_current is not None:
                current_id = current.get("baseline_id") if current else None
                if current_id != expected_current:
                    raise BaselineConflict(
                        "CAS_CONFLICT",
                        "default baseline for scope " + scope + " is "
                        + repr(current_id) + ", expected " + repr(expected_current),
                    )
            elif current is not None:
                raise BaselineConflict(
                    "CAS_CONFLICT",
                    "default baseline for scope " + scope
                    + " already exists; pass expected_current to move it",
                )
            if (
                current is not None
                and current.get("baseline_id") == pointer["baseline_id"]
            ):
                connection.rollback()
                return current
            pointer["position"] = (current.get("position", 0) + 1) if current else 1
            payload_json = _dumps(pointer)
            connection.execute(
                "INSERT INTO default_baselines(scope, baseline_id, position, payload)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(scope) DO UPDATE SET baseline_id = excluded.baseline_id,"
                " position = excluded.position, payload = excluded.payload",
                (scope, pointer["baseline_id"], pointer["position"], payload_json),
            )
            connection.execute(
                "INSERT INTO default_baseline_history(scope, position, payload)"
                " VALUES (?, ?, ?)",
                (scope, pointer["position"], payload_json),
            )
            connection.commit()
            return pointer

    def get_default(self, scope: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM default_baselines WHERE scope = ?",
                (scope,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def default_history(self, scope: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM default_baseline_history"
                " WHERE scope = ? ORDER BY position DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]
