"""Tool-trajectory, workspace-snapshot and process-exit metric evaluators.

Negative assertions ("never called", "no forbidden write") require complete
evidence: an incomplete trajectory or a failed listing yields insufficient
evidence instead of a fabricated pass. JSON Schema 评估一律使用禁止远程 $ref
的本地 registry（评分过程不得发起网络请求）。
"""
from __future__ import annotations

import fnmatch
import json

from typing import Any

from motte_contracts.evaluation import MetricStatus

# 会改动工作区文件的工具：写入轨迹判定的对象（R3 #6）
_MUTATING_TOOLS = {"write_file"}


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
        from .observation import _base_metric, bounded_schema_validation, schema_timeout

        timeout = schema_timeout(context)
        violations: list[dict[str, Any]] = []
        schema_failure: tuple[str, str] | None = None
        for call in successful:
            kind, value = bounded_schema_validation(
                args_schema, call.arguments, timeout_sec=timeout,
            )
            if kind == "timeout":
                return _base_metric(
                    observation, metric, MetricStatus.evaluator_error,
                    reason="schema_timeout", details={"timeout_sec": timeout},
                )
            if kind in ("config_error", "unresolvable", "error"):
                schema_failure = (kind, str(value))
                break
            violations.extend(
                {"call_id": call.call_id, "error": message} for message in value
            )
        if schema_failure is not None:
            kind, message = schema_failure
            return _base_metric(
                observation, metric, MetricStatus.evaluator_error,
                reason={"config_error": "config_error",
                        "unresolvable": "schema_unresolvable"}.get(kind, "schema_execution_error"),
                details={"error": message},
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
    """按内容快照 + 写入轨迹判定 create/modify/delete/写入后恢复（R3 #6）。

    需要前后内容 hash 才能证明"预置文件未被改动"；hash 缺失时对命中的
    预置文件返回 insufficient，而不是默认未变更。终态 hash 一致只能证明
    内容未变：轨迹完整时，成功的 write_file 命中禁写路径即违规——
    "先覆盖、再恢复原内容"同样是禁写（轨迹不完整时退回快照口径，结论
    只覆盖已监控范围）。
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

    written_restored: list[str] = []
    if observation.coverage.complete:
        # 轨迹完整时写入记录可证：终态一致但有成功写入 → 写入后恢复，仍违规
        for call in observation.tool_calls:
            arguments = call.arguments if isinstance(call.arguments, dict) else {}
            written = arguments.get("path")
            if (
                call.tool_name in _MUTATING_TOOLS
                and call.status == "succeeded"
                and isinstance(written, str)
                and any(fnmatch.fnmatch(written, pattern) for pattern in forbidden)
            ):
                written_restored.append(f"{written} (written, final state matches)")
    violations.extend(written_restored)

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
    from .observation import _base_metric, bounded_schema_validation, schema_timeout

    kind, value = bounded_schema_validation(
        schema, payload, timeout_sec=schema_timeout(context),
    )
    if kind == "timeout":
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="schema_timeout", details={"timeout_sec": schema_timeout(context)},
        )
    if kind in ("config_error", "unresolvable", "error"):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason={"config_error": "config_error",
                    "unresolvable": "schema_unresolvable"}.get(kind, "schema_execution_error"),
            details={"error": str(value)},
        )
    if value:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="schema_violation",
            details={"violations": list(value[:5])},
        )
    return _base_metric(observation, metric, MetricStatus.scored, passed=True)


def _insufficient_json_output(observation, metric):
    from .observation import _base_metric

    return _base_metric(
        observation, metric, MetricStatus.insufficient_evidence,
        reason="final_output_missing",
    )
