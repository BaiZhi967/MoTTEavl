"""Baseline 快照存储（M6-T03 Lite）。

固定 Run+ScoringPass 的指标快照；写入后不可变（同 id 再写是错误，不是
覆盖）。current pass 漂移不影响已固定的 baseline 引用。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from threading import RLock
from typing import Any

from .run_store import _connect

_BASELINE_FIELDS = ("id", "run_id", "scoring_pass_id", "metrics")


def _validate(snapshot: dict[str, Any]) -> dict[str, Any]:
    for field in _BASELINE_FIELDS:
        if field == "metrics":
            if not isinstance(snapshot.get(field), dict):
                raise ValueError("baseline metrics must be an object")
        elif not isinstance(snapshot.get(field), str) or not snapshot[field]:
            raise ValueError("baseline requires a nonempty string " + field)
    return deepcopy(snapshot)


class MemoryBaselines:
    def __init__(self, lock: RLock | None = None) -> None:
        self._lock = lock or RLock()
        self._snapshots: dict[str, dict[str, Any]] = {}

    def put(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        stored = _validate(snapshot)
        stored.setdefault("created_at", "")
        with self._lock:
            if stored["id"] in self._snapshots:
                raise ValueError(
                    "baseline snapshots are immutable: " + stored["id"],
                )
            self._snapshots[stored["id"]] = stored
            return deepcopy(stored)

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            return deepcopy(snapshot) if snapshot is not None else None

    def get_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                item for item in self._snapshots.values() if item.get("run_id") == run_id
            ])


class SQLiteBaselines:
    def __init__(self, path: str) -> None:
        self._path = path
        with closing(_connect(path)) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS baseline_snapshots (
                  id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  scoring_pass_id TEXT NOT NULL,
                  metrics TEXT NOT NULL,
                  payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS baseline_run_idx ON baseline_snapshots(run_id);
            """)

    def put(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        stored = _validate(snapshot)
        stored.setdefault("created_at", "")
        snapshot_id = str(stored.get("id"))
        run_id = str(stored.get("run_id"))
        pass_id = str(stored.get("scoring_pass_id"))
        metrics_json = json.dumps(stored.get("metrics"), sort_keys=True)
        payload_json = json.dumps(stored, sort_keys=True)
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM baseline_snapshots WHERE id = ?", (snapshot_id,),
            ).fetchone()
            if row is not None:
                raise ValueError("baseline snapshots are immutable: " + snapshot_id)
            connection.execute(
                "INSERT INTO baseline_snapshots(id, run_id, scoring_pass_id, metrics, payload) VALUES (?, ?, ?, ?, ?)",
                (snapshot_id, run_id, pass_id, metrics_json, payload_json),
            )
        return deepcopy(stored)

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM baseline_snapshots WHERE id = ?", (snapshot_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def get_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM baseline_snapshots WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
