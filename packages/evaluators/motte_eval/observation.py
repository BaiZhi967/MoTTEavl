"""Observation-driven deterministic evaluation (M1 core evaluator).

A closed registry maps metric kinds to pure functions over a frozen
``FrozenObservation``. The registry is deliberately closed like the Direct LLM
``ScorerRegistry``: callers resolve built-in kinds only, there is no dynamic
import. One evaluator crashing yields ``evaluator_error`` for that metric and
never blocks the others; unknown, missing, or not-applicable outcomes are never
flattened into 0 or pass.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from motte_contracts.evaluation import (
    EvidenceRef,
    FrozenObservation,
    MetricResult,
    MetricStatus,
)

EVALUATOR_ID = "agent-deterministic"
EVALUATOR_VERSION = "1"

DEFAULT_MAX_INPUT_BYTES = 1_000_000
DEFAULT_MAX_ARTIFACT_BYTES = 10_000_000
MAX_INPUT_BYTES_LIMIT = 16_000_000
MAX_ARTIFACT_BYTES_LIMIT = 64_000_000
DEFAULT_REGEX_TIMEOUT_SEC = 2.0
MAX_REGEX_TIMEOUT_SEC = 10.0
DEFAULT_EVAL_DEADLINE_SEC = 30.0
MAX_EVAL_DEADLINE_SEC = 120.0
# schema 子进程校验的兜底期限（无全局 deadline 的直调场景）；
# 在批内执行时受剩余 eval deadline 双重约束（R3 #5）。
MAX_SCHEMA_TIMEOUT_SEC = 10.0

MetricRequest = dict[str, Any]


class EvaluatorConfigError(ValueError):
    """The evaluator configuration itself is invalid (never a subject failure)."""


@dataclass
class EvaluationLimits:
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES
    regex_timeout_sec: float = DEFAULT_REGEX_TIMEOUT_SEC
    eval_deadline_sec: float = DEFAULT_EVAL_DEADLINE_SEC

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_input_bytes": self.max_input_bytes,
            "max_artifact_bytes": self.max_artifact_bytes,
            "regex_timeout_sec": self.regex_timeout_sec,
            "eval_deadline_sec": self.eval_deadline_sec,
        }


@dataclass
class EvaluationContext:
    """Bounded access to frozen artifacts plus per-run limits and clock."""

    observation: FrozenObservation
    limits: EvaluationLimits = field(default_factory=EvaluationLimits)
    artifact_reader: Callable[[str], bytes | None] | None = None
    deadline: float | None = None

    def read_artifact(self, artifact_id: str) -> bytes | None:
        known = {
            entry.artifact_id: entry for entry in self.observation.artifact_refs
        }
        entry = known.get(artifact_id)
        if entry is None or not entry.available:
            return None
        if self.artifact_reader is None:
            return None
        try:
            return self.artifact_reader(artifact_id)
        except Exception:  # noqa: BLE001 - 读取失败按不可得处理，不升级为评分器故障
            return None

    def expired(self) -> bool:
        return self.deadline is not None and monotonic() > self.deadline


def local_schema_validator(schema: dict[str, Any]):
    """构造禁止一切远程 $ref 的 schema 校验器（#8：评分不得发起网络请求）。"""
    import jsonschema
    from referencing import Registry
    from referencing.exceptions import Unresolvable

    def blocked(uri: str):
        raise Unresolvable(ref=uri)

    return jsonschema.Draft202012Validator(schema, registry=Registry(retrieve=blocked))


def schema_timeout(context: "EvaluationContext") -> float:
    """schema 子进程期限：剩余全局 eval deadline 与兜底上限的较小值（R3 #5）。"""
    from time import monotonic

    effective = MAX_SCHEMA_TIMEOUT_SEC
    if context.deadline is not None:
        effective = min(effective, max(context.deadline - monotonic(), 0.05))
    return effective


def _base_metric(
    observation: FrozenObservation,
    metric: MetricRequest,
    status: MetricStatus,
    *,
    value: float | None = None,
    passed: bool | None = None,
    unit: str | None = None,
    reason: str | None = None,
    denominator: bool | None = None,
    details: dict[str, Any] | None = None,
    refs: list[EvidenceRef] | None = None,
) -> MetricResult:
    if denominator is None:
        denominator = status is MetricStatus.scored
    return MetricResult(
        metric_id=metric["metric_id"],
        status=status,
        value=value,
        passed=passed,
        unit=unit or metric.get("unit"),
        evaluator_id=EVALUATOR_ID,
        evaluator_version=EVALUATOR_VERSION,
        evidence_refs=refs or [],
        reason=reason,
        denominator=denominator,
        details=details or {},
    )


def _insufficient(
    observation: FrozenObservation,
    metric: MetricRequest,
    reason: str,
    *,
    details: dict[str, Any] | None = None,
    refs: list[EvidenceRef] | None = None,
) -> MetricResult:
    return _base_metric(
        observation, metric, MetricStatus.insufficient_evidence,
        reason=reason, details=details, refs=refs,
    )


# ---------------------------------------------------------------- 文本评分器

_NORMALIZATIONS = ("strip", "lowercase", "trim_whitespace")


def evaluate_exact(observation: FrozenObservation, metric: MetricRequest, context: EvaluationContext):
    if "expected" not in metric:
        return _base_metric(
            observation, metric, MetricStatus.not_applicable,
            reason="no_expectation", denominator=False,
        )
    expected = metric["expected"]
    output = observation.final_output
    if output is None:
        return _insufficient(observation, metric, "final_output_missing")
    normalization = list(metric.get("normalization", []))
    if any(item not in _NORMALIZATIONS for item in normalization):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error",
            details={"error": f"unknown normalization: {normalization}"},
        )
    left: Any = output
    right: Any = expected
    if isinstance(left, str) and isinstance(right, str):
        for item in normalization:
            if item in ("strip", "trim_whitespace"):
                left, right = left.strip(), right.strip()
            elif item == "lowercase":
                left, right = left.lower(), right.lower()
    ok = left == right
    return _base_metric(
        observation, metric, MetricStatus.scored, passed=ok,
        reason=None if ok else "value_mismatch",
    )


def evaluate_contains(observation: FrozenObservation, metric: MetricRequest, context: EvaluationContext):
    expected = metric.get("expected")
    if expected is None:
        return _base_metric(
            observation, metric, MetricStatus.not_applicable,
            reason="no_expectation", denominator=False,
        )
    if not isinstance(expected, str):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": "contains expected must be a string"},
        )
    output = observation.final_output
    if not isinstance(output, str):
        return _insufficient(observation, metric, "final_output_missing")
    policy = metric.get("case_policy", "sensitive")
    if policy not in ("sensitive", "insensitive"):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": f"unknown case_policy: {policy!r}"},
        )
    haystack = output if policy == "sensitive" else output.lower()
    needle = expected if policy == "sensitive" else expected.lower()
    ok = needle in haystack
    return _base_metric(
        observation, metric, MetricStatus.scored, passed=ok,
        reason=None if ok else "substring_missing",
    )


_REGEX_FLAG_NAMES = {"ignore_case", "multiline", "dotall"}
_FLAG_VALUES = {"ignore_case": re.IGNORECASE, "multiline": re.MULTILINE, "dotall": re.DOTALL}


def evaluate_regex(observation: FrozenObservation, metric: MetricRequest, context: EvaluationContext):
    pattern = metric.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": "regex metric requires a pattern"},
        )
    flag_names = list(metric.get("flags", []))
    if any(name not in _REGEX_FLAG_NAMES for name in flag_names):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": f"unsupported regex flags: {flag_names}"},
        )
    flags = 0
    for name in flag_names:
        flags |= _FLAG_VALUES[name]
    try:
        re.compile(pattern, flags)
    except re.error as error:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": str(error)},
        )
    output = observation.final_output
    if not isinstance(output, str):
        return _insufficient(observation, metric, "final_output_missing")
    max_bytes = metric.get("max_input_bytes", context.limits.max_input_bytes)
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="input_too_large",
            details={"size_bytes": len(encoded), "max_input_bytes": max_bytes},
        )
    from time import monotonic

    # 单指标超时受配置上限与剩余全局期限双重约束（#7：不允许绕过 eval deadline）
    requested_timeout = float(metric.get(
        "timeout_sec", context.limits.regex_timeout_sec,
    ))
    effective_timeout = min(requested_timeout, context.limits.regex_timeout_sec)
    if context.deadline is not None:
        remaining = context.deadline - monotonic()
        if remaining <= 0:
            return _base_metric(
                observation, metric, MetricStatus.evaluator_error,
                reason="eval_deadline_exceeded",
                details={"requested_timeout_sec": requested_timeout},
            )
        effective_timeout = min(effective_timeout, remaining)
    outcome, value = bounded_regex_search(
        pattern, output, flags, timeout_sec=effective_timeout,
    )
    if outcome == "timeout":
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="regex_timeout",
            details={"timeout_sec": context.limits.regex_timeout_sec},
        )
    if outcome == "error":
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="regex_execution_error", details={"error": str(value)},
        )
    ok = bool(value)
    return _base_metric(
        observation, metric, MetricStatus.scored, passed=ok,
        reason=None if ok else "pattern_not_matched",
    )


def bounded_regex_search(
    pattern: str, text: str, flags: int, *, timeout_sec: float,
) -> tuple[str, Any]:
    """Terminable regex match: a spawned child is killed when the deadline hits."""
    import multiprocessing as mp

    from . import _regex_worker

    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_regex_worker.worker_main, args=(child_conn, pattern, flags, text), daemon=True,
    )
    process.start()
    child_conn.close()
    try:
        if parent_conn.poll(timeout_sec):
            kind, value = parent_conn.recv()
        else:
            kind, value = "timeout", None
    except (EOFError, OSError):
        kind, value = "error", "matcher process died"
    finally:
        parent_conn.close()
    if kind == "timeout":
        process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join(1.0)
    else:
        process.join(1.0)
        if process.is_alive():
            process.terminate()
            process.join(1.0)
    return kind, value


def bounded_schema_validation(
    schema: dict[str, Any], instance: Any, *, timeout_sec: float,
) -> tuple[str, Any]:
    """Terminable JSON Schema validation (R3 #5).

    schema 内的 ``pattern`` / ``patternProperties`` 由 CPython ``re`` 同步执行，
    病态模式会卡死进程内校验；与 regex 相同地放进可终止的子进程执行。返回
    ("ok", [error.message, ...]) / ("timeout", None) / ("error", str)。
    """
    import multiprocessing as mp

    from . import _schema_worker

    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_schema_worker.worker_main, args=(child_conn, schema, instance), daemon=True,
    )
    process.start()
    child_conn.close()
    try:
        if parent_conn.poll(timeout_sec):
            kind, value = parent_conn.recv()
        else:
            kind, value = "timeout", None
    except (EOFError, OSError):
        kind, value = "error", "validator process died"
    finally:
        parent_conn.close()
    if kind == "timeout":
        process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join(1.0)
    else:
        process.join(1.0)
        if process.is_alive():
            process.terminate()
            process.join(1.0)
    return kind, value


# 封闭注册表：kind -> 评分函数。测试通过替换表内函数注入故障，不新增 import 入口。
_METRIC_KINDS: dict[str, Callable[[FrozenObservation, MetricRequest, EvaluationContext], Any]] = {}


def _register(kind: str, function):  # noqa: ANN001 - internal registration helper
    _METRIC_KINDS[kind] = function
    return function


_register("exact", evaluate_exact)
_register("contains", evaluate_contains)
_register("regex", evaluate_regex)


def register_metric_kind(kind: str, function) -> None:  # noqa: ANN001
    """Register an additional metric implementation (built-in modules only)."""
    if kind in _METRIC_KINDS:
        raise EvaluatorConfigError(f"metric kind already registered: {kind}")
    _METRIC_KINDS[kind] = function


def known_metric_kinds() -> tuple[str, ...]:
    return tuple(sorted(_METRIC_KINDS))


# ---------------------------------------------------------------- 配置规范化

_ALLOWED_METRIC_KEYS = {
    "metric_id", "kind", "unit",
    # exact / contains
    "expected", "normalization", "case_policy",
    # regex
    "pattern", "flags", "timeout_sec",
    # json-schema
    "schema",
    # file metrics
    "path", "mode", "sha256",
    # exit-code
    "allowed", "label",
    # tool-call
    "tool", "min_calls", "max_calls", "args_schema", "forbidden",
    # no-forbidden-write
    "forbidden", "ignore_preexisting",
}


def _positive_number(value: Any, *, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise EvaluatorConfigError(f"limit must be a positive number: {value!r}")
    if maximum is not None and value > maximum:
        raise EvaluatorConfigError(f"limit exceeds the maximum {maximum}: {value!r}")
    return float(value)


def _validate_metric(metric: Any) -> MetricRequest:
    if not isinstance(metric, dict):
        raise EvaluatorConfigError("metric config must be an object")
    metric_id = metric.get("metric_id")
    if not isinstance(metric_id, str) or not metric_id:
        raise EvaluatorConfigError("metric requires a non-empty metric_id")
    kind = metric.get("kind")
    if not isinstance(kind, str) or kind not in _METRIC_KINDS:
        raise EvaluatorConfigError(
            f"unsupported metric kind: {kind!r} (known: {', '.join(known_metric_kinds())})"
        )
    unknown = set(metric) - _ALLOWED_METRIC_KEYS
    if unknown:
        raise EvaluatorConfigError(f"unknown metric fields: {', '.join(sorted(unknown))}")
    from motte_contracts.evaluation import validate_safe_relative_path

    path = metric.get("path")
    if path is not None:
        validate_safe_relative_path(path)
    if kind == "contains" and metric.get("case_policy", "sensitive") not in (
        "sensitive", "insensitive"
    ):
        raise EvaluatorConfigError("case_policy must be sensitive or insensitive")
    if kind == "regex":
        metric_timeout = metric.get("timeout_sec")
        if metric_timeout is not None:
            if isinstance(metric_timeout, bool) or not isinstance(metric_timeout, (int, float)) \
                    or metric_timeout <= 0 or metric_timeout > MAX_REGEX_TIMEOUT_SEC:
                raise EvaluatorConfigError(
                    f"regex timeout_sec must be positive and <= {MAX_REGEX_TIMEOUT_SEC}"
                )
    if kind == "file-content" and metric.get("mode") not in (
        "exact", "contains", "hash", "schema"
    ):
        raise EvaluatorConfigError("file-content mode must be exact/contains/hash/schema")
    if kind == "file-content" and metric.get("mode") == "hash":
        digest = metric.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise EvaluatorConfigError("hash mode requires a 64-char lowercase sha256")
    if kind == "tool-call":
        if not isinstance(metric.get("tool"), str) or not metric.get("tool"):
            raise EvaluatorConfigError("tool-call requires a tool name")
        for key in ("min_calls", "max_calls"):
            value = metric.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or value < 0):
                raise EvaluatorConfigError(f"{key} must be a nonnegative integer")
    if kind == "no-forbidden-write":
        patterns = metric.get("forbidden")
        if not isinstance(patterns, list) or not patterns or any(
            not isinstance(item, str) or not item for item in patterns
        ):
            raise EvaluatorConfigError("no-forbidden-write requires nonempty forbidden patterns")
        for pattern in patterns:
            validate_safe_relative_path(pattern)
    if kind == "exit-code":
        allowed = metric.get("allowed")
        if not isinstance(allowed, list) or not allowed or any(
            isinstance(item, bool) or not isinstance(item, int) for item in allowed
        ):
            raise EvaluatorConfigError("exit-code allowed must be a nonempty list of integers")
    return dict(metric)


def normalize_evaluator_config(config: Any) -> dict[str, Any]:
    """Validate and freeze the evaluator config; unknown fields and kinds rejected."""
    if not isinstance(config, dict):
        raise EvaluatorConfigError("evaluator config must be an object")
    unknown = set(config) - {
        "metrics", "max_input_bytes", "max_artifact_bytes",
        "regex_timeout_sec", "eval_deadline_sec",
    }
    if unknown:
        raise EvaluatorConfigError(f"unknown evaluator config fields: {', '.join(sorted(unknown))}")
    metrics = config.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise EvaluatorConfigError("evaluator config requires a nonempty metrics list")
    normalized_metrics = [_validate_metric(metric) for metric in metrics]
    ids = [metric["metric_id"] for metric in normalized_metrics]
    if len(ids) != len(set(ids)):
        raise EvaluatorConfigError("metric_id values must be unique within one evaluator config")
    return {
        "metrics": normalized_metrics,
        "max_input_bytes": int(_positive_number(
            config.get("max_input_bytes", DEFAULT_MAX_INPUT_BYTES), maximum=MAX_INPUT_BYTES_LIMIT
        )),
        "max_artifact_bytes": int(_positive_number(
            config.get("max_artifact_bytes", DEFAULT_MAX_ARTIFACT_BYTES),
            maximum=MAX_ARTIFACT_BYTES_LIMIT,
        )),
        "regex_timeout_sec": _positive_number(
            config.get("regex_timeout_sec", DEFAULT_REGEX_TIMEOUT_SEC),
            maximum=MAX_REGEX_TIMEOUT_SEC,
        ),
        "eval_deadline_sec": _positive_number(
            config.get("eval_deadline_sec", DEFAULT_EVAL_DEADLINE_SEC),
            maximum=MAX_EVAL_DEADLINE_SEC,
        ),
    }


def evaluate_observation(
    observation: FrozenObservation,
    config: Mapping[str, Any],
    *,
    artifact_reader: Callable[[str], bytes | None] | None = None,
    limits: EvaluationLimits | None = None,
) -> list[MetricResult]:
    """Evaluate every metric in the frozen config against one frozen observation."""
    normalized = normalize_evaluator_config(config)
    evaluation_limits = limits or EvaluationLimits(
        max_input_bytes=normalized["max_input_bytes"],
        max_artifact_bytes=normalized["max_artifact_bytes"],
        regex_timeout_sec=normalized["regex_timeout_sec"],
        eval_deadline_sec=normalized["eval_deadline_sec"],
    )
    context = EvaluationContext(
        observation=observation,
        limits=evaluation_limits,
        artifact_reader=artifact_reader,
        deadline=monotonic() + evaluation_limits.eval_deadline_sec,
    )
    results: list[MetricResult] = []
    for metric in normalized["metrics"]:
        try:
            result = _METRIC_KINDS[metric["kind"]](observation, metric, context)
        except Exception as error:  # noqa: BLE001 - 逐指标隔离，绝不整批失败
            result = _base_metric(
                observation, metric, MetricStatus.evaluator_error,
                reason="evaluator_crash",
                details={"error": f"{type(error).__name__}: {error}"},
            )
        results.append(result)
        if context.expired():
            # Deadline hit: remaining metrics are marked, not silently skipped.
            for remaining in normalized["metrics"][len(results):]:
                results.append(_base_metric(
                    observation, remaining, MetricStatus.evaluator_error,
                    reason="eval_deadline_exceeded",
                ))
            break
    return results


# ---------------------------------------------------------------- 投影与聚合

def metric_result_to_score(metric: MetricResult, case_id: str) -> dict[str, Any]:
    """Project a MetricResult into the persistent multi-metric Score row."""
    return {
        "case_id": case_id,
        "metric_id": metric.metric_id,
        "evaluator_id": metric.evaluator_id,
        "evaluator_version": metric.evaluator_version,
        "metric_status": metric.status.value,
        "value": metric.value,
        "passed": metric.passed,
        "unit": metric.unit,
        "reason": metric.reason,
        "denominator": metric.denominator,
        "details": dict(metric.details),
    }


_STATUS_KEYS = {
    MetricStatus.scored: "scored",
    MetricStatus.insufficient_evidence: "insufficient_evidence",
    MetricStatus.evaluator_error: "evaluator_error",
    MetricStatus.not_applicable: "not_applicable",
}


def aggregate_metric_results(results: list[MetricResult]) -> dict[str, Any]:
    """Aggregate by metric id: per-metric pass counts over the scored denominator."""
    metrics: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = metrics.setdefault(result.metric_id, {
            "scored": 0, "passed": 0, "failed": 0,
            "insufficient_evidence": 0, "evaluator_error": 0, "not_applicable": 0,
        })
        bucket[_STATUS_KEYS[result.status]] += 1
        if result.status is MetricStatus.scored:
            if result.passed is True:
                bucket["passed"] += 1
            elif result.passed is False:
                bucket["failed"] += 1
    totals = {
        "scored": 0, "insufficient_evidence": 0, "evaluator_error": 0, "not_applicable": 0,
    }
    for bucket in metrics.values():
        for key in totals:
            totals[key] += bucket.get(key, 0)
    return {
        "evaluator_id": EVALUATOR_ID,
        "evaluator_version": EVALUATOR_VERSION,
        "metrics": metrics,
        "totals": totals,
    }


# Built-in metric kinds beyond the text scorers live in sibling modules and are
# registered once at import time (closed registry, no dynamic import surface).
from . import files as _files  # noqa: E402
from . import tools as _tools  # noqa: E402

register_metric_kind("json-schema", _tools.evaluate_json_schema)
register_metric_kind("file-exists", _files.evaluate_file_exists)
register_metric_kind("file-content", _files.evaluate_file_content)
register_metric_kind("exit-code", _tools.evaluate_exit_code)
register_metric_kind("tool-call", _tools.evaluate_tool_call)
register_metric_kind("no-forbidden-write", _tools.evaluate_no_forbidden_write)
