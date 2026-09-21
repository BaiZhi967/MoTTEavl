"""Gate 政策与结果存储（M6-T03）。

- GatePolicyVersion 按 (policy_id, version) 不可变：同内容幂等、异内容冲突；
  弃用只改 lifecycle 字段，不改写历史结论。
- GateResult 按 gate_result_id 追加只读：重复求值同 id 同内容幂等落盘，
  异内容冲突（协议 §1：重复求值不改写原结果）。
- Memory 与 SQLite 语义一致；PostgreSQL 实现在 pg_audit_store。
- SQLite 表由中央 `_SCHEMA`（run_store）创建；语句全部内联字面量 + 参数绑定。
"""
from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy
from threading import RLock
from typing import Any

from .run_store import _connect
from .sqlite_schema import create_and_upgrade


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _validate_policy(payload: dict[str, Any]) -> dict[str, Any]:
    policy_id = payload.get("policy_id")
    version = payload.get("version")
    if not isinstance(policy_id, str) or not policy_id:
        raise ValueError("gate policy requires a nonempty string policy_id")
    if not isinstance(version, str) or not version:
        raise ValueError("gate policy requires a nonempty string version")
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("gate policy requires a nonempty rules list")
    return deepcopy(payload)


def _validate_result(payload: dict[str, Any]) -> dict[str, Any]:
    result_id = payload.get("gate_result_id")
    policy_id = payload.get("policy_id")
    if not isinstance(result_id, str) or not result_id:
        raise ValueError("gate result requires a nonempty string gate_result_id")
    if not isinstance(policy_id, str) or not policy_id:
        raise ValueError("gate result requires a nonempty string policy_id")
    return deepcopy(payload)


class MemoryGateStore:
    def __init__(self, lock: RLock | None = None) -> None:
        self._lock = lock or RLock()
        self._policies: dict[tuple[str, str], dict[str, Any]] = {}
        self._results: dict[str, dict[str, Any]] = {}

    def put_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        policy = _validate_policy(payload)
        key = (policy["policy_id"], policy["version"])
        with self._lock:
            existing = self._policies.get(key)
            if existing is not None:
                if existing != policy:
                    raise ValueError(
                        "gate policies are immutable: "
                        + policy["policy_id"] + "@" + policy["version"]
                    )
                return deepcopy(existing)
            self._policies[key] = policy
            return deepcopy(policy)

    def get_policy(self, policy_id: str, version: str) -> dict[str, Any] | None:
        with self._lock:
            policy = self._policies.get((policy_id, version))
            return deepcopy(policy) if policy is not None else None

    def list_policies(self, policy_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            policies = [
                deepcopy(policy) for (pid, _), policy in self._policies.items()
                if policy_id is None or pid == policy_id
            ]
        return sorted(policies, key=lambda item: (item["policy_id"], item["version"]))

    def deprecate_policy(self, policy_id: str, version: str) -> dict[str, Any]:
        """弃用：只改 lifecycle 字段；已弃用幂等。"""
        with self._lock:
            policy = self._policies.get((policy_id, version))
            if policy is None:
                raise KeyError(policy_id + "@" + version)
            if policy.get("lifecycle") == "deprecated":
                return deepcopy(policy)
            policy["lifecycle"] = "deprecated"
            return deepcopy(policy)

    def put_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = _validate_result(payload)
        with self._lock:
            existing = self._results.get(result["gate_result_id"])
            if existing is not None:
                if existing != result:
                    raise ValueError(
                        "gate results are append-only: " + result["gate_result_id"]
                    )
                return deepcopy(existing)
            self._results[result["gate_result_id"]] = result
            return deepcopy(result)

    def get_result(self, gate_result_id: str) -> dict[str, Any] | None:
        with self._lock:
            result = self._results.get(gate_result_id)
            return deepcopy(result) if result is not None else None

    def list_results(
        self, policy_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            ordered = [
                key for key, value in reversed(list(self._results.items()))
                if policy_id is None or value.get("policy_id") == policy_id
            ]
            return [deepcopy(self._results[key]) for key in ordered[:limit]]


class SQLiteGateStore:
    def __init__(self, path: str) -> None:
        from .run_store import _SCHEMA

        self._path = path
        with closing(_connect(path)) as connection:
            create_and_upgrade(connection, _SCHEMA)

    def put_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        policy = _validate_policy(payload)
        policy_id = policy["policy_id"]
        version = policy["version"]
        payload_json = _dumps(policy)
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gate_policies WHERE policy_id = ? AND version = ?",
                (policy_id, version),
            ).fetchone()
            if row is not None:
                stored = json.loads(row[0])
                if stored != policy:
                    raise ValueError(
                        "gate policies are immutable: " + policy_id + "@" + version
                    )
                connection.rollback()
                return stored
            connection.execute(
                "INSERT INTO gate_policies(policy_id, version, payload) VALUES (?, ?, ?)",
                (policy_id, version, payload_json),
            )
            connection.commit()
        return deepcopy(policy)

    def get_policy(self, policy_id: str, version: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM gate_policies WHERE policy_id = ? AND version = ?",
                (policy_id, version),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def list_policies(self, policy_id: str | None = None) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            if policy_id is None:
                rows = connection.execute(
                    "SELECT payload FROM gate_policies ORDER BY policy_id, version",
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM gate_policies WHERE policy_id = ? ORDER BY version",
                    (policy_id,),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def deprecate_policy(self, policy_id: str, version: str) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gate_policies WHERE policy_id = ? AND version = ?",
                (policy_id, version),
            ).fetchone()
            if row is None:
                raise KeyError(policy_id + "@" + version)
            policy = json.loads(row[0])
            if policy.get("lifecycle") != "deprecated":
                policy["lifecycle"] = "deprecated"
                connection.execute(
                    "UPDATE gate_policies SET payload = ? WHERE policy_id = ? AND version = ?",
                    (_dumps(policy), policy_id, version),
                )
                connection.commit()
            else:
                connection.rollback()
            return policy

    def put_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = _validate_result(payload)
        result_id = result["gate_result_id"]
        policy_id = result["policy_id"]
        payload_json = _dumps(result)
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gate_results WHERE gate_result_id = ?",
                (result_id,),
            ).fetchone()
            if row is not None:
                stored = json.loads(row[0])
                if stored != result:
                    raise ValueError("gate results are append-only: " + result_id)
                connection.rollback()
                return stored
            connection.execute(
                "INSERT INTO gate_results(gate_result_id, policy_id, payload) VALUES (?, ?, ?)",
                (result_id, policy_id, payload_json),
            )
            connection.commit()
        return deepcopy(result)

    def get_result(self, gate_result_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM gate_results WHERE gate_result_id = ?",
                (gate_result_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def list_results(
        self, policy_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            if policy_id is None:
                rows = connection.execute(
                    "SELECT payload FROM gate_results ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM gate_results WHERE policy_id = ? ORDER BY rowid DESC LIMIT ?",
                    (policy_id, limit),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]
