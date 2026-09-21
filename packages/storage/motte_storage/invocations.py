"""Persistent agent invocation boundaries: prepared -> dispatching -> settled.

每次模型/工具调用的持久边界。这是 CaseAttempt 之下的证据与恢复判定，
不是第二套 Run 状态机：settled 需要 outcome；未 settled 的 dispatching
记录表示副作用可能已发生（indeterminate）。

M5-T09b 扩展（保持向后兼容）：
- purpose：subject（默认）或 judge。judge 调用必须有 job_id，费用与 subject
  分开核算。
- owner：经过校验的 subject-or-calibration owner union。subject 使用真实
  run_id/case_id；calibration 样本归属 calibration:<calibration_job_id>，
  显式命名空间而不是伪造 Run/CaseAttempt，且 run_id/case_id 列仍保持 NOT NULL。
- 事务内写入助手（create_invocation_in_transaction 等）让 ScoringJob 能把
  prepared/dispatching 边界与 job 状态放进同一个事务。
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

INVOCATION_PURPOSES = ("subject", "judge")
INVOCATION_OWNER_KINDS = ("subject", "calibration")

#: 契约之外的存储层扩展字段（不进 InvocationRecord 的 strict 校验）。
_EXTENSION_FIELDS = ("revision", "purpose", "owner", "job_id", "scoring_pass_id",
                     "criterion_id")


def _validate_owner(
    owner: dict[str, Any], validated: dict[str, Any], purpose: str,
) -> dict[str, Any]:
    if not isinstance(owner, dict):
        raise ValueError("invocation owner must be an object")
    kind = owner.get("kind")
    if kind not in INVOCATION_OWNER_KINDS:
        raise ValueError(f"unknown invocation owner kind: {kind!r}")
    run_id = validated["run_id"]
    case_id = validated["case_id"]
    if kind == "subject":
        if owner.get("run_id") not in (None, run_id):
            raise ValueError("subject invocation owner must match the run")
        if run_id.startswith("calibration:"):
            raise ValueError("subject invocation cannot use a calibration owner reference")
        normalized = {
            "kind": "subject",
            "run_id": run_id,
            "case_id": case_id,
            "attempt_id": owner.get("attempt_id") or validated.get("attempt_id"),
        }
    else:
        calibration_job_id = owner.get("calibration_job_id")
        sample_id = owner.get("sample_id")
        if not isinstance(calibration_job_id, str) or not calibration_job_id:
            raise ValueError("calibration owner requires calibration_job_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError("calibration owner requires sample_id")
        expected = f"calibration:{calibration_job_id}"
        if run_id != expected:
            raise ValueError(
                "calibration invocation must use the namespaced owner reference "
                f"{expected!r}, never a fabricated Run"
            )
        if case_id != sample_id:
            raise ValueError(
                "calibration invocation case_id must be the calibration sample id"
            )
        if validated["kind"] != "model":
            raise ValueError("calibration invocations must be model calls")
        if purpose != "judge":
            raise ValueError("calibration invocations must be judge-purpose calls")
        normalized = {
            "kind": "calibration",
            "calibration_job_id": calibration_job_id,
            "sample_id": sample_id,
            "run_id": run_id,
            "case_id": case_id,
        }
    return normalized


def validate_invocation(record: dict[str, Any]) -> dict[str, Any]:
    """契约校验 + 存储层扩展校验；扩展字段在契约验证前剥离。"""
    from motte_contracts.evaluation import InvocationRecord

    contract_view = {
        key: value for key, value in record.items() if key not in _EXTENSION_FIELDS
    }
    try:
        validated = InvocationRecord.model_validate(contract_view).model_dump(mode="json")
    except Exception as error:  # noqa: BLE001 - 统一转存储层 ValueError
        raise ValueError(f"invocation does not match the contract: {error}") from error

    purpose = record.get("purpose", "subject")
    if purpose not in INVOCATION_PURPOSES:
        raise ValueError(f"unknown invocation purpose: {purpose!r}")
    owner = record.get("owner")
    if owner is None:
        owner = {"kind": "subject", "run_id": validated["run_id"]}
    validated["owner"] = _validate_owner(owner, validated, purpose)
    validated["purpose"] = purpose
    job_id = record.get("job_id")
    if purpose == "judge":
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("judge invocations must carry the scoring job id")
        validated["job_id"] = job_id
    elif job_id is not None:
        raise ValueError("only judge invocations may carry a scoring job id")
    scoring_pass_id = record.get("scoring_pass_id")
    if scoring_pass_id is not None and not isinstance(scoring_pass_id, str):
        raise ValueError("scoring_pass_id must be a string when provided")
    validated["scoring_pass_id"] = scoring_pass_id
    criterion_id = record.get("criterion_id")
    if criterion_id is not None and not isinstance(criterion_id, str):
        raise ValueError("criterion_id must be a string when provided")
    validated["criterion_id"] = criterion_id
    if "revision" in record:
        validated["revision"] = record["revision"]
    return validated


def create_invocation_in_transaction(
    connection: sqlite3.Connection, record: dict[str, Any],
) -> dict[str, Any]:
    """在调用方事务内写入 prepared invocation（不做 BEGIN/COMMIT）。"""
    stored = validate_invocation({**record, "status": record.get("status", "prepared")})
    stored.setdefault("revision", 1)
    try:
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


def transition_invocation_in_transaction(
    connection: sqlite3.Connection, invocation_id: str, *, expected_revision: int,
    expected_status: str, status: str, changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在调用方事务内做 CAS 状态迁移。"""
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
    stored = validate_invocation({
        **current, **deepcopy(changes or {}), "status": status,
        "revision": expected_revision + 1,
    })
    if connection.execute(
        "UPDATE agent_invocations SET payload = ?, status = ?, revision = ? "
        "WHERE id = ? AND status = ? AND revision = ?",
        (json.dumps(stored, sort_keys=True), status, stored["revision"],
         invocation_id, expected_status, expected_revision),
    ).rowcount != 1:
        raise RunConflictError(f"invocation revision or status changed: {invocation_id}")
    return deepcopy(stored)


def create_invocation_pg(cursor: Any, record: dict[str, Any]) -> dict[str, Any]:
    """PostgreSQL 事务内的 prepared invocation 写入（psycopg cursor）。"""
    from psycopg.types.json import Json

    stored = validate_invocation({**record, "status": record.get("status", "prepared")})
    stored.setdefault("revision", 1)
    cursor.execute(
        "INSERT INTO agent_invocations(id, run_id, case_id, kind, step, status, "
        "revision, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (stored["id"], stored["run_id"], stored["case_id"], stored["kind"],
         stored["step"], stored["status"], stored["revision"], Json(stored)),
    )
    return deepcopy(stored)


def transition_invocation_pg(
    cursor: Any, invocation_id: str, *, expected_revision: int, expected_status: str,
    status: str, changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """PostgreSQL 事务内的 CAS 状态迁移（psycopg cursor）。"""
    from psycopg.types.json import Json

    cursor.execute(
        "SELECT payload FROM agent_invocations WHERE id = %s FOR UPDATE",
        (invocation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise RunConflictError(f"invocation missing: {invocation_id}")
    current = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    if current.get("revision") != expected_revision or current.get("status") != expected_status:
        raise RunConflictError(f"invocation revision or status changed: {invocation_id}")
    if status not in INVOCATION_TRANSITIONS.get(expected_status, set()):
        raise ValueError(f"invalid invocation transition: {expected_status} -> {status}")
    stored = validate_invocation({
        **current, **deepcopy(changes or {}), "status": status,
        "revision": expected_revision + 1,
    })
    cursor.execute(
        "UPDATE agent_invocations SET payload = %s, status = %s, revision = %s "
        "WHERE id = %s AND status = %s AND revision = %s",
        (Json(stored), status, stored["revision"], invocation_id,
         expected_status, expected_revision),
    )
    if cursor.rowcount != 1:
        raise RunConflictError(f"invocation revision or status changed: {invocation_id}")
    return deepcopy(stored)


class SQLiteInvocations:
    def __init__(self, path: str) -> None:
        self._path = path

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return create_invocation_in_transaction(connection, record)

    def create_within(
        self, connection: sqlite3.Connection, record: dict[str, Any],
    ) -> dict[str, Any]:
        return create_invocation_in_transaction(connection, record)

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
            return transition_invocation_in_transaction(
                connection, invocation_id, expected_revision=expected_revision,
                expected_status=expected_status, status=status, changes=changes,
            )

    def transition_within(
        self, connection: sqlite3.Connection, invocation_id: str, *,
        expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return transition_invocation_in_transaction(
            connection, invocation_id, expected_revision=expected_revision,
            expected_status=expected_status, status=status, changes=changes,
        )

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_invocations WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_for_case(self, run_id: str, case_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id) if item["case_id"] == case_id]

    def list_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_invocations "
                "WHERE json_extract(payload, '$.job_id') = ? ORDER BY rowid",
                (job_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_unsettled(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id)
                if item["status"] in {"prepared", "dispatching"}]


class MemoryInvocations:
    def __init__(self, lock: RLock) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = lock

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = validate_invocation({**record, "status": record.get("status", "prepared")})
        stored.setdefault("revision", 1)
        with self._lock:
            if stored["id"] in self._rows:
                raise RunConflictError(f"invocation already exists: {stored['id']}")
            self._rows[stored["id"]] = deepcopy(stored)
        return deepcopy(stored)

    def create_within(self, _connection: Any, record: dict[str, Any]) -> dict[str, Any]:
        return self.create(record)

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

    def transition_within(
        self, connection: Any, invocation_id: str, *, expected_revision: int,
        expected_status: str, status: str, changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.transition(
            invocation_id, expected_revision=expected_revision,
            expected_status=expected_status, status=status, changes=changes,
        )

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([row for row in self._rows.values() if row["run_id"] == run_id])

    def list_for_case(self, run_id: str, case_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id) if item["case_id"] == case_id]

    def list_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                row for row in self._rows.values() if row.get("job_id") == job_id
            ])

    def list_unsettled(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id)
                if item["status"] in {"prepared", "dispatching"}]
