"""Shared invariants for the three RunStore implementations."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4


class RunConflictError(RuntimeError):
    """An insert collided or the expected revision/status no longer matches."""


def new_run_id() -> str:
    return f"run-{uuid4().hex}"


def stored_run(run: dict[str, Any], revision: int) -> dict[str, Any]:
    result = deepcopy(run)
    result["revision"] = revision
    result["schema_version"] = 1 if revision == 0 else 2
    return result


def new_run(run: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(run.get("id"), str) or not run["id"]:
        raise ValueError("run.id must be a nonempty string")
    if not isinstance(run.get("status"), str) or not run["status"]:
        raise ValueError("run.status must be a nonempty string")
    if type(run.get("revision", 1)) is not int or run.get("revision", 1) != 1 \
            or type(run.get("schema_version", 2)) is not int or run.get("schema_version", 2) != 2:
        raise ValueError("new runs must use revision=1 and schema_version=2")
    return stored_run(run, 1)


def next_run(run: dict[str, Any], expected_revision: int) -> dict[str, Any]:
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a nonnegative integer")
    if "revision" in run and (type(run["revision"]) is not int or run["revision"] != expected_revision):
        raise RunConflictError("run payload revision differs from expected_revision")
    return stored_run(run, expected_revision + 1)


def validate_event(event: dict[str, Any] | None, run_id: str) -> dict[str, Any] | None:
    if event is None:
        return None
    if event.get("run_id", run_id) != run_id or not event.get("type"):
        raise ValueError("event must have a type and match the run_id")
    return {**deepcopy(event), "run_id": run_id}


ATTEMPT_TRANSITIONS = {
    "prepared": {"dispatching", "failed"},
    "dispatching": {"succeeded", "failed", "indeterminate"},
    "succeeded": set(),
    "failed": set(),
    "indeterminate": set(),
}
COMMAND_TRANSITIONS = {
    "queued": {"delivered", "failed"},
    "delivered": {"acknowledged", "failed"},
    "acknowledged": set(),
    "failed": set(),
}


def new_record(record: dict[str, Any], prefix: str, status: str) -> dict[str, Any]:
    result = deepcopy(record)
    result.setdefault("id", f"{prefix}-{uuid4().hex}")
    if not isinstance(result["id"], str) or not result["id"]:
        raise ValueError("id must be a nonempty string")
    if not isinstance(result.get("run_id"), str) or not result["run_id"]:
        raise ValueError("run_id must be a nonempty string")
    if result.get("status", status) != status or type(result.get("revision", 1)) is not int \
            or result.get("revision", 1) != 1:
        raise ValueError(f"new {prefix} must start as {status} at revision 1")
    result["status"] = status
    result["revision"] = 1
    return result


def advance_record(
    current: dict[str, Any], *, expected_revision: int, expected_status: str,
    status: str, changes: dict[str, Any] | None, transitions: dict[str, set[str]],
) -> dict[str, Any]:
    if current.get("revision") != expected_revision or current.get("status") != expected_status:
        raise RunConflictError("record revision or status changed")
    if status not in transitions.get(expected_status, set()):
        raise ValueError(f"invalid transition: {expected_status} -> {status}")
    changes = changes or {}
    if {"id", "run_id", "case_id", "attempt_no", "revision", "status"}.intersection(changes):
        raise ValueError("transition changes cannot replace identity, revision or status")
    return {**deepcopy(current), **deepcopy(changes), "status": status, "revision": expected_revision + 1}


def validate_scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from pydantic import ValidationError
    from motte_contracts.evidence import Score

    result = deepcopy(scores)
    ids = [score.get("case_id") for score in result]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("scores need distinct nonempty case_id values")
    try:
        for score in result:
            Score.model_validate(score)
    except ValidationError as error:
        raise ValueError(f"score does not match the public contract: {error}") from error
    return result
