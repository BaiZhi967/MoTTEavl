"""PostgreSQL audit repositories; write operations are single transactions."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Json

from .audit_store import _attempt_trial_id, _indexed_attempt, _pass_record
from .benchmark_datasets import RevisionConflictError, _content_identity, _record_id, _validate
from .integrity import (
    ATTEMPT_TRANSITIONS,
    COMMAND_TRANSITIONS,
    RunConflictError,
    advance_record,
    new_record,
    next_run,
    stored_run,
    validate_case_rows,
    validate_event,
    validate_scores,
)
from .postgres import _append_event, _connect


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class PgAttempts:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def begin(
        self, record: dict[str, Any], *, expected_run_revision: int | None = None,
        expected_run_status: str | None = None,
    ) -> dict[str, Any]:
        stored = new_record(record, "attempt", "prepared")
        if not isinstance(stored.get("case_id"), str) or not stored["case_id"]:
            raise ValueError("attempt needs a nonempty case_id")
        stored.setdefault("attempt_no", 1)
        if type(stored["attempt_no"]) is not int or stored["attempt_no"] < 1:
            raise ValueError("attempt_no must be a positive integer")
        trial_id = _attempt_trial_id(stored)
        stored["trial_id"] = trial_id
        try:
            with _connect(self._dsn) as connection:
                with connection.cursor() as cursor:
                    if (expected_run_revision is None) != (expected_run_status is None):
                        raise ValueError("provide both expected Run revision and status")
                    if expected_run_revision is not None:
                        cursor.execute(
                            "SELECT revision, payload->>'status' FROM runs WHERE id = %s FOR UPDATE",
                            (stored["run_id"],),
                        )
                        run_row = cursor.fetchone()
                        if run_row != (expected_run_revision, expected_run_status):
                            raise RunConflictError(
                                f"run revision or status changed: {stored['run_id']}"
                            )
                    if not trial_id:
                        cursor.execute(
                            "SELECT 1 FROM case_runs WHERE run_id = %s AND case_id = %s",
                            (stored["run_id"], stored["case_id"]),
                        )
                        if cursor.fetchone() is not None:
                            raise RunConflictError(
                                f"case result already exists: {stored['case_id']}"
                            )
                    cursor.execute(
                        "SELECT 1 FROM case_attempts WHERE run_id = %s AND case_id = %s "
                        "AND trial_id = %s AND status IN ('prepared', 'dispatching', 'indeterminate') FOR UPDATE",  # noqa: E501
                        (stored["run_id"], stored["case_id"], trial_id),
                    )
                    if cursor.fetchone() is not None:
                        raise RunConflictError(
                            f"case already has an open attempt: {stored['case_id']}"
                        )
                    cursor.execute(
                        "INSERT INTO case_attempts(id, run_id, case_id, attempt_no, status, revision, payload, trial_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",  # noqa: E501
                        (stored["id"], stored["run_id"], stored["case_id"], stored["attempt_no"],
                         stored["status"], stored["revision"], Json(stored), trial_id),
                    )
        except UniqueViolation as error:
            raise RunConflictError("attempt id or attempt_no already exists") from error
        return deepcopy(stored)

    def dispatch(
        self, attempt_id: str, *, expected_revision: int,
        run_id: str, expected_run_revision: int, expected_run_status: str,
    ) -> dict[str, Any]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, revision FROM runs WHERE id = %s FOR UPDATE", (run_id,)
                )
                run_row = cursor.fetchone()
                if run_row is None or run_row[1] != expected_run_revision:
                    raise RunConflictError(f"run revision or status changed: {run_id}")
                run = stored_run(run_row[0], run_row[1])
                if run.get("status") != expected_run_status or run.get("cancellation"):
                    raise RunConflictError(f"run is no longer dispatchable: {run_id}")
                cursor.execute(
                    "SELECT payload, trial_id FROM case_attempts WHERE id = %s FOR UPDATE", (attempt_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    raise RunConflictError(f"attempt missing: {attempt_id}")
                current_attempt = _indexed_attempt(row[0], row[1])
                if current_attempt.get("run_id") != run_id:
                    raise RunConflictError(f"attempt belongs to another run: {attempt_id}")
                stored = advance_record(
                    current_attempt, expected_revision=expected_revision,
                    expected_status="prepared", status="dispatching",
                    changes={"dispatched_at": datetime.now(UTC).isoformat()},
                    transitions=ATTEMPT_TRANSITIONS,
                )
                cursor.execute(
                    "UPDATE case_attempts SET payload = %s, status = %s, revision = %s "
                    "WHERE id = %s AND status = 'prepared' AND revision = %s",
                    (Json(stored), "dispatching", stored["revision"], attempt_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise RunConflictError(f"attempt revision or status changed: {attempt_id}")
        return deepcopy(stored)

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload, trial_id FROM case_attempts WHERE id = %s", (attempt_id,))
                row = cursor.fetchone()
        return deepcopy(_indexed_attempt(row[0], row[1])) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, trial_id FROM case_attempts WHERE run_id = %s ORDER BY position", (run_id,)
                )
                rows = cursor.fetchall()
        return [deepcopy(_indexed_attempt(row[0], row[1])) for row in rows]

    def list_open(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id)
                if item["status"] in {"prepared", "dispatching", "indeterminate"}]

    def transition(
        self, attempt_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload, trial_id FROM case_attempts WHERE id = %s FOR UPDATE", (attempt_id,))
                row = cursor.fetchone()
                if row is None:
                    raise RunConflictError(f"attempt missing: {attempt_id}")
                stored = advance_record(
                    _indexed_attempt(row[0], row[1]), expected_revision=expected_revision,
                    expected_status=expected_status, status=status, changes=changes,
                    transitions=ATTEMPT_TRANSITIONS,
                )
                cursor.execute(
                    "UPDATE case_attempts SET payload = %s, status = %s, revision = %s "
                    "WHERE id = %s AND status = %s AND revision = %s",
                    (Json(stored), status, stored["revision"], attempt_id,
                     expected_status, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise RunConflictError(f"attempt revision or status changed: {attempt_id}")
        return deepcopy(stored)

    def complete(
        self, attempt_id: str, *, expected_revision: int, expected_status: str = "dispatching",
        status: str = "succeeded", changes: dict[str, Any] | None = None,
        case_run: dict[str, Any] | None = None, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed"}:
            raise ValueError("attempt completion requires succeeded or failed")
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload, trial_id FROM case_attempts WHERE id = %s FOR UPDATE", (attempt_id,))
                row = cursor.fetchone()
                if row is None:
                    raise RunConflictError(f"attempt missing: {attempt_id}")
                previous = _indexed_attempt(row[0], row[1])
                pending = validate_event(event, previous["run_id"])
                if case_run is not None and _attempt_trial_id(previous):
                    raise RunConflictError(
                        "trial-scoped attempts must not write the task-level case result",
                    )
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
                    cursor.execute(
                        "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM case_runs WHERE run_id = %s",
                        (previous["run_id"],),
                    )
                    ordinal = cursor.fetchone()[0]
                    cursor.execute(
                        "INSERT INTO case_runs(run_id, case_id, ordinal, payload) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (run_id, case_id) DO NOTHING",
                        (previous["run_id"], previous["case_id"], ordinal, Json(case_run)),
                    )
                    cursor.execute(
                        "SELECT payload FROM case_runs WHERE run_id = %s AND case_id = %s",
                        (previous["run_id"], previous["case_id"]),
                    )
                    if cursor.fetchone()[0] != case_run:
                        raise RunConflictError("a different case result is already persisted")
                cursor.execute(
                    "UPDATE case_attempts SET payload = %s, status = %s, revision = %s "
                    "WHERE id = %s AND status = %s AND revision = %s",
                    (Json(stored), status, stored["revision"], attempt_id,
                     expected_status, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise RunConflictError(f"attempt revision or status changed: {attempt_id}")
                if pending is not None:
                    _append_event(cursor, pending)
        return deepcopy(stored)

    def mark_indeterminate(self, run_id: str) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, trial_id FROM case_attempts WHERE run_id = %s AND status = 'dispatching' "
                    "ORDER BY position FOR UPDATE", (run_id,),
                )
                for payload, trial_id in cursor.fetchall():
                    current = _indexed_attempt(payload, trial_id)
                    stored = advance_record(
                        current, expected_revision=current["revision"], expected_status="dispatching",
                        status="indeterminate", changes=None, transitions=ATTEMPT_TRANSITIONS,
                    )
                    cursor.execute(
                        "UPDATE case_attempts SET payload = %s, status = %s, revision = %s WHERE id = %s",
                        (Json(stored), "indeterminate", stored["revision"], stored["id"]),
                    )
                    changed.append(deepcopy(stored))
        return changed

    def quarantine_indeterminate(
        self, run_id: str, *, expected_run_revision: int, expected_run_status: str,
        changes: dict[str, Any], event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Atomically quarantine uncertain attempts and their owning Run."""
        pending = validate_event(event, run_id)
        if pending is None:
            raise ValueError("quarantine requires an event")
        if {"id", "revision", "schema_version", "status"}.intersection(changes):
            raise ValueError("quarantine changes cannot overwrite run identity, revision or status")
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, revision FROM runs WHERE id = %s FOR UPDATE", (run_id,)
                )
                run_row = cursor.fetchone()
                if run_row is None or run_row[1] != expected_run_revision:
                    raise RunConflictError(f"run revision changed: {run_id}")
                run = stored_run(run_row[0], run_row[1])
                if run.get("status") != expected_run_status:
                    raise RunConflictError(f"run status changed: {run_id}")
                cursor.execute(
                    "SELECT payload, trial_id FROM case_attempts WHERE run_id = %s "
                    "AND status IN ('dispatching', 'indeterminate') "
                    "ORDER BY position FOR UPDATE", (run_id,),
                )
                uncertain: list[dict[str, Any]] = []
                for payload, trial_id in cursor.fetchall():
                    current = _indexed_attempt(payload, trial_id)
                    stored = current
                    if current["status"] == "dispatching":
                        stored = advance_record(
                            current, expected_revision=current["revision"],
                            expected_status="dispatching", status="indeterminate", changes=None,
                            transitions=ATTEMPT_TRANSITIONS,
                        )
                        cursor.execute(
                            "UPDATE case_attempts SET payload = %s, status = %s, revision = %s "
                            "WHERE id = %s",
                            (Json(stored), "indeterminate", stored["revision"], stored["id"]),
                        )
                    uncertain.append(deepcopy(stored))
                if not uncertain:
                    return []
                updated_run = next_run(
                    {**run, **deepcopy(changes), "status": "needs_review"}, expected_run_revision
                )
                cursor.execute(
                    "UPDATE runs SET payload = %s, revision = %s WHERE id = %s AND revision = %s "
                    "AND payload->>'status' = %s",
                    (Json(updated_run), updated_run["revision"], run_id,
                     expected_run_revision, expected_run_status),
                )
                if cursor.rowcount != 1:
                    raise RunConflictError(f"run revision or status changed: {run_id}")
                _append_event(cursor, pending)
        return uncertain


class PgScoringPasses:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def append(
        self, record: dict[str, Any], scores: list[dict[str, Any]], *,
        expected_run_revision: int | None = None, expected_run_status: str | None = None,
        event: dict[str, Any] | None = None, final_status: str | None = None,
        run_changes: dict[str, Any] | None = None,
        terminal_event: dict[str, Any] | None = None,
        score_events: list[dict[str, Any]] | None = None,
        require_no_open_attempts: bool = False,
        case_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        stored = _pass_record(record)
        rows = validate_scores(scores)
        bound_case_rows = validate_case_rows(case_rows, stored["run_id"])
        if any(score.get("scoring_pass_id") not in (None, stored["id"]) for score in rows):
            raise ValueError("score belongs to a different scoring pass")
        stored["scores"] = deepcopy(rows)
        pending = validate_event(event, stored["run_id"])
        pending_scores = [
            validate_event(item, stored["run_id"]) for item in (score_events or [])
        ]
        pending_terminal = validate_event(terminal_event, stored["run_id"])
        if (expected_run_revision is None) != (expected_run_status is None):
            raise ValueError("provide both expected_run_revision and expected_run_status")
        if final_status is not None and expected_run_revision is None:
            raise ValueError("final scoring status requires an expected Run revision and status")
        changes = deepcopy(run_changes or {})
        if {"id", "revision", "schema_version", "status"}.intersection(changes):
            raise ValueError("scoring changes cannot overwrite run identity, revision or status")
        try:
            with _connect(self._dsn) as connection:
                with connection.cursor() as cursor:
                    updated_run = None
                    if expected_run_revision is not None:
                        cursor.execute(
                            "SELECT payload, revision FROM runs WHERE id = %s FOR UPDATE",
                            (stored["run_id"],),
                        )
                        run_row = cursor.fetchone()
                        if run_row is None or run_row[1] != expected_run_revision:
                            raise RunConflictError("run revision changed during scoring")
                        run = stored_run(run_row[0], run_row[1])
                        if run.get("status") != expected_run_status:
                            raise RunConflictError("run status changed during scoring")
                        if require_no_open_attempts:
                            cursor.execute(
                                "SELECT 1 FROM case_attempts WHERE run_id = %s "
                                "AND status IN ('prepared', 'dispatching', 'indeterminate') "
                                "LIMIT 1 FOR UPDATE",
                                (stored["run_id"],),
                            )
                            if cursor.fetchone() is not None:
                                raise RunConflictError("run has an open case attempt")
                        updated_run = next_run(
                            {
                                **run,
                                **changes,
                                "current_scoring_pass_id": stored["id"],
                                "status": final_status or run["status"],
                            },
                            expected_run_revision,
                        )
                    for case_row in bound_case_rows:
                        cursor.execute(
                            "SELECT 1 FROM case_runs WHERE run_id = %s AND case_id = %s FOR UPDATE",
                            (stored["run_id"], case_row["case_id"]),
                        )
                        if cursor.fetchone() is not None:
                            raise RunConflictError(
                                f"case row appeared during terminalization: {case_row['case_id']}"
                            )
                        cursor.execute(
                            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM case_runs WHERE run_id = %s",
                            (stored["run_id"],),
                        )
                        ordinal = cursor.fetchone()[0]
                        cursor.execute(
                            "INSERT INTO case_runs(run_id, case_id, ordinal, payload) "
                            "VALUES (%s, %s, %s, %s) ON CONFLICT (run_id, case_id) DO NOTHING",
                            (stored["run_id"], case_row["case_id"], ordinal, Json(case_row)),
                        )
                        if cursor.rowcount != 1:
                            raise RunConflictError(
                                f"case row appeared during terminalization: {case_row['case_id']}"
                            )
                    cursor.execute(
                        "INSERT INTO scoring_passes(id, run_id, payload) VALUES (%s, %s, %s)",
                        (stored["id"], stored["run_id"], Json(stored)),
                    )
                    cursor.executemany(
                        "INSERT INTO score_sets(scoring_pass_id, case_id, trial_id, metric_id, "
                        "evaluator_id, evaluator_version, ordinal, payload) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(
                            stored["id"], row["case_id"],
                            row.get("trial_id") or "", row.get("metric_id") or "",
                            row.get("evaluator_id") or "", row.get("evaluator_version") or "",
                            index, Json(row),
                        ) for index, row in enumerate(rows)],
                    )
                    if updated_run is not None:
                        cursor.execute(
                            "UPDATE runs SET payload = %s, revision = %s WHERE id = %s AND revision = %s "
                            "AND payload->>'status' = %s",
                            (Json(updated_run), updated_run["revision"], stored["run_id"],
                             expected_run_revision, expected_run_status),
                        )
                        if cursor.rowcount != 1:
                            raise RunConflictError("run revision or status changed during scoring")
                    terminal_first = pending_terminal is not None and pending_terminal.get(
                        "status"
                    ) in {"cancelled", "unsupported"}
                    if terminal_first:
                        _append_event(cursor, pending_terminal)
                    for score_event in pending_scores:
                        if score_event is not None:
                            _append_event(cursor, score_event)
                    if pending is not None:
                        _append_event(cursor, pending)
                    if pending_terminal is not None and not terminal_first:
                        _append_event(cursor, pending_terminal)
        except UniqueViolation as error:
            raise RunConflictError(f"scoring pass already exists: {stored['id']}") from error
        return deepcopy(stored)

    def get(self, pass_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM scoring_passes WHERE id = %s", (pass_id,))
                row = cursor.fetchone()
        return deepcopy(row[0]) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM scoring_passes WHERE run_id = %s ORDER BY position", (run_id,)
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def current(self, run_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM runs WHERE id = %s", (run_id,))
                row = cursor.fetchone()
                pass_id = row[0].get("current_scoring_pass_id") if row else None
                if pass_id:
                    cursor.execute(
                        "SELECT payload FROM scoring_passes WHERE id = %s AND run_id = %s",
                        (pass_id, run_id),
                    )
                else:
                    cursor.execute(
                        "SELECT payload FROM scoring_passes WHERE run_id = %s "
                        "ORDER BY position DESC LIMIT 1", (run_id,),
                    )
                selected = cursor.fetchone()
        return deepcopy(selected[0]) if selected else None


class PgScoreSets:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def list_for_pass(self, pass_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM score_sets WHERE scoring_pass_id = %s ORDER BY ordinal", (pass_id,)
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def list_for_case(self, pass_id: str, case_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM score_sets WHERE scoring_pass_id = %s AND case_id = %s "
                    "ORDER BY ordinal", (pass_id, case_id),
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def get(self, pass_id: str, case_id: str) -> dict[str, Any] | None:
        """Legacy 单指标读取；多指标行显式报歧义，绝不静默取第一行。"""
        rows = self.list_for_case(pass_id, case_id)
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError(
                f"ambiguous multi-metric scores for case {case_id}; query by metric instead"
            )
        return rows[0]

    def get_metric(
        self, pass_id: str, case_id: str, metric_id: str,
        evaluator_id: str, evaluator_version: str, *, trial_id: str | None = None,
    ) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM score_sets WHERE scoring_pass_id = %s AND case_id = %s "
                    "AND trial_id = %s AND metric_id = %s AND evaluator_id = %s "
                    "AND evaluator_version = %s",
                    (pass_id, case_id, trial_id or "", metric_id, evaluator_id, evaluator_version),
                )
                row = cursor.fetchone()
        return deepcopy(row[0]) if row else None


class PgInvocations:
    """PostgreSQL invocation boundary log (prepared -> dispatching -> settled)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        from .invocations import validate_invocation

        stored = validate_invocation({**record, "status": record.get("status", "prepared")})
        stored.setdefault("revision", 1)
        try:
            with _connect(self._dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO agent_invocations(id, run_id, case_id, kind, step, "
                        "status, revision, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (stored["id"], stored["run_id"], stored["case_id"], stored["kind"],
                         stored["step"], stored["status"], stored["revision"], Json(stored)),
                    )
        except UniqueViolation as error:
            raise RunConflictError(f"invocation already exists: {stored['id']}") from error
        return deepcopy(stored)

    def get(self, invocation_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM agent_invocations WHERE id = %s", (invocation_id,)
                )
                row = cursor.fetchone()
        return deepcopy(row[0]) if row else None

    def transition(
        self, invocation_id: str, *, expected_revision: int, expected_status: str,
        status: str, changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .invocations import INVOCATION_TRANSITIONS, validate_invocation

        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM agent_invocations WHERE id = %s FOR UPDATE",
                    (invocation_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RunConflictError(f"invocation missing: {invocation_id}")
                current = row[0]
                if current.get("revision") != expected_revision or current.get("status") != expected_status:
                    raise RunConflictError(
                        f"invocation revision or status changed: {invocation_id}"
                    )
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
                    raise RunConflictError(
                        f"invocation revision or status changed: {invocation_id}"
                    )
        return deepcopy(stored)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM agent_invocations WHERE run_id = %s ORDER BY position",
                    (run_id,),
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def list_for_case(self, run_id: str, case_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id) if item["case_id"] == case_id]

    def list_unsettled(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id)
                if item["status"] in {"prepared", "dispatching"}]


class PgCommands:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = new_record(record, "command", "queued")
        try:
            with _connect(self._dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO run_commands(id, run_id, status, revision, payload) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (stored["id"], stored["run_id"], stored["status"], stored["revision"],
                         Json(stored)),
                    )
        except UniqueViolation as error:
            raise RunConflictError(f"command already exists: {stored['id']}") from error
        return deepcopy(stored)

    def get(self, command_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM run_commands WHERE id = %s", (command_id,))
                row = cursor.fetchone()
        return deepcopy(row[0]) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM run_commands WHERE run_id = %s ORDER BY position", (run_id,)
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def list(self, run_id: str) -> list[dict[str, Any]]:
        return self.list_for_run(run_id)

    def transition(
        self, command_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM run_commands WHERE id = %s FOR UPDATE", (command_id,))
                row = cursor.fetchone()
                if row is None:
                    raise RunConflictError(f"command missing: {command_id}")
                stored = advance_record(
                    row[0], expected_revision=expected_revision, expected_status=expected_status,
                    status=status, changes=changes, transitions=COMMAND_TRANSITIONS,
                )
                cursor.execute(
                    "UPDATE run_commands SET payload = %s, status = %s, revision = %s "
                    "WHERE id = %s AND status = %s AND revision = %s",
                    (Json(stored), status, stored["revision"], command_id,
                     expected_status, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise RunConflictError(f"command revision or status changed: {command_id}")
        return deepcopy(stored)


def _as_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value)


class PgExternalJobs:
    """PostgreSQL 外部 Job 仓库；与 SQLite/Memory 实现语义一致（M2-T03）。

    表结构来自 alembic ``0006_external_jobs``；写操作单事务，记录与
    checkpoint 同事务提交。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def begin_job(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = deepcopy(record)
        job_id = str(stored.get("job_id"))
        run_id = str(stored.get("run_id"))
        status = str(stored.get("status"))
        launch_token = str(stored.get("launch_token"))
        message = "job already exists: " + job_id
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM external_jobs WHERE job_id = %s", (job_id,))
                if cursor.fetchone() is not None:
                    raise RunConflictError(message)
                cursor.execute(
                    "INSERT INTO external_jobs(job_id, run_id, status, launch_token, payload) VALUES (%s, %s, %s, %s, %s)",
                    (job_id, run_id, status, launch_token, Json(stored)),
                )
        return deepcopy(stored)

    def update_job(
        self, job_id: str, changes: dict[str, Any],
        *, guard_status_not: str | None = None,
    ) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM external_jobs WHERE job_id = %s FOR UPDATE", (job_id,))
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(job_id)
                job = _as_payload(row[0])
                updated = {**job, **deepcopy(changes), "updated_at": _utc_now()}
                new_status = str(updated.get("status") or job.get("status"))
                if guard_status_not is not None:
                    cursor.execute(
                        "UPDATE external_jobs SET status = %s, payload = %s WHERE job_id = %s AND status != %s",
                        (new_status, Json(updated), job_id, str(guard_status_not)),
                    )
                    if cursor.rowcount != 1:
                        return None
                else:
                    cursor.execute(
                        "UPDATE external_jobs SET status = %s, payload = %s WHERE job_id = %s",
                        (new_status, Json(updated), job_id),
                    )
        return deepcopy(updated)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM external_jobs WHERE job_id = %s", (job_id,))
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def jobs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM external_jobs WHERE run_id = %s ORDER BY created_at, job_id",
                    (run_id,),
                )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]

    def recoverable_for_run(self, run_id: str) -> list[dict[str, Any]]:
        recoverable = ("launching", "active", "collecting")
        return [
            job for job in self.jobs_for_run(run_id)
            if job.get("status") in recoverable
        ]

    def import_record(
        self,
        job_id: str,
        source_record_key: str,
        parser_version: str,
        content_hash: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM external_jobs WHERE job_id = %s", (job_id,))
                job_row = cursor.fetchone()
                if job_row is None:
                    raise KeyError(job_id)
                cursor.execute(
                    "SELECT content_hash, payload FROM external_job_records WHERE job_id = %s AND source_record_key = %s AND parser_version = %s",
                    (job_id, source_record_key, parser_version),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    existing_hash = existing[0]
                    existing_payload = _as_payload(existing[1])
                    if existing_hash == content_hash:
                        return {"status": "noop", "record": {
                            "job_id": job_id,
                            "source_record_key": source_record_key,
                            "parser_version": parser_version,
                            "content_hash": existing_hash,
                            "payload": existing_payload,
                        }}
                    cursor.execute(
                        "INSERT INTO external_job_conflicts(job_id, source_record_key, parser_version, existing_hash, incoming_hash, incoming_payload, detected_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (job_id, source_record_key, parser_version, existing_hash,
                         content_hash, Json(payload), _utc_now()),
                    )
                    return {"status": "conflict", "existing": {
                        "job_id": job_id,
                        "source_record_key": source_record_key,
                        "parser_version": parser_version,
                        "content_hash": existing_hash,
                        "payload": existing_payload,
                    }, "incoming": {"content_hash": content_hash, "payload": deepcopy(payload)}}
                imported_at = _utc_now()
                cursor.execute(
                    "INSERT INTO external_job_records(job_id, source_record_key, parser_version, content_hash, payload, imported_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (job_id, source_record_key, parser_version, content_hash,
                     Json(payload), imported_at),
                )
                job = _as_payload(job_row[0])
                checkpoint = dict(job.get("checkpoint") or {})
                checkpoint["records_consumed"] = int(checkpoint.get("records_consumed", 0)) + 1
                job["checkpoint"] = checkpoint
                job["updated_at"] = _utc_now()
                new_status = str(job.get("status") or "")
                cursor.execute(
                    "UPDATE external_jobs SET status = %s, payload = %s WHERE job_id = %s",
                    (new_status, Json(job), job_id),
                )
        return {"status": "imported", "record": {
            "job_id": job_id,
            "source_record_key": source_record_key,
            "parser_version": parser_version,
            "content_hash": content_hash,
            "payload": deepcopy(payload),
            "imported_at": imported_at,
        }}

    def list_records(self, job_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT job_id, source_record_key, parser_version, content_hash, payload FROM external_job_records WHERE job_id = %s ORDER BY imported_at, position",
                    (job_id,),
                )
                rows = cursor.fetchall()
        records = []
        for row in rows:
            records.append({
                "job_id": row[0], "source_record_key": row[1],
                "parser_version": row[2], "content_hash": row[3],
                "payload": _as_payload(row[4]),
            })
        return records

    def list_conflicts(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                if job_id is None:
                    cursor.execute(
                        "SELECT job_id, source_record_key, parser_version, existing_hash, incoming_hash, incoming_payload, detected_at FROM external_job_conflicts ORDER BY position",
                    )
                else:
                    cursor.execute(
                        "SELECT job_id, source_record_key, parser_version, existing_hash, incoming_hash, incoming_payload, detected_at FROM external_job_conflicts WHERE job_id = %s ORDER BY position",
                        (job_id,),
                    )
                rows = cursor.fetchall()
        conflicts = []
        for row in rows:
            conflicts.append({
                "job_id": row[0], "source_record_key": row[1],
                "parser_version": row[2], "existing_hash": row[3],
                "incoming_hash": row[4], "incoming_payload": _as_payload(row[5]),
                "detected_at": str(row[6]),
            })
        return conflicts


class PgBenchmarkDatasets:
    """PostgreSQL 外部 Benchmark 数据准备结果仓库（review M2-R13）。

    表结构来自 alembic ``0007_benchmark_datasets``；payload 为准备结果的
    完整 JSON dump（清单/逐行内容/治理证据），按
    ``(benchmark_id, dataset_revision)`` 幂等覆盖。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = deepcopy(record)
        benchmark_id = str(stored.get("benchmark_id"))
        dataset_revision = str(stored.get("dataset_revision"))
        stored.setdefault("created_at", _utc_now())
        created_at = str(stored.get("created_at"))
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO benchmark_datasets(benchmark_id, dataset_revision, payload, created_at) VALUES (%s, %s, %s, %s) ON CONFLICT (benchmark_id, dataset_revision) DO UPDATE SET payload = EXCLUDED.payload, created_at = EXCLUDED.created_at",  # noqa: E501
                    (benchmark_id, dataset_revision, Json(stored), created_at),
                )
        return deepcopy(stored)

    def put_immutable(self, record: dict[str, Any]) -> dict[str, Any]:
        """原子首次插入；冲突等待提交后比较冻结语义，不覆盖既有 revision。"""
        stored = _validate(record)
        benchmark_id = str(stored.get("benchmark_id"))
        dataset_revision = str(stored.get("dataset_revision"))
        stored["id"] = _record_id(benchmark_id, dataset_revision)
        stored.setdefault("created_at", _utc_now())
        created_at = str(stored.get("created_at"))
        identity = _content_identity(stored)
        message = (
            "benchmark dataset revision already exists with different content: "
            + benchmark_id + "@" + dataset_revision
        )
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                # 行不存在时 SELECT FOR UPDATE 无锁可取。唯一索引仲裁首写；
                # DO NOTHING 等待竞争事务结束，下一条 READ COMMITTED 查询可见它。
                cursor.execute(
                    "INSERT INTO benchmark_datasets(benchmark_id, dataset_revision, payload, created_at) VALUES (%s, %s, %s, %s) ON CONFLICT (benchmark_id, dataset_revision) DO NOTHING RETURNING payload",  # noqa: E501
                    (benchmark_id, dataset_revision, Json(stored), created_at),
                )
                row = cursor.fetchone()
                if row is not None:
                    return {"status": "created", "record": _as_payload(row[0])}
                cursor.execute(
                    "SELECT payload FROM benchmark_datasets WHERE benchmark_id = %s AND dataset_revision = %s FOR UPDATE",  # noqa: E501
                    (benchmark_id, dataset_revision),
                )
                row = cursor.fetchone()
                if row is not None:
                    existing = _as_payload(row[0])
                    if _content_identity(existing) == identity:
                        return {"status": "identical", "record": existing}
                raise RevisionConflictError(message)

    def get(self, benchmark_id: str, dataset_revision: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM benchmark_datasets WHERE benchmark_id = %s AND dataset_revision = %s",  # noqa: E501
                    (benchmark_id, dataset_revision),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def latest(self, benchmark_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM benchmark_datasets WHERE benchmark_id = %s ORDER BY created_at DESC, position DESC LIMIT 1",  # noqa: E501
                    (benchmark_id,),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def list(self, benchmark_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                if benchmark_id is None:
                    cursor.execute(
                        "SELECT payload FROM benchmark_datasets ORDER BY position",
                    )
                else:
                    cursor.execute(
                        "SELECT payload FROM benchmark_datasets WHERE benchmark_id = %s ORDER BY position",  # noqa: E501
                        (benchmark_id,),
                    )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]


class PgBaselines:
    """PostgreSQL baseline 快照仓库（review M2-R12）；写入后不可变。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def put(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        stored = deepcopy(snapshot)
        snapshot_id = str(stored.get("id"))
        run_id = str(stored.get("run_id"))
        pass_id = str(stored.get("scoring_pass_id"))
        stored.setdefault("created_at", _utc_now())
        message = "baseline snapshots are immutable: " + snapshot_id
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM baseline_snapshots WHERE id = %s", (snapshot_id,))
                if cursor.fetchone() is not None:
                    raise ValueError(message)
                cursor.execute(
                    "INSERT INTO baseline_snapshots(id, run_id, scoring_pass_id, metrics, payload) VALUES (%s, %s, %s, %s, %s)",  # noqa: E501
                    (snapshot_id, run_id, pass_id, Json(stored.get("metrics")), Json(stored)),
                )
        return deepcopy(stored)

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM baseline_snapshots WHERE id = %s", (snapshot_id,),
                )
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def get_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM baseline_snapshots WHERE run_id = %s ORDER BY position",  # noqa: E501
                    (run_id,),
                )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]


class PgTrials:
    """PostgreSQL Trial 仓库（M3-T02）：与 Memory/SQLite 同语义。

    幂等/冲突语义复用 ``motte_storage.trials`` 的共享实现，避免三个后端
    各写一套判定逻辑。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def create_plans(self, plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from .trials import _plan_record

        results: list[dict[str, Any]] = []
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                for plan in plans:
                    results.append(self._create_one(cursor, _plan_record(plan)))
        return results

    def _create_one(self, cursor: Any, incoming: dict[str, Any]) -> dict[str, Any]:
        """原子首写，整批共用一个事务（review R22）。

        ``ON CONFLICT (trial_id) DO NOTHING`` 在 READ COMMITTED 下会先等待并发
        未提交事务，rowcount==0 表示另一个事务已插入同一 trial_id；随后同一事务
        里的 SELECT 能读到其已提交行，于是 identical/conflict 与串行写入共用
        ``_apply_plan`` 判定，不再有 UniqueViolation。第二个循环分支只覆盖
        "并发事务回滚导致 DO NOTHING 未插入" 的边界，重试一次仍不可见才报冲突。
        """
        from .trials import _apply_plan

        for _ in range(2):
            cursor.execute(
                "INSERT INTO trials(trial_id, run_id, task_key, repeat_index, status, plan_hash, payload, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (trial_id) DO NOTHING",  # noqa: E501
                (
                    incoming["trial_id"], incoming["run_id"], incoming["task_key"],
                    int(incoming["repeat_index"]), incoming["status"],
                    incoming["plan_hash"], Json(incoming), incoming["created_at"],
                ),
            )
            if cursor.rowcount == 1:
                return {
                    "status": "created", "trial_id": incoming["trial_id"],
                    "plan": deepcopy(incoming),
                }
            cursor.execute(
                "SELECT payload FROM trials WHERE trial_id = %s", (incoming["trial_id"],),
            )
            row = cursor.fetchone()
            if row is not None:
                return _apply_plan(_as_payload(row[0]), incoming)
        raise RunConflictError(
            f"trial first write is not observable after a concurrent rollback: "
            f"{incoming['trial_id']}"
        )

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM trials WHERE run_id = %s ORDER BY task_key, repeat_index, trial_id",  # noqa: E501
                    (run_id,),
                )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]

    def list_for_task(self, run_id: str, task_key: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM trials WHERE run_id = %s AND task_key = %s ORDER BY repeat_index, trial_id",  # noqa: E501
                    (run_id, task_key),
                )
                rows = cursor.fetchall()
        return [_as_payload(row[0]) for row in rows]

    def get(self, trial_id: str) -> dict[str, Any] | None:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM trials WHERE trial_id = %s", (trial_id,))
                row = cursor.fetchone()
        return _as_payload(row[0]) if row is not None else None

    def put_result(
        self, trial_id: str, result: dict[str, Any], *,
        source_hash: str, parser_version: str,
    ) -> dict[str, Any]:
        from .trials import (
            _apply_result, _canonical, _check_result_identity, _placeholder_replacement,
            _result_conflict, validate_result,
        )

        stored = validate_result(result)
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, result_payload FROM trials WHERE trial_id = %s FOR UPDATE",
                    (trial_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return {"status": "unknown_trial", "trial_id": trial_id}
                current = _as_payload(row[0])
                _check_result_identity(current, stored, trial_id)
                result_hash = _canonical(stored)
                if row[1] is not None:
                    if current.get("source_hash") == source_hash and current.get(
                        "result_hash",
                    ) == result_hash:
                        return {
                            "status": "identical", "trial_id": trial_id,
                            "result": _as_payload(row[1]),
                        }
                    if _placeholder_replacement(current, stored) is None:
                        return _result_conflict(
                            current, stored, source_hash=source_hash,
                            parser_version=parser_version,
                        )
                outcome = _apply_result(
                    current, stored, source_hash=source_hash,
                    parser_version=parser_version, result_hash=result_hash,
                )
                cursor.execute(
                    "UPDATE trials SET status = %s, payload = %s, result_payload = %s, finished_at = %s WHERE trial_id = %s",  # noqa: E501
                    (
                        current["status"], Json(current), Json(stored),
                        current["finished_at"], trial_id,
                    ),
                )
        return outcome
