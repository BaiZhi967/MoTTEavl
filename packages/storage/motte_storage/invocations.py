"""Persistent agent invocation boundaries: prepared -> dispatching -> settled.

每次模型/工具调用的持久边界。这是 CaseAttempt 之下的证据与恢复判定，
不是第二套 Run 状态机：settled 需要 outcome；未 settled 的 dispatching
记录表示副作用可能已发生（indeterminate）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from threading import RLock
from typing import Any

from .integrity import RunConflictError
from .run_store import _connect

INVOCATION_TRANSITIONS = {
    "prepared": {"dispatching", "settled"},
    "dispatching": {"settled"},
    "settled": set(),
}


def validate_invocation(record: dict[str, Any]) -> dict[str, Any]:
    from motte_contracts.evaluation import InvocationRecord

    try:
        return InvocationRecord.model_validate(record).model_dump(mode="json")
    except Exception as error:  # noqa: BLE001 - 统一转存储层 ValueError
        raise ValueError(f"invocation does not match the contract: {error}") from error


class SQLiteInvocations:
    def __init__(self, path: str) -> None:
        self._path = path

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = validate_invocation({**record, "status": record.get("status", "prepared")})
        try:
            with closing(_connect(self._path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO agent_invocations(id, run_id, case_id, kind, step, status, "
                    "revision, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (stored["id"], stored["run_id"], stored["case_id"], stored["kind"],
                     stored["step"], stored["status"], stored["revision"],
                     json.dumps(stored, sort_keys=True)),
                )
        except sqlite3.IntegrityError as error:
            raise RunConflictError(f"invocation already exists: {stored['id']}") from error
        return deepcopy(stored)

    def get(self, invocation_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM agent_invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def transition(
        self, invocation_id: str, *, expected_revision: int, expected_status: str,
        status: str, changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM agent_invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
            if row is None:
                raise RunConflictError(f"invocation missing: {invocation_id}")
            current = json.loads(row[0])
            if current.get("revision") != expected_revision or current.get("status") != expected_status:
                raise RunConflictError(f"invocation revision or status changed: {invocation_id}")
            if status not in INVOCATION_TRANSITIONS.get(expected_status, set()):
                raise ValueError(f"invalid invocation transition: {expected_status} -> {status}")
            stored = {
                **current, **deepcopy(changes or {}), "status": status,
                "revision": expected_revision + 1,
            }
            stored = validate_invocation(stored)
            if connection.execute(
                "UPDATE agent_invocations SET payload = ?, status = ?, revision = ? "
                "WHERE id = ? AND status = ? AND revision = ?",
                (json.dumps(stored, sort_keys=True), status, stored["revision"],
                 invocation_id, expected_status, expected_revision),
            ).rowcount != 1:
                raise RunConflictError(f"invocation revision or status changed: {invocation_id}")
        return deepcopy(stored)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_invocations WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_for_case(self, run_id: str, case_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id) if item["case_id"] == case_id]

    def list_unsettled(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id)
                if item["status"] in {"prepared", "dispatching"}]


class MemoryInvocations:
    def __init__(self, lock: RLock) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = lock

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = validate_invocation({**record, "status": record.get("status", "prepared")})
        with self._lock:
            if stored["id"] in self._rows:
                raise RunConflictError(f"invocation already exists: {stored['id']}")
            self._rows[stored["id"]] = deepcopy(stored)
        return deepcopy(stored)

    def get(self, invocation_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._rows.get(invocation_id))

    def transition(
        self, invocation_id: str, *, expected_revision: int, expected_status: str,
        status: str, changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            current = self._rows.get(invocation_id)
            if current is None:
                raise RunConflictError(f"invocation missing: {invocation_id}")
            if current.get("revision") != expected_revision or current.get("status") != expected_status:
                raise RunConflictError(f"invocation revision or status changed: {invocation_id}")
            if status not in INVOCATION_TRANSITIONS.get(expected_status, set()):
                raise ValueError(f"invalid invocation transition: {expected_status} -> {status}")
            stored = validate_invocation({
                **current, **deepcopy(changes or {}), "status": status,
                "revision": expected_revision + 1,
            })
            self._rows[invocation_id] = deepcopy(stored)
        return deepcopy(stored)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([row for row in self._rows.values() if row["run_id"] == run_id])

    def list_for_case(self, run_id: str, case_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id) if item["case_id"] == case_id]

    def list_unsettled(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id)
                if item["status"] in {"prepared", "dispatching"}]
