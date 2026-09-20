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
    # M4-T10：rejected/expired/delivery_unknown 与历史 failed 并存；
    # 交付不确定（重启/断连）永不回退为已送达，也不重复投递危险批准。
    "queued": {"delivered", "failed", "rejected", "expired"},
    "delivered": {"acknowledged", "failed", "delivery_unknown"},
    "acknowledged": set(),
    "failed": set(),
    "rejected": set(),
    "expired": set(),
    "delivery_unknown": set(),
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
    # trial_id 与 run/case/attempt 身份同级不可变（review R24）：改写它会让
    # trial-scoped attempt 伪装成任务级 attempt，绕过"不写任务级 CaseRun"的保护。
    if {
        "id", "run_id", "case_id", "attempt_no", "trial_id", "revision", "status",
    }.intersection(changes):
        raise ValueError("transition changes cannot replace identity, revision or status")
    return {**deepcopy(current), **deepcopy(changes), "status": status, "revision": expected_revision + 1}


def validate_case_rows(rows: list[dict[str, Any]] | None, run_id: str) -> list[dict[str, Any]]:
    bound: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict) or row.get("run_id") != run_id:
            raise ValueError("case row must match the scoring pass run_id")
        if not isinstance(row.get("case_id"), str) or not row["case_id"]:
            raise ValueError("case row needs a nonempty case_id")
        if row["case_id"] in seen:
            raise ValueError("case rows need distinct case_id values")
        seen.add(row["case_id"])
        bound.append(deepcopy(row))
    return bound


def score_identity(score: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """持久化复合键：NULL 语义以 '' 规范化，避免 SQL NULL 唯一性差异。"""
    def part(value: Any) -> str:
        return value if isinstance(value, str) else ""
    return (
        part(score.get("case_id")),
        part(score.get("trial_id")),
        part(score.get("metric_id")),
        part(score.get("evaluator_id")),
        part(score.get("evaluator_version")),
    )


def validate_scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from pydantic import ValidationError
    from motte_contracts.evidence import Score

    result = deepcopy(scores)
    ids = [score.get("case_id") for score in result]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids):
        raise ValueError("scores need distinct nonempty case_id values")
    try:
        for score in result:
            Score.model_validate(score)
    except ValidationError as error:
        raise ValueError(f"score does not match the public contract: {error}") from error
    has_metric_identity = [
        any(score.get(key) for key in ("trial_id", "metric_id", "evaluator_id", "evaluator_version"))
        for score in result
    ]
    if any(has_metric_identity) and not all(has_metric_identity):
        raise ValueError("scores cannot mix legacy and multi-metric rows")
    if any(has_metric_identity):
        for score in result:
            if not all(score.get(key) for key in ("metric_id", "evaluator_id", "evaluator_version")):
                raise ValueError(
                    "multi-metric scores require metric_id, evaluator_id and evaluator_version"
                )
        keys = [score_identity(score) for score in result]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "scores require a unique (case_id, trial_id, metric_id, evaluator_id, "
                "evaluator_version) key per score"
            )
    elif len(ids) != len(set(ids)):
        raise ValueError("scores need distinct nonempty case_id values")
    return result
