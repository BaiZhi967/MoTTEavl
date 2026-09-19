"""Tool-trajectory, workspace-snapshot and process-exit metric evaluators.

Negative assertions ("never called", "no forbidden write") require complete
evidence: an incomplete trajectory or a failed listing yields insufficient
evidence instead of a fabricated pass. JSON Schema 评估一律使用禁止远程 $ref
的本地 registry（评分过程不得发起网络请求）。
"""
from __future__ import annotations

import fnmatch
import json

from motte_contracts.evaluation import MetricStatus


def evaluate_tool_call(observation, metric, context):
    from .observation import _base_metric, _insufficient

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
        from jsonschema.exceptions import SchemaError

        from .observation import local_schema_validator

        try:
            validator = local_schema_validator(args_schema)
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
        except Exception as error:  # noqa: BLE001 - 含被禁止的远程 $ref
            return _base_metric(
                observation, metric, MetricStatus.evaluator_error,
                reason="schema_unresolvable", details={"error": str(error)},
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
    """按内容快照判定 create/modify/delete（#6：不只比文件名）。

    需要前后内容 hash 才能证明"预置文件未被改动"；hash 缺失时对命中的
    预置文件返回 insufficient，而不是默认未变更。
    """
    from .observation import _base_metric, _insufficient

    workspace = observation.workspace
    if workspace is None or not workspace.complete:
        return _insufficient(observation, metric, "workspace_snapshot_incomplete")
    forbidden = list(metric["forbidden"])
    ignore_preexisting = metric.get("ignore_preexisting", True)
    before = set(workspace.before)
    after = set(workspace.after)
    before_hashes = dict(workspace.before_hashes or {})
    after_hashes = dict(workspace.after_hashes or {})

    violations: list[str] = []
    unprovable: list[str] = []
    matched = [path for path in before | after
               if any(fnmatch.fnmatch(path, pattern) for pattern in forbidden)]
    for path in matched:
        in_before = path in before
        in_after = path in after
        if in_after and not in_before:
            violations.append(f"{path} (created)")
            continue
        if in_before and not in_after:
            # 预置文件被删除也是一次禁止性变更
            violations.append(f"{path} (deleted)")
            continue
        # 前后都在：只有内容 hash 能区分"未动"与"覆盖"
        before_hash = before_hashes.get(path)
        after_hash = after_hashes.get(path)
        if before_hash is None or after_hash is None:
            if ignore_preexisting:
                unprovable.append(path)  # 无法证明未变更：如实暴露
            else:
                violations.append(f"{path} (modified, hash unavailable)")
            continue
        if before_hash != after_hash:
            violations.append(f"{path} (modified)")
        # hash 相同：预置文件未改动，不算违规

    if unprovable and not violations:
        return _insufficient(
            observation, metric, "preexisting_content_unprovable",
            details={"paths": sorted(unprovable)},
        )
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
            "monitored_files": len(after),
            **({"unprovable_preexisting": sorted(unprovable)} if unprovable else {}),
        },
    )


def evaluate_exit_code(observation, metric, context):
    from .observation import _base_metric, _insufficient

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
    from .observation import _base_metric

    schema = metric.get("schema")
    if not isinstance(schema, dict):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": "json-schema requires a schema object"},
        )
    output = observation.final_output
    if not isinstance(output, str):
        return _insufficient_json_output(observation, metric)
    # 按字节检查超限并拒绝；不截取前缀后当作完整输出评分（#12）
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) > context.limits.max_input_bytes:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="input_too_large",
            details={"size_bytes": len(encoded),
                     "max_input_bytes": context.limits.max_input_bytes},
        )
    try:
        payload = json.loads(output)
    except ValueError as error:
        # Empty/invalid JSON from the subject is a subject failure.
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="invalid_json", details={"error": str(error)},
        )
    from jsonschema.exceptions import SchemaError

    from .observation import local_schema_validator

    try:
        validator = local_schema_validator(schema)
        errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
    except SchemaError as error:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": str(error)},
        )
    except Exception as error:  # noqa: BLE001 - 含被禁止的远程 $ref
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="schema_unresolvable", details={"error": str(error)},
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
