"""PostgreSQL M6 仓库（M6-T03/T05）：与 Memory/SQLite 同语义。

PgExperiments / PgGateStore / PgBaselineStore 分别对应
motte_storage.experiments / gate_store / baseline_store 的契约；不可变、
同内容幂等、异内容冲突、cell 分配状态机与默认指针 CAS 语义一致。
表结构由 Alembic 0014 创建。语句全部内联字面量 + %s 参数绑定。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from psycopg.types.json import Json

from .baseline_store import BaselineConflict
from .pg_audit_store import _as_payload, _connect


def _dumps(value: dict[str, Any]) -> dict[str, Any]:
    return Json(value)


class PgExperiments:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    # -- spec ---------------------------------------------------------------

    def put_spec(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = deepcopy(payload)
        experiment_id = spec["experiment_id"]
        version = spec["version"]
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM experiment_specs WHERE experiment_id = %s AND version = %s",
                    (experiment_id, version),
                )
                row = cursor.fetchone()
                if row is not None:
                    stored = _as_payload(row[0])
                    if stored != spec:
                        raise ValueError(
                            "experiment specs are immutable: "
                            + experiment_id + "@" + version
                        )
                    return stored
                cursor.execute(
                    "INSERT INTO experiment_specs(experiment_id, version, payload) VALUES (%s, %s, %s)",
                    (experiment_id, version, _dumps(spec)),
                )
        return deepcopy(spec)

    def get_spec(self, experiment_id: str, version: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM experiment_specs WHERE experiment_id = %s AND version = %s",
                    (experiment_id, version),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def list_specs(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                if experiment_id is None:
                    cursor.execute(
                        "SELECT payload FROM experiment_specs ORDER BY experiment_id, version",
                    )
                else:
                    cursor.execute(
                        "SELECT payload FROM experiment_specs WHERE experiment_id = %s ORDER BY version",
                        (experiment_id,),
                    )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]

    # -- cells --------------------------------------------------------------

    def put_cell(self, payload: dict[str, Any]) -> dict[str, Any]:
        cell = deepcopy(payload)
        cell.setdefault("allocation_status", "pending")
        cell_id = cell["cell_id"]
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM experiment_cells WHERE cell_id = %s",
                    (cell_id,),
                )
                row = cursor.fetchone()
                if row is not None:
                    stored = _as_payload(row[0])
                    if stored != cell:
                        raise ValueError("cell content conflict: " + cell_id)
                    return stored
                cursor.execute(
                    "INSERT INTO experiment_cells(cell_id, experiment_id, experiment_version, allocation_status, payload) VALUES (%s, %s, %s, %s, %s)",
                    (
                        cell_id, cell["experiment_id"], cell["experiment_version"],
                        cell["allocation_status"], _dumps(cell),
                    ),
                )
        return deepcopy(cell)

    def get_cell(self, cell_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM experiment_cells WHERE cell_id = %s",
                    (cell_id,),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def list_cells(
        self, experiment_id: str, version: str | None = None,
    ) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                if version is None:
                    cursor.execute(
                        "SELECT payload FROM experiment_cells WHERE experiment_id = %s ORDER BY position",
                        (experiment_id,),
                    )
                else:
                    cursor.execute(
                        "SELECT payload FROM experiment_cells WHERE experiment_id = %s AND experiment_version = %s ORDER BY position",
                        (experiment_id, version),
                    )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]

    def _mutate_cell(self, cell_id: str, mutate) -> dict[str, Any]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM experiment_cells WHERE cell_id = %s FOR UPDATE",
                    (cell_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(cell_id)
                cell = _as_payload(row[0])
                mutate(cell)
                cursor.execute(
                    "UPDATE experiment_cells SET allocation_status = %s, payload = %s WHERE cell_id = %s",
                    (cell.get("allocation_status", "pending"), _dumps(cell), cell_id),
                )
                return cell

    def claim_cell(self, cell_id: str) -> bool:
        """pending → allocating（FOR UPDATE 行锁下原子；非 pending 返回 False）。"""
        def mutate(cell: dict[str, Any]) -> bool:
            if cell.get("allocation_status") != "pending":
                return False
            cell["allocation_status"] = "allocating"
            return True

        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM experiment_cells WHERE cell_id = %s FOR UPDATE",
                    (cell_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(cell_id)
                cell = _as_payload(row[0])
                if not mutate(cell):
                    return False
                cursor.execute(
                    "UPDATE experiment_cells SET allocation_status = %s, payload = %s WHERE cell_id = %s",
                    ("allocating", _dumps(cell), cell_id),
                )
                return True

    def complete_cell(self, cell_id: str, run_id: str) -> dict[str, Any]:
        def mutate(cell: dict[str, Any]) -> None:
            if cell.get("allocation_status") != "allocating":
                raise ValueError(
                    "cell " + cell_id + " is " + repr(cell.get("allocation_status"))
                    + ", expected 'allocating'"
                )
            cell["allocation_status"] = "allocated"
            cell["run_id"] = run_id

        return self._mutate_cell(cell_id, mutate)

    def fail_cell(self, cell_id: str, reason: str) -> dict[str, Any]:
        def mutate(cell: dict[str, Any]) -> None:
            if cell.get("allocation_status") != "allocating":
                raise ValueError(
                    "cell " + cell_id + " is " + repr(cell.get("allocation_status"))
                    + ", expected 'allocating'"
                )
            cell["allocation_status"] = "failed"
            cell["failure_reason"] = reason

        return self._mutate_cell(cell_id, mutate)

    def cancel_cell(self, cell_id: str) -> dict[str, Any]:
        def mutate(cell: dict[str, Any]) -> None:
            if cell.get("allocation_status") not in ("pending", "allocating"):
                raise ValueError(
                    "cell " + cell_id + " is " + repr(cell.get("allocation_status"))
                    + "; only pending/allocating cells can be cancelled"
                )
            cell["allocation_status"] = "cancelled"

        return self._mutate_cell(cell_id, mutate)

    def reset_allocating(self, cell_id: str) -> bool:
        outcome = {"reset": False}

        def mutate(cell: dict[str, Any]) -> None:
            if cell.get("allocation_status") != "allocating":
                raise _NoTransition()
            cell["allocation_status"] = "pending"
            outcome["reset"] = True

        try:
            self._mutate_cell(cell_id, mutate)
        except _NoTransition:
            return False
        return outcome["reset"]

    def record_superseding(self, cell_id: str, run_id: str) -> dict[str, Any]:
        def mutate(cell: dict[str, Any]) -> None:
            superseding = list(cell.get("superseding_run_ids") or ())
            if run_id not in superseding:
                superseding.append(run_id)
            cell["superseding_run_ids"] = superseding

        return self._mutate_cell(cell_id, mutate)


class _NoTransition(Exception):
    """内部哨兵：状态不满足转移条件且不需要报错。"""


class PgGateStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def put_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        policy = deepcopy(payload)
        policy_id = policy["policy_id"]
        version = policy["version"]
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM gate_policies WHERE policy_id = %s AND version = %s",
                    (policy_id, version),
                )
                row = cursor.fetchone()
                if row is not None:
                    stored = _as_payload(row[0])
                    if stored != policy:
                        raise ValueError(
                            "gate policies are immutable: " + policy_id + "@" + version
                        )
                    return stored
                cursor.execute(
                    "INSERT INTO gate_policies(policy_id, version, payload) VALUES (%s, %s, %s)",
                    (policy_id, version, _dumps(policy)),
                )
        return deepcopy(policy)

    def get_policy(self, policy_id: str, version: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM gate_policies WHERE policy_id = %s AND version = %s",
                    (policy_id, version),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def list_policies(self, policy_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                if policy_id is None:
                    cursor.execute(
                        "SELECT payload FROM gate_policies ORDER BY policy_id, version",
                    )
                else:
                    cursor.execute(
                        "SELECT payload FROM gate_policies WHERE policy_id = %s ORDER BY version",
                        (policy_id,),
                    )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]

    def deprecate_policy(self, policy_id: str, version: str) -> dict[str, Any]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM gate_policies WHERE policy_id = %s AND version = %s FOR UPDATE",
                    (policy_id, version),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(policy_id + "@" + version)
                policy = _as_payload(row[0])
                if policy.get("lifecycle") != "deprecated":
                    policy["lifecycle"] = "deprecated"
                    cursor.execute(
                        "UPDATE gate_policies SET payload = %s WHERE policy_id = %s AND version = %s",
                        (_dumps(policy), policy_id, version),
                    )
                return policy

    def put_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(payload)
        result_id = result["gate_result_id"]
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM gate_results WHERE gate_result_id = %s",
                    (result_id,),
                )
                row = cursor.fetchone()
                if row is not None:
                    stored = _as_payload(row[0])
                    if stored != result:
                        raise ValueError("gate results are append-only: " + result_id)
                    return stored
                cursor.execute(
                    "INSERT INTO gate_results(gate_result_id, policy_id, payload) VALUES (%s, %s, %s)",
                    (result_id, result["policy_id"], _dumps(result)),
                )
        return deepcopy(result)

    def get_result(self, gate_result_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM gate_results WHERE gate_result_id = %s",
                    (gate_result_id,),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def list_results(
        self, policy_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                if policy_id is None:
                    cursor.execute(
                        "SELECT payload FROM gate_results ORDER BY position DESC LIMIT %s",
                        (limit,),
                    )
                else:
                    cursor.execute(
                        "SELECT payload FROM gate_results WHERE policy_id = %s ORDER BY position DESC LIMIT %s",
                        (policy_id, limit),
                    )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]


class PgBaselineStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def put(self, payload: dict[str, Any]) -> dict[str, Any]:
        from motte_contracts.comparison import BaselineSnapshot

        BaselineSnapshot.model_validate(payload)
        snapshot = deepcopy(payload)
        baseline_id = snapshot["baseline_id"]
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM m6_baselines WHERE baseline_id = %s",
                    (baseline_id,),
                )
                row = cursor.fetchone()
                if row is not None:
                    stored = _as_payload(row[0])
                    if stored != snapshot:
                        raise BaselineConflict(
                            "BASELINE_IMMUTABLE",
                            "baseline snapshots are immutable: " + baseline_id,
                        )
                    return stored
                cursor.execute(
                    "INSERT INTO m6_baselines(baseline_id, payload) VALUES (%s, %s)",
                    (baseline_id, _dumps(snapshot)),
                )
        return deepcopy(snapshot)

    def get(self, baseline_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM m6_baselines WHERE baseline_id = %s",
                    (baseline_id,),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM m6_baselines ORDER BY position DESC LIMIT %s",
                    (limit,),
                )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]

    def set_default(
        self, pointer: dict[str, Any], *, expected_current: str | None = None,
    ) -> dict[str, Any]:
        pointer = deepcopy(pointer)
        scope = pointer["scope"]
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM default_baselines WHERE scope = %s FOR UPDATE",
                    (scope,),
                )
                row = cursor.fetchone()
                current = _as_payload(row[0]) if row is not None else None
                if expected_current is not None:
                    current_id = current.get("baseline_id") if current else None
                    if current_id != expected_current:
                        raise BaselineConflict(
                            "CAS_CONFLICT",
                            "default baseline for scope " + scope + " is "
                            + repr(current_id) + ", expected "
                            + repr(expected_current),
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
                    return current
                pointer["position"] = (
                    (current.get("position", 0) + 1) if current else 1
                )
                payload_json = _dumps(pointer)
                cursor.execute(
                    "INSERT INTO default_baselines(scope, baseline_id, position, payload) VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (scope) DO UPDATE SET baseline_id = EXCLUDED.baseline_id, position = EXCLUDED.position, payload = EXCLUDED.payload",
                    (scope, pointer["baseline_id"], pointer["position"], payload_json),
                )
                cursor.execute(
                    "INSERT INTO default_baseline_history(scope, position, payload) VALUES (%s, %s, %s)",
                    (scope, pointer["position"], payload_json),
                )
                return pointer

    def get_default(self, scope: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM default_baselines WHERE scope = %s",
                    (scope,),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def default_history(self, scope: str, limit: int = 20) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM default_baseline_history WHERE scope = %s ORDER BY position DESC LIMIT %s",
                    (scope, limit),
                )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in reversed(rows)]
