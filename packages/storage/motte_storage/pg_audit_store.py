"""PostgreSQL audit repositories; write operations are single transactions."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Json

from .audit_store import _pass_record
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
                    cursor.execute(
                        "SELECT 1 FROM case_runs WHERE run_id = %s AND case_id = %s",
                        (stored["run_id"], stored["case_id"]),
                    )
                    if cursor.fetchone() is not None:
                        raise RunConflictError(f"case result already exists: {stored['case_id']}")
                    cursor.execute(
                        "SELECT 1 FROM case_attempts WHERE run_id = %s AND case_id = %s "
                        "AND status IN ('prepared', 'dispatching', 'indeterminate') FOR UPDATE",
                        (stored["run_id"], stored["case_id"]),
                    )
                    if cursor.fetchone() is not None:
                        raise RunConflictError(f"case already has an open attempt: {stored['case_id']}")
                    cursor.execute(
                        "INSERT INTO case_attempts(id, run_id, case_id, attempt_no, status, revision, payload) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (stored["id"], stored["run_id"], stored["case_id"], stored["attempt_no"],
                         stored["status"], stored["revision"], Json(stored)),
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
                    "SELECT payload FROM case_attempts WHERE id = %s FOR UPDATE", (attempt_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    raise RunConflictError(f"attempt missing: {attempt_id}")
                current_attempt = row[0]
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
                cursor.execute("SELECT payload FROM case_attempts WHERE id = %s", (attempt_id,))
                row = cursor.fetchone()
        return deepcopy(row[0]) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM case_attempts WHERE run_id = %s ORDER BY position", (run_id,)
                )
                rows = cursor.fetchall()
        return [deepcopy(row[0]) for row in rows]

    def list_open(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_for_run(run_id)
                if item["status"] in {"prepared", "dispatching", "indeterminate"}]

    def transition(
        self, attempt_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with _connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM case_attempts WHERE id = %s FOR UPDATE", (attempt_id,))
                row = cursor.fetchone()
                if row is None:
                    raise RunConflictError(f"attempt missing: {attempt_id}")
                stored = advance_record(
                    row[0], expected_revision=expected_revision, expected_status=expected_status,
                    status=status, changes=changes, transitions=ATTEMPT_TRANSITIONS,
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
                cursor.execute("SELECT payload FROM case_attempts WHERE id = %s FOR UPDATE", (attempt_id,))
                row = cursor.fetchone()
                if row is None:
                    raise RunConflictError(f"attempt missing: {attempt_id}")
                previous = row[0]
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
                    "SELECT payload FROM case_attempts WHERE run_id = %s AND status = 'dispatching' "
                    "ORDER BY position FOR UPDATE", (run_id,),
                )
                for (current,) in cursor.fetchall():
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
                    "SELECT payload FROM case_attempts WHERE run_id = %s "
                    "AND status IN ('dispatching', 'indeterminate') "
                    "ORDER BY position FOR UPDATE", (run_id,),
                )
                uncertain: list[dict[str, Any]] = []
                for (current,) in cursor.fetchall():
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
