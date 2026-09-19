"""Tool-trajectory, workspace-snapshot and process-exit metric evaluators.

Negative assertions ("never called", "no forbidden write") require complete
evidence: an incomplete trajectory or a failed listing yields insufficient
evidence instead of a fabricated pass.
"""
from __future__ import annotations

import fnmatch
from typing import Any

from motte_contracts.evaluation import MetricStatus


def evaluate_tool_call(observation, metric, context):
    from .observation import MetricRequest, _base_metric, _insufficient

    if not observation.coverage.complete:
        return _insufficient(observation, metric, "tool_trajectory_incomplete")
    tool = metric["tool"]
    calls = [call for call in observation.tool_calls if call.tool_name == tool]
    forbidden = metric.get("forbidden") is True
    if forbidden and calls:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="forbidden_tool_called",
            details={"calls": len(calls)},
        )
    successful = [call for call in calls if call.status == "succeeded"]
    min_calls = metric.get("min_calls")
    max_calls = metric.get("max_calls")
    if min_calls is not None and len(successful) < min_calls:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="too_few_calls", details={"calls": len(successful), "min_calls": min_calls},
        )
    if max_calls is not None and len(successful) > max_calls:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="too_many_calls", details={"calls": len(successful), "max_calls": max_calls},
        )
    args_schema = metric.get("args_schema")
    if args_schema is not None:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        try:
            validator = Draft202012Validator(args_schema)
            violations = [
                {"call_id": call.call_id, "error": error.message}
                for call in successful
                for error in validator.iter_errors(call.arguments)
            ]
        except SchemaError as error:
            return _base_metric(
                observation, metric, MetricStatus.evaluator_error,
                reason="config_error", details={"error": str(error)},
            )
        if violations:
            return _base_metric(
                observation, metric, MetricStatus.scored, passed=False,
                reason="args_schema_violation",
                details={"violations": violations[:5]},
            )
    return _base_metric(observation, metric, MetricStatus.scored, passed=True,
                        details={"calls": len(successful)})


def evaluate_no_forbidden_write(observation, metric, context):
    from .observation import MetricRequest, _base_metric, _insufficient

    workspace = observation.workspace
    if workspace is None or not workspace.complete:
        return _insufficient(observation, metric, "workspace_snapshot_incomplete")
    forbidden = list(metric["forbidden"])
    ignore_preexisting = metric.get("ignore_preexisting", True)
    before = set(workspace.before)
    monitored = workspace.after
    violations = []
    for path in monitored:
        if not any(fnmatch.fnmatch(path, pattern) for pattern in forbidden):
            continue
        if ignore_preexisting and path in before:
            continue
        violations.append(path)
    if violations:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="forbidden_write_detected",
            details={"violations": sorted(violations)},
        )
    return _base_metric(
        observation, metric, MetricStatus.scored, passed=True,
        details={
            # The pass only speaks for the monitored workspace scope.
            "scope": "workspace",
            "monitored_files": len(monitored),
        },
    )


def evaluate_exit_code(observation, metric, context):
    from .observation import MetricRequest, _base_metric, _insufficient

    label = metric.get("label")
    processes = observation.processes
    if label is not None:
        processes = [item for item in processes if item.label == label]
    if not processes:
        return _insufficient(observation, metric, "process_not_observed")
    codes = [item.exit_code for item in processes]
    if any(code is None for code in codes):
        return _insufficient(observation, metric, "exit_code_unobserved")
    allowed = list(metric["allowed"])
    ok = all(code in allowed for code in codes)
    return _base_metric(
        observation, metric, MetricStatus.scored, passed=ok,
        reason=None if ok else "exit_code_mismatch",
        details={"exit_codes": codes, "allowed": allowed},
    )


def evaluate_json_schema(observation, metric, context):
    from .observation import MetricRequest, _base_metric

    import json

    schema = metric.get("schema")
    if not isinstance(schema, dict):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": "json-schema requires a schema object"},
        )
    output = observation.final_output
    if not isinstance(output, str):
        return _insufficient_json_output(observation, metric)
    text = output[: context.limits.max_input_bytes]
    try:
        payload = json.loads(text)
    except ValueError as error:
        # Empty/invalid JSON from the subject is a subject failure.
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="invalid_json", details={"error": str(error)},
        )
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
    except SchemaError as error:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": str(error)},
        )
    if errors:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="schema_violation",
            details={"violations": [error.message for error in errors[:5]]},
        )
    return _base_metric(observation, metric, MetricStatus.scored, passed=True)


def _insufficient_json_output(observation, metric):
    from .observation import _base_metric

    return _base_metric(
        observation, metric, MetricStatus.insufficient_evidence,
        reason="final_output_missing",
    )
