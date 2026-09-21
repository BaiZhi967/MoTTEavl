"""Experiment/Cell 存储（M6-T05）。

- Spec 按 (experiment_id, version) 不可变：同内容幂等、异内容冲突。
- Cell 按 cell_id 唯一：分配走 ``claim_cell``（pending→allocating 原子）→
  创建 Run → ``complete_cell``；崩溃窗口由 ``reset_allocating`` 恢复
  （调用方先确认 Run 不存在，dispatch 后未知不自动重放）。
- Memory 与 SQLite 语义一致；PostgreSQL 实现在 pg_audit_store。
- 全部 SQL 为静态字面量 + 参数绑定；无任何拼接。
"""
from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy
from threading import RLock
from typing import Any

from .run_store import _connect

_SPEC_FIELDS = ("experiment_id", "version")
_CELL_FIELDS = ("cell_id", "experiment_id", "experiment_version", "allocation_status")

_SELECT_SPEC = "SELECT payload FROM experiment_specs WHERE experiment_id = ? AND version = ?"
_INSERT_SPEC = "INSERT INTO experiment_specs(experiment_id, version, payload) VALUES (?, ?, ?)"
_SELECT_SPECS_ALL = "SELECT payload FROM experiment_specs ORDER BY experiment_id, version"
_SELECT_SPECS_ONE = "SELECT payload FROM experiment_specs WHERE experiment_id = ? ORDER BY version"
_SELECT_CELL = "SELECT payload FROM experiment_cells WHERE cell_id = ?"
_INSERT_CELL = "INSERT INTO experiment_cells(cell_id, experiment_id, experiment_version, allocation_status, payload) VALUES (?, ?, ?, ?, ?)"
_UPDATE_CELL = "UPDATE experiment_cells SET allocation_status = ?, payload = ? WHERE cell_id = ?"
_SELECT_CELLS_ALL = "SELECT payload FROM experiment_cells WHERE experiment_id = ? ORDER BY rowid"
_SELECT_CELLS_VERSIONED = "SELECT payload FROM experiment_cells WHERE experiment_id = ? AND experiment_version = ? ORDER BY rowid"
_SPEC_TABLE_SQL = "CREATE TABLE IF NOT EXISTS experiment_specs (experiment_id TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (experiment_id, version))"
_CELL_TABLE_SQL = "CREATE TABLE IF NOT EXISTS experiment_cells (cell_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, experiment_version TEXT NOT NULL, allocation_status TEXT NOT NULL, payload TEXT NOT NULL)"
_CELL_INDEX_SQL = "CREATE INDEX IF NOT EXISTS experiment_cells_experiment_idx ON experiment_cells(experiment_id, experiment_version)"


def _validate_spec(payload: dict[str, Any]) -> dict[str, Any]:
    for field in _SPEC_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"experiment spec requires a nonempty string {field}")
    return deepcopy(payload)


def _validate_cell(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("allocation_status", "pending")
    for field in _CELL_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"experiment cell requires a nonempty string {field}")
    if not isinstance(payload.get("repeat_index"), int):
        raise ValueError("experiment cell requires integer repeat_index")
    if not isinstance(payload.get("factor_assignment"), dict):
        raise ValueError("experiment cell requires a factor_assignment object")
    return deepcopy(payload)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


class MemoryExperiments:
    def __init__(self, lock: RLock | None = None) -> None:
        self._lock = lock or RLock()
        self._specs: dict[tuple[str, str], dict[str, Any]] = {}
        self._cells: dict[str, dict[str, Any]] = {}

    def put_spec(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = _validate_spec(payload)
        key = (spec["experiment_id"], spec["version"])
        with self._lock:
            existing = self._specs.get(key)
            if existing is not None:
                if existing != spec:
                    raise ValueError(
                        "experiment specs are immutable: "
                        + spec["experiment_id"] + "@" + spec["version"]
                    )
                return deepcopy(existing)
            self._specs[key] = spec
            return deepcopy(spec)

    def get_spec(self, experiment_id: str, version: str) -> dict[str, Any] | None:
        with self._lock:
            spec = self._specs.get((experiment_id, version))
            return deepcopy(spec) if spec is not None else None

    def list_specs(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            specs = [
                deepcopy(spec) for (spec_id, _), spec in self._specs.items()
                if experiment_id is None or spec_id == experiment_id
            ]
        return sorted(
            specs, key=lambda item: (item["experiment_id"], item["version"]),
        )

    def put_cell(self, payload: dict[str, Any]) -> dict[str, Any]:
        cell = _validate_cell(payload)
        cell.setdefault("allocation_status", "pending")
        with self._lock:
            existing = self._cells.get(cell["cell_id"])
            if existing is not None:
                if existing != cell:
                    raise ValueError("cell content conflict: " + cell["cell_id"])
                return deepcopy(existing)
            self._cells[cell["cell_id"]] = cell
            return deepcopy(cell)

    def get_cell(self, cell_id: str) -> dict[str, Any] | None:
        with self._lock:
            cell = self._cells.get(cell_id)
            return deepcopy(cell) if cell is not None else None

    def list_cells(
        self, experiment_id: str, version: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            cells = [
                deepcopy(cell) for cell in self._cells.values()
                if cell["experiment_id"] == experiment_id
                and (version is None or cell["experiment_version"] == version)
            ]
        return sorted(
            cells,
            key=lambda item: (
                item.get("repeat_index", 0), _dumps(item.get("factor_assignment") or {}),
            ),
        )

    def claim_cell(self, cell_id: str) -> bool:
        """pending → allocating（锁内原子）；已被领取/完成返回 False。"""
        with self._lock:
            cell = self._cells.get(cell_id)
            if cell is None:
                raise KeyError(cell_id)
            if cell.get("allocation_status") != "pending":
                return False
            cell["allocation_status"] = "allocating"
            return True

    def _transition(self, cell_id: str, mutate) -> dict[str, Any]:
        with self._lock:
            cell = self._cells.get(cell_id)
            if cell is None:
                raise KeyError(cell_id)
            mutate(cell)
            return deepcopy(cell)

    def complete_cell(self, cell_id: str, run_id: str) -> dict[str, Any]:
        def mutate(cell: dict[str, Any]) -> None:
            if cell.get("allocation_status") != "allocating":
                raise ValueError(
                    "cell " + cell_id + " is " + repr(cell.get("allocation_status"))
                    + ", expected 'allocating'"
                )
            cell["allocation_status"] = "allocated"
            cell["run_id"] = run_id

        return self._transition(cell_id, mutate)

    def fail_cell(self, cell_id: str, reason: str) -> dict[str, Any]:
        def mutate(cell: dict[str, Any]) -> None:
            if cell.get("allocation_status") != "allocating":
                raise ValueError(
                    "cell " + cell_id + " is " + repr(cell.get("allocation_status"))
                    + ", expected 'allocating'"
                )
            cell["allocation_status"] = "failed"
            cell["failure_reason"] = reason

        return self._transition(cell_id, mutate)

    def cancel_cell(self, cell_id: str) -> dict[str, Any]:
        def mutate(cell: dict[str, Any]) -> None:
            if cell.get("allocation_status") not in ("pending", "allocating"):
                raise ValueError(
                    "cell " + cell_id + " is " + repr(cell.get("allocation_status"))
                    + "; only pending/allocating cells can be cancelled"
                )
            cell["allocation_status"] = "cancelled"

        return self._transition(cell_id, mutate)

    def reset_allocating(self, cell_id: str) -> bool:
        """恢复：allocating → pending（调用方必须先确认没有已创建的 Run）。"""
        with self._lock:
            cell = self._cells.get(cell_id)
            if cell is None:
                raise KeyError(cell_id)
            if cell.get("allocation_status") != "allocating":
                return False
            cell["allocation_status"] = "pending"
            return True

    def record_superseding(self, cell_id: str, run_id: str) -> dict[str, Any]:
        """显式 retry 的 superseding 子 Run 记录（原 initial Run 不消失）。"""
        def mutate(cell: dict[str, Any]) -> None:
            superseding = list(cell.get("superseding_run_ids") or ())
            if run_id not in superseding:
                superseding.append(run_id)
            cell["superseding_run_ids"] = superseding

        return self._transition(cell_id, mutate)


class SQLiteExperiments:
    def __init__(self, path: str) -> None:
        self._path = path
        with closing(_connect(path)) as connection:
            connection.execute(_SPEC_TABLE_SQL)
            connection.execute(_CELL_TABLE_SQL)
            connection.execute(_CELL_INDEX_SQL)
            connection.commit()

    def put_spec(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = _validate_spec(payload)
        payload_json = _dumps(spec)
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _SELECT_SPEC, (spec["experiment_id"], spec["version"]),
            ).fetchone()
            if row is not None:
                stored = json.loads(row[0])
                if stored != spec:
                    raise ValueError(
                        "experiment specs are immutable: "
                        + spec["experiment_id"] + "@" + spec["version"]
                    )
                connection.rollback()
                return stored
            connection.execute(
                _INSERT_SPEC,
                (spec["experiment_id"], spec["version"], payload_json),
            )
            connection.commit()
        return deepcopy(spec)

    def get_spec(self, experiment_id: str, version: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(_SELECT_SPEC, (experiment_id, version)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def list_specs(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            if experiment_id is None:
                rows = connection.execute(_SELECT_SPECS_ALL).fetchall()
            else:
                rows = connection.execute(_SELECT_SPECS_ONE, (experiment_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put_cell(self, payload: dict[str, Any]) -> dict[str, Any]:
        cell = _validate_cell(payload)
        cell.setdefault("allocation_status", "pending")
        payload_json = _dumps(cell)
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(_SELECT_CELL, (cell["cell_id"],)).fetchone()
            if row is not None:
                stored = json.loads(row[0])
                if stored != cell:
                    raise ValueError("cell content conflict: " + cell["cell_id"])
                connection.rollback()
                return stored
            connection.execute(
                _INSERT_CELL,
                (
                    cell["cell_id"], cell["experiment_id"],
                    cell["experiment_version"], cell["allocation_status"], payload_json,
                ),
            )
            connection.commit()
        return deepcopy(cell)

    def get_cell(self, cell_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(_SELECT_CELL, (cell_id,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def list_cells(
        self, experiment_id: str, version: str | None = None,
    ) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            if version is None:
                rows = connection.execute(_SELECT_CELLS_ALL, (experiment_id,)).fetchall()
            else:
                rows = connection.execute(
                    _SELECT_CELLS_VERSIONED, (experiment_id, version),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _mutate_cell(self, cell_id: str, mutate) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(_SELECT_CELL, (cell_id,)).fetchone()
            if row is None:
                raise KeyError(cell_id)
            cell = json.loads(row[0])
            mutate(cell)
            connection.execute(
                _UPDATE_CELL,
                (cell.get("allocation_status", "pending"), _dumps(cell), cell_id),
            )
            connection.commit()
            return cell

    def claim_cell(self, cell_id: str) -> bool:
        """pending → allocating（BEGIN IMMEDIATE 下原子；非 pending 返回 False）。"""
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(_SELECT_CELL, (cell_id,)).fetchone()
            if row is None:
                raise KeyError(cell_id)
            cell = json.loads(row[0])
            if cell.get("allocation_status") != "pending":
                connection.rollback()
                return False
            cell["allocation_status"] = "allocating"
            connection.execute(_UPDATE_CELL, ("allocating", _dumps(cell), cell_id))
            connection.commit()
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
