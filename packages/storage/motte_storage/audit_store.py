"""Append-only scoring, case attempts and delivery commands for local stores."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from threading import RLock
from typing import Any
from uuid import uuid4

from .integrity import (
    ATTEMPT_TRANSITIONS,
    COMMAND_TRANSITIONS,
    RunConflictError,
    advance_record,
    new_record,
    next_run,
    stored_run,
    validate_event,
    validate_scores,
)
from .run_store import _append_event, _connect


def _pass_record(record: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(record)
    result.setdefault("id", f"pass-{uuid4().hex}")
    if not isinstance(result["id"], str) or not result["id"]:
        raise ValueError("scoring pass needs a nonempty id")
    if not isinstance(result.get("run_id"), str) or not result["run_id"]:
        raise ValueError("scoring pass needs a nonempty run_id")
    return result


class SQLiteAttempts:
    def __init__(self, path: str) -> None:
        self._path = path

    def begin(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = new_record(record, "attempt", "prepared")
        if not isinstance(stored.get("case_id"), str) or not stored["case_id"]:
            raise ValueError("attempt needs a nonempty case_id")
        stored.setdefault("attempt_no", 1)
        if type(stored["attempt_no"]) is not int or stored["attempt_no"] < 1:
            raise ValueError("attempt_no must be a positive integer")
        try:
            with closing(_connect(self._path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO case_attempts(id, run_id, case_id, attempt_no, status, revision, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (stored["id"], stored["run_id"], stored["case_id"], stored["attempt_no"],
                     stored["status"], stored["revision"], json.dumps(stored, sort_keys=True)),
                )
        except sqlite3.IntegrityError as error:
            raise RunConflictError("attempt id or attempt_no already exists") from error
        return deepcopy(stored)

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute("SELECT payload FROM case_attempts WHERE id = ?", (attempt_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM case_attempts WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_open(self, run_id: str) -> list[dict[str, Any]]:
        return [record for record in self.list_for_run(run_id)
                if record["status"] in {"prepared", "dispatching", "indeterminate"}]

    def transition(
        self, attempt_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM case_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RunConflictError(f"attempt missing: {attempt_id}")
            stored = advance_record(
                json.loads(row[0]), expected_revision=expected_revision, expected_status=expected_status,
                status=status, changes=changes, transitions=ATTEMPT_TRANSITIONS,
            )
            if connection.execute(
                "UPDATE case_attempts SET payload = ?, status = ?, revision = ? "
                "WHERE id = ? AND status = ? AND revision = ?",
                (json.dumps(stored, sort_keys=True), status, stored["revision"], attempt_id,
                 expected_status, expected_revision),
            ).rowcount != 1:
                raise RunConflictError(f"attempt revision or status changed: {attempt_id}")
        return deepcopy(stored)

    def complete(
        self, attempt_id: str, *, expected_revision: int, expected_status: str = "dispatching",
        status: str = "succeeded", changes: dict[str, Any] | None = None,
        case_run: dict[str, Any] | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed"}:
            raise ValueError("attempt completion requires succeeded or failed")
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM case_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RunConflictError(f"attempt missing: {attempt_id}")
            previous = json.loads(row[0])
            pending = validate_event(event, previous["run_id"])
            if case_run is not None and (
                case_run.get("run_id") != previous["run_id"]
                or case_run.get("case_id") != previous["case_id"]
            ):
                raise ValueError("case result must match the attempt run and case")
            stored = advance_record(
                previous, expected_revision=expected_revision, expected_status=expected_status,
                status=status, changes=changes, transitions=ATTEMPT_TRANSITIONS,
            )
            if case_run is not None:
                old = connection.execute(
                    "SELECT payload FROM case_runs WHERE run_id = ? AND case_id = ?",
                    (previous["run_id"], previous["case_id"]),
                ).fetchone()
                if old is not None and json.loads(old[0]) != case_run:
                    raise RunConflictError("a different case result is already persisted")
                if old is None:
                    ordinal = connection.execute(
                        "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM case_runs WHERE run_id = ?",
                        (previous["run_id"],),
                    ).fetchone()[0]
                    connection.execute(
                        "INSERT INTO case_runs(run_id, case_id, ordinal, payload) VALUES (?, ?, ?, ?)",
                        (previous["run_id"], previous["case_id"], ordinal,
                         json.dumps(case_run, sort_keys=True)),
                    )
            if connection.execute(
                "UPDATE case_attempts SET payload = ?, status = ?, revision = ? "
                "WHERE id = ? AND status = ? AND revision = ?",
                (json.dumps(stored, sort_keys=True), status, stored["revision"], attempt_id,
                 expected_status, expected_revision),
            ).rowcount != 1:
                raise RunConflictError(f"attempt revision or status changed: {attempt_id}")
            if pending is not None:
                _append_event(connection, pending)
        return deepcopy(stored)

    def mark_indeterminate(self, run_id: str) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT payload FROM case_attempts WHERE run_id = ? AND status = 'dispatching' ORDER BY rowid",
                (run_id,),
            ).fetchall()
            for (payload,) in rows:
                current = json.loads(payload)
                stored = advance_record(
                    current, expected_revision=current["revision"], expected_status="dispatching",
                    status="indeterminate", changes=None, transitions=ATTEMPT_TRANSITIONS,
                )
                connection.execute(
                    "UPDATE case_attempts SET payload = ?, status = ?, revision = ? WHERE id = ?",
                    (json.dumps(stored, sort_keys=True), "indeterminate", stored["revision"], stored["id"]),
                )
                changed.append(stored)
        return changed


class SQLiteScoringPasses:
    def __init__(self, path: str) -> None:
        self._path = path

    def append(
        self, record: dict[str, Any], scores: list[dict[str, Any]], *,
        expected_run_revision: int | None = None, expected_run_status: str | None = None,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stored = _pass_record(record)
        rows = validate_scores(scores)
        pending = validate_event(event, stored["run_id"])
        if (expected_run_revision is None) != (expected_run_status is None):
            raise ValueError("provide both expected_run_revision and expected_run_status")
        try:
            with closing(_connect(self._path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                updated_run = None
                if expected_run_revision is not None:
                    run_row = connection.execute(
                        "SELECT payload, revision FROM runs WHERE id = ?", (stored["run_id"],)
                    ).fetchone()
                    if run_row is None or run_row[1] != expected_run_revision:
                        raise RunConflictError("run revision changed during scoring")
                    run = stored_run(json.loads(run_row[0]), run_row[1])
                    if run.get("status") != expected_run_status:
                        raise RunConflictError("run status changed during scoring")
                    updated_run = next_run(
                        {**run, "current_scoring_pass_id": stored["id"]}, expected_run_revision
                    )
                connection.execute(
                    "INSERT INTO scoring_passes(id, run_id, payload) VALUES (?, ?, ?)",
                    (stored["id"], stored["run_id"], json.dumps(stored, sort_keys=True)),
                )
                connection.executemany(
                    "INSERT INTO score_sets(scoring_pass_id, case_id, ordinal, payload) VALUES (?, ?, ?, ?)",
                    [(stored["id"], row["case_id"], index, json.dumps(row, sort_keys=True))
                     for index, row in enumerate(rows)],
                )
                if updated_run is not None:
                    if connection.execute(
                        "UPDATE runs SET payload = ?, revision = ? WHERE id = ? AND revision = ? "
                        "AND json_extract(payload, '$.status') = ?",
                        (json.dumps(updated_run, sort_keys=True), updated_run["revision"],
                         stored["run_id"], expected_run_revision, expected_run_status),
                    ).rowcount != 1:
                        raise RunConflictError("run revision or status changed during scoring")
                if pending is not None:
                    _append_event(connection, pending)
        except sqlite3.IntegrityError as error:
            raise RunConflictError(f"scoring pass already exists: {stored['id']}") from error
        return deepcopy(stored)

    def get(self, pass_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute("SELECT payload FROM scoring_passes WHERE id = ?", (pass_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM scoring_passes WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def current(self, run_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
            pass_id = json.loads(row[0]).get("current_scoring_pass_id") if row else None
            if pass_id:
                selected = connection.execute(
                    "SELECT payload FROM scoring_passes WHERE id = ? AND run_id = ?", (pass_id, run_id)
                ).fetchone()
            else:
                selected = connection.execute(
                    "SELECT payload FROM scoring_passes WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
        return json.loads(selected[0]) if selected else None


class SQLiteScoreSets:
    def __init__(self, path: str) -> None:
        self._path = path

    def list_for_pass(self, pass_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM score_sets WHERE scoring_pass_id = ? ORDER BY ordinal", (pass_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, pass_id: str, case_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM score_sets WHERE scoring_pass_id = ? AND case_id = ?",
                (pass_id, case_id),
            ).fetchone()
        return json.loads(row[0]) if row else None


class SQLiteCommands:
    def __init__(self, path: str) -> None:
        self._path = path

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = new_record(record, "command", "queued")
        try:
            with closing(_connect(self._path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO run_commands(id, run_id, status, revision, payload) VALUES (?, ?, ?, ?, ?)",
                    (stored["id"], stored["run_id"], stored["status"], stored["revision"],
                     json.dumps(stored, sort_keys=True)),
                )
        except sqlite3.IntegrityError as error:
            raise RunConflictError(f"command already exists: {stored['id']}") from error
        return deepcopy(stored)

    def get(self, command_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute("SELECT payload FROM run_commands WHERE id = ?", (command_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM run_commands WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list(self, run_id: str) -> list[dict[str, Any]]:
        return self.list_for_run(run_id)

    def transition(
        self, command_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM run_commands WHERE id = ?", (command_id,)).fetchone()
            if row is None:
                raise RunConflictError(f"command missing: {command_id}")
            stored = advance_record(
                json.loads(row[0]), expected_revision=expected_revision, expected_status=expected_status,
                status=status, changes=changes, transitions=COMMAND_TRANSITIONS,
            )
            if connection.execute(
                "UPDATE run_commands SET payload = ?, status = ?, revision = ? "
                "WHERE id = ? AND status = ? AND revision = ?",
                (json.dumps(stored, sort_keys=True), status, stored["revision"], command_id,
                 expected_status, expected_revision),
            ).rowcount != 1:
                raise RunConflictError(f"command revision or status changed: {command_id}")
        return deepcopy(stored)


class MemoryAttempts:
    def __init__(self, case_runs: Any, events: Any, lock: RLock) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._cases = case_runs
        self._events = events
        self._lock = lock

    def begin(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = new_record(record, "attempt", "prepared")
        if not isinstance(stored.get("case_id"), str) or not stored["case_id"]:
            raise ValueError("attempt needs a nonempty case_id")
        stored.setdefault("attempt_no", 1)
        if type(stored["attempt_no"]) is not int or stored["attempt_no"] < 1:
            raise ValueError("attempt_no must be a positive integer")
        with self._lock:
            if stored["id"] in self._rows or any(
                item["run_id"] == stored["run_id"] and item["case_id"] == stored["case_id"]
                and item["attempt_no"] == stored["attempt_no"] for item in self._rows.values()
            ):
                raise RunConflictError("attempt id or attempt_no already exists")
            self._rows[stored["id"]] = deepcopy(stored)
        return deepcopy(stored)

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._rows.get(attempt_id))

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([item for item in self._rows.values() if item["run_id"] == run_id])

    def list_open(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id)
                if item["status"] in {"prepared", "dispatching", "indeterminate"}]

    def transition(
        self, attempt_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if attempt_id not in self._rows:
                raise RunConflictError(f"attempt missing: {attempt_id}")
            stored = advance_record(
                self._rows[attempt_id], expected_revision=expected_revision,
                expected_status=expected_status, status=status, changes=changes,
                transitions=ATTEMPT_TRANSITIONS,
            )
            self._rows[attempt_id] = deepcopy(stored)
            return deepcopy(stored)

    def complete(
        self, attempt_id: str, *, expected_revision: int, expected_status: str = "dispatching",
        status: str = "succeeded", changes: dict[str, Any] | None = None,
        case_run: dict[str, Any] | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed"}:
            raise ValueError("attempt completion requires succeeded or failed")
        with self._lock:
            if attempt_id not in self._rows:
                raise RunConflictError(f"attempt missing: {attempt_id}")
            previous = self._rows[attempt_id]
            pending = validate_event(event, previous["run_id"])
            if case_run is not None and (
                case_run.get("run_id") != previous["run_id"]
                or case_run.get("case_id") != previous["case_id"]
            ):
                raise ValueError("case result must match the attempt run and case")
            stored = advance_record(
                previous, expected_revision=expected_revision, expected_status=expected_status,
                status=status, changes=changes, transitions=ATTEMPT_TRANSITIONS,
            )
            if case_run is not None:
                old = self._cases.get(previous["run_id"], previous["case_id"])
                if old is not None and old != case_run:
                    raise RunConflictError("a different case result is already persisted")
            if pending is not None:
                self._events.append(pending)
            if case_run is not None:
                self._cases.upsert(case_run)
            self._rows[attempt_id] = deepcopy(stored)
            return deepcopy(stored)

    def mark_indeterminate(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            changed = []
            for attempt_id, current in self._rows.items():
                if current["run_id"] == run_id and current["status"] == "dispatching":
                    stored = advance_record(
                        current, expected_revision=current["revision"], expected_status="dispatching",
                        status="indeterminate", changes=None, transitions=ATTEMPT_TRANSITIONS,
                    )
                    self._rows[attempt_id] = stored
                    changed.append(deepcopy(stored))
            return changed


class MemoryScoringPasses:
    def __init__(self, runs: Any, events: Any, score_sets: MemoryScoreSets, lock: RLock) -> None:
        self._passes: dict[str, dict[str, Any]] = {}
        self._runs = runs
        self._events = events
        self._score_sets = score_sets
        self._lock = lock

    def append(
        self, record: dict[str, Any], scores: list[dict[str, Any]], *,
        expected_run_revision: int | None = None, expected_run_status: str | None = None,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stored = _pass_record(record)
        rows = validate_scores(scores)
        pending = validate_event(event, stored["run_id"])
        if (expected_run_revision is None) != (expected_run_status is None):
            raise ValueError("provide both expected_run_revision and expected_run_status")
        with self._lock:
            if stored["id"] in self._passes:
                raise RunConflictError(f"scoring pass already exists: {stored['id']}")
            current = self._runs.get(stored["run_id"])
            if expected_run_revision is not None and (
                current is None or current["revision"] != expected_run_revision
                or current["status"] != expected_run_status
            ):
                raise RunConflictError("run revision or status changed during scoring")
            # Validate and publish the event before the pass and the run pointer.
            if pending is not None:
                self._events.append(pending)
            self._passes[stored["id"]] = deepcopy(stored)
            self._score_sets._sets[stored["id"]] = rows
            if expected_run_revision is not None:
                self._runs.update(
                    {**current, "current_scoring_pass_id": stored["id"]},
                    expected_revision=expected_run_revision, expected_status=expected_run_status,
                )
        return deepcopy(stored)

    def get(self, pass_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._passes.get(pass_id))

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([item for item in self._passes.values() if item["run_id"] == run_id])

    def current(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            pass_id = run.get("current_scoring_pass_id") if run else None
            if pass_id:
                result = self._passes.get(pass_id)
                return deepcopy(result) if result and result["run_id"] == run_id else None
            records = self.list_for_run(run_id)
            return records[-1] if records else None


class MemoryScoreSets:
    def __init__(self, lock: RLock) -> None:
        self._sets: dict[str, list[dict[str, Any]]] = {}
        self._lock = lock

    def list_for_pass(self, pass_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._sets.get(pass_id, []))

    def get(self, pass_id: str, case_id: str) -> dict[str, Any] | None:
        return next((row for row in self.list_for_pass(pass_id) if row["case_id"] == case_id), None)


class MemoryCommands:
    def __init__(self, lock: RLock) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = lock

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = new_record(record, "command", "queued")
        with self._lock:
            if stored["id"] in self._rows:
                raise RunConflictError(f"command already exists: {stored['id']}")
            self._rows[stored["id"]] = deepcopy(stored)
        return deepcopy(stored)

    def get(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._rows.get(command_id))

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([item for item in self._rows.values() if item["run_id"] == run_id])

    def list(self, run_id: str) -> list[dict[str, Any]]:
        return self.list_for_run(run_id)

    def transition(
        self, command_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if command_id not in self._rows:
                raise RunConflictError(f"command missing: {command_id}")
            stored = advance_record(
                self._rows[command_id], expected_revision=expected_revision,
                expected_status=expected_status, status=status, changes=changes,
                transitions=COMMAND_TRANSITIONS,
            )
            self._rows[command_id] = deepcopy(stored)
            return deepcopy(stored)
