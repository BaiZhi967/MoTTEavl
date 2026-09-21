"""M5-T04: process, business-state and side-effect assertions over frozen evidence.

New deterministic metric kinds, registered into the existing closed
``motte_eval.observation`` registry and projected through the existing
``MetricResult`` -> ``Score`` path (no private ScoreSet is invented):

* ``state-equals``    - a declared checkpoint field equals an expectation;
* ``state-delta``     - numeric delta / unchanged / changed between two checkpoints;
* ``no-side-effect``  - no forbidden side effect inside a DECLARED monitored scope;
* ``goal-achieved``   - conjunction of final-state AND process requirements;
* ``response-policy`` - deterministic ordered policy over an action log, the tool
  trajectory, or the final output.

Evidence discipline:

* A checkpoint reference is Artifact + content hash + schema (with its own
  digest) + owner + step. References resolve ONLY through the observation that
  declares them; cross-Case / cross-Run ownership, corrupted hashes, schema
  errors and missing artifacts are refused. Scoring never reads a live mutable
  database and never calls the subject model or its tools.
* A negative conclusion (a pass) requires a declared and complete monitoring
  scope. A complete tool inventory does not prove there were no hidden
  shell/MCP side effects, so unknown stays ``insufficient_evidence`` - never 0,
  never pass.
* checker cannot run / times out / schema error -> ``evaluator_error``; business
  requirement not met -> ``scored`` with ``passed=False``; nothing to check ->
  ``not_applicable``. The three never convert into each other, and a goal
  conjunction never lets a correct final state mask a process failure (M5-A02).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from motte_contracts.evaluation import (
    EvidenceRef,
    FrozenObservation,
    MetricResult,
    MetricStatus,
    WorkflowActionLog,
    WorkflowEvidenceRef,
    workflow_schema_digest,
)

from .observation import (
    EvaluationContext,
    EvaluatorConfigError,
    MetricRequest,
    _ALLOWED_METRIC_KEYS,
    _FLAG_VALUES,
    _METRIC_KINDS,
    _REGEX_FLAG_NAMES,
    _base_metric,
    _insufficient,
    _validate_metric,
    bounded_regex_search,
    bounded_schema_validation,
    register_metric_kind,
    schema_timeout,
)

WORKFLOW_METRIC_KINDS: tuple[str, ...] = (
    "state-equals",
    "state-delta",
    "no-side-effect",
    "goal-achieved",
    "response-policy",
)

# The kind registry is closed (only built-in modules register code) but its
# allowed-field whitelist has no per-kind registration hook. This module only
# ADDS its own field names and re-checks them per kind in every metric function
# below, so no other metric kind silently gains these fields.
WORKFLOW_METRIC_KEYS = frozenset({
    "checkpoint", "before", "after", "delta", "unchanged", "changed", "tolerance",
    "source", "log", "logs", "require", "forbid", "ordered", "scope", "components",
})
_ALLOWED_METRIC_KEYS.update(WORKFLOW_METRIC_KEYS)

_STATE_EQUALS_KEYS = frozenset({
    "metric_id", "kind", "unit", "checkpoint", "path", "expected",
    "normalization", "tolerance",
})
_STATE_DELTA_KEYS = frozenset({
    "metric_id", "kind", "unit", "before", "after", "path",
    "delta", "unchanged", "changed", "tolerance",
})
_RESPONSE_POLICY_KEYS = frozenset({
    "metric_id", "kind", "unit", "source", "log", "require", "forbid",
    "ordered", "case_policy", "timeout_sec", "flags",
})
_NO_SIDE_EFFECT_KEYS = frozenset({
    "metric_id", "kind", "unit", "scope", "log", "logs", "forbid",
})
_GOAL_KEYS = frozenset({"metric_id", "kind", "unit", "components"})
_MATCHER_KEYS = frozenset({
    "action", "tool", "target", "status", "text", "pattern",
    "min_count", "max_count", "case_policy",
})

_NORMALIZATIONS = ("strip", "lowercase", "trim_whitespace")
_POLICY_SOURCES = ("action_log", "tool_calls", "final_output")
_SIDE_EFFECT_SCOPES = ("tool_calls", "actions", "filesystem")
_GOAL_ROLES = ("final_state", "process", "side_effect")
_CONTROLLED_TOOLS = frozenset({"read_file", "list_files", "write_file"})
_ORDER_SCALE = 1_000_000


class WorkflowEvidenceRefused(Exception):
    """Frozen workflow evidence cannot be used: refuse, never guess or repair."""

    def __init__(
        self,
        reason: str,
        status: MetricStatus = MetricStatus.insufficient_evidence,
        detail: str | None = None,
        ref: WorkflowEvidenceRef | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.detail = detail
        self.ref = ref


@dataclass(frozen=True)
class ResolvedWorkflowEvidence:
    """A declared, owner-verified, hash-verified and schema-valid checkpoint."""

    ref: WorkflowEvidenceRef
    payload: Any
    raw: bytes
    evidence_ref: EvidenceRef


_EVIDENCE_CACHE_ATTR = "_workflow_evidence_cache"


def read_workflow_evidence(
    observation: FrozenObservation,
    evidence_id: str,
    context: EvaluationContext,
    *,
    kind: str,
) -> ResolvedWorkflowEvidence:
    """Resolve a declared reference once per scoring call (frozen memo only).

    The frozen artifact bytes and their schema validation are immutable inside
    one ``evaluate_observation`` call, so several metrics referencing the same
    checkpoint must not repeat identical deterministic work (including the
    bounded schema subprocess). Nothing live is memoized: the cache lives in the
    per-call context and only stores already owner/hash/schema verified evidence.
    """
    cache = getattr(context, _EVIDENCE_CACHE_ATTR, None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            setattr(context, _EVIDENCE_CACHE_ATTR, cache)
        except AttributeError:  # pragma: no cover - defensive: context without __dict__
            pass
    key = (observation.observation_id, evidence_id, kind)
    resolved = cache.get(key)
    if resolved is None:
        resolved = _resolve_workflow_evidence(observation, evidence_id, context, kind=kind)
        cache[key] = resolved
    return resolved


def _resolve_workflow_evidence(
    observation: FrozenObservation,
    evidence_id: str,
    context: EvaluationContext,
    *,
    kind: str,
) -> ResolvedWorkflowEvidence:
    """Resolve one declared workflow evidence reference for THIS observation.

    Every step is a refusal condition, never a repair: undeclared or ambiguous
    ids, a foreign owner, a kind mismatch, a missing/redacted/truncated artifact,
    corrupted bytes, an invalid payload or a schema error all raise
    ``WorkflowEvidenceRefused``. The returned payload is frozen artifact bytes
    only - no live fixture database is ever consulted.
    """
    matches = [ref for ref in observation.workflow_evidence if ref.evidence_id == evidence_id]
    if not matches:
        raise WorkflowEvidenceRefused("evidence_not_declared")
    if len(matches) > 1:
        raise WorkflowEvidenceRefused(
            "evidence_ambiguous", MetricStatus.evaluator_error, ref=matches[0],
        )
    ref = matches[0]
    if ref.evidence_kind != kind:
        raise WorkflowEvidenceRefused(
            "evidence_kind_mismatch",
            MetricStatus.evaluator_error,
            detail=f"declared {ref.evidence_kind}, required {kind}",
            ref=ref,
        )
    owner = ref.owner
    foreign = owner.run_id != observation.run_id or owner.case_id != observation.case_id
    if not foreign and owner.attempt_id is not None and observation.attempt_id is not None:
        foreign = owner.attempt_id != observation.attempt_id
    if foreign:
        raise WorkflowEvidenceRefused(
            "foreign_evidence_refused",
            detail=("evidence owner "
                    f"{owner.run_id}/{owner.case_id}/{owner.attempt_id} does not match "
                    f"{observation.run_id}/{observation.case_id}/{observation.attempt_id}"),
            ref=ref,
        )
    entry = next(
        (item for item in observation.artifact_refs if item.artifact_id == ref.artifact_id),
        None,
    )
    if entry is None:
        raise WorkflowEvidenceRefused("artifact_not_captured", ref=ref)
    if not entry.available:
        raise WorkflowEvidenceRefused("artifact_unavailable", ref=ref)
    if entry.redacted:
        raise WorkflowEvidenceRefused("artifact_redacted", ref=ref)
    if entry.truncated:
        raise WorkflowEvidenceRefused("artifact_truncated", ref=ref)
    data = context.read_artifact(ref.artifact_id)
    if data is None:
        raise WorkflowEvidenceRefused("artifact_unavailable", ref=ref)
    if len(data) > context.limits.max_artifact_bytes:
        raise WorkflowEvidenceRefused(
            "artifact_too_large",
            detail=f"{len(data)} > {context.limits.max_artifact_bytes}",
            ref=ref,
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != ref.artifact_sha256:
        raise WorkflowEvidenceRefused(
            "evidence_hash_mismatch",
            detail=f"frozen {ref.artifact_sha256} != read {digest}",
            ref=ref,
        )
    if entry.sha256 is not None and entry.sha256 != digest:
        raise WorkflowEvidenceRefused("artifact_hash_mismatch", ref=ref)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise WorkflowEvidenceRefused(
            "evidence_invalid_json",
            MetricStatus.evaluator_error,
            detail=f"{type(error).__name__}: {error}",
            ref=ref,
        ) from error
    if workflow_schema_digest(ref.payload_schema) != ref.schema_sha256:
        # 契约层已绑定；读取端再做一次纵深校验，绝不按漂移的 schema 评分。
        raise WorkflowEvidenceRefused(
            "evidence_schema_digest_mismatch", MetricStatus.evaluator_error, ref=ref,
        )
    kind_outcome, value = bounded_schema_validation(
        ref.payload_schema, payload, timeout_sec=schema_timeout(context),
    )
    if kind_outcome == "timeout":
        raise WorkflowEvidenceRefused(
            "evidence_schema_timeout", MetricStatus.evaluator_error, ref=ref,
        )
    if kind_outcome in ("config_error", "unresolvable", "error"):
        raise WorkflowEvidenceRefused(
            "evidence_schema_error",
            MetricStatus.evaluator_error,
            detail=str(value),
            ref=ref,
        )
    if value:
        raise WorkflowEvidenceRefused(
            "evidence_schema_violation",
            MetricStatus.evaluator_error,
            detail="; ".join(str(message) for message in list(value)[:5]),
            ref=ref,
        )
    return ResolvedWorkflowEvidence(
        ref=ref,
        payload=payload,
        raw=data,
        evidence_ref=EvidenceRef(kind="artifact", run_id=observation.run_id,
                                 locator=ref.artifact_id),
    )


@dataclass(frozen=True)
class CheckerOutcome:
    """Result of one deterministic declarative checker over frozen payloads."""

    status: MetricStatus
    passed: bool | None = None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class CheckerTimeout(RuntimeError):
    """The declarative checker exceeded its bounded deadline (evaluator_error)."""


class CheckerFailure(RuntimeError):
    """The declarative checker could not run (evaluator_error, never a subject fail)."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _config_error(observation: FrozenObservation, metric: MetricRequest,
                  message: str) -> MetricResult:
    return _base_metric(
        observation, metric, MetricStatus.evaluator_error,
        reason="config_error", details={"error": message},
    )


def _deadline_error(observation: FrozenObservation, metric: MetricRequest) -> MetricResult:
    return _base_metric(
        observation, metric, MetricStatus.evaluator_error,
        reason="eval_deadline_exceeded",
    )


def _shape(observation: FrozenObservation, metric: MetricRequest, *, allowed: frozenset[str],
           required: tuple[str, ...] = ()) -> MetricResult | None:
    unknown = sorted(set(metric) - set(allowed))
    if unknown:
        return _config_error(
            observation, metric,
            f"unknown {metric.get('kind')} fields: {', '.join(unknown)}",
        )
    missing = [name for name in required if metric.get(name) is None]
    if missing:
        return _config_error(
            observation, metric,
            f"{metric.get('kind')} requires: {', '.join(missing)}",
        )
    return None


def _refusal_result(observation: FrozenObservation, metric: MetricRequest,
                    error: WorkflowEvidenceRefused, evidence_id: str) -> MetricResult:
    details: dict[str, Any] = {"evidence_id": evidence_id}
    if error.ref is not None:
        details["evidence_id"] = error.ref.evidence_id
        details["owner"] = error.ref.owner.model_dump(mode="json")
        details["step"] = error.ref.step
    if error.detail:
        details["error"] = error.detail
    return _base_metric(
        observation, metric, error.status, reason=error.reason, details=details,
    )


def _checker_metric(observation: FrozenObservation, metric: MetricRequest,
                    outcome: CheckerOutcome, refs: list[EvidenceRef]) -> MetricResult:
    return _base_metric(
        observation, metric, outcome.status, passed=outcome.passed,
        reason=outcome.reason, details=outcome.details, refs=refs,
    )


def _run_checker(checker, data: Mapping[str, Any], args: Mapping[str, Any]) -> CheckerOutcome:
    """Run a pure declarative checker; a broken checker is an evaluator error."""
    try:
        return checker(data, args)
    except CheckerTimeout as error:
        return CheckerOutcome(MetricStatus.evaluator_error, reason="checker_timeout",
                              details={"error": str(error)})
    except CheckerFailure as error:
        return CheckerOutcome(MetricStatus.evaluator_error, reason=error.reason,
                              details={"error": error.detail or str(error)})
    except Exception as error:  # noqa: BLE001 - 检查器故障不伪装成被测失败
        return CheckerOutcome(
            MetricStatus.evaluator_error, reason="checker_execution_error",
            details={"error": f"{type(error).__name__}: {error}"},
        )


def _resolve_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Dotted JSON path; missing segments/indices report "not found", never a guess."""
    current = payload
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return False, None
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            if not segment.isdigit() or int(segment) >= len(current):
                return False, None
            current = current[int(segment)]
        else:
            return False, None
    return True, current


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _values_equal(left: Any, right: Any, *, normalization: Sequence[str],
                  tolerance: float | None) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        for name in normalization:
            if name in ("strip", "trim_whitespace"):
                left, right = left.strip(), right.strip()
            elif name == "lowercase":
                left, right = left.lower(), right.lower()
        return left == right
    if _is_number(left) and _is_number(right) and tolerance is not None:
        return abs(float(left) - float(right)) <= tolerance
    return left == right


def _normalization_problem(metric: MetricRequest) -> str | None:
    normalization = metric.get("normalization", [])
    if not isinstance(normalization, list) or any(
        not isinstance(item, str) for item in normalization
    ):
        return "normalization must be a list of strings"
    unknown = sorted(set(normalization) - set(_NORMALIZATIONS))
    if unknown:
        return f"unknown normalization: {', '.join(unknown)}"
    return None


def _tolerance_problem(metric: MetricRequest) -> str | None:
    tolerance = metric.get("tolerance")
    if tolerance is None:
        return None
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
        return "tolerance must be a nonnegative number"
    return None


def _checker_state_equals(data: Mapping[str, Any], args: Mapping[str, Any]) -> CheckerOutcome:
    found, actual = _resolve_path(data.get("state"), args["path"])
    if not found:
        # 不完整快照缺字段是证据缺口，不是业务失败；完整快照缺字段才是业务失败。
        if not data.get("complete", True):
            return CheckerOutcome(
                MetricStatus.insufficient_evidence, reason="snapshot_incomplete",
                details={"path": args["path"]},
            )
        return CheckerOutcome(
            MetricStatus.scored, passed=False, reason="state_path_missing",
            details={"path": args["path"]},
        )
    ok = _values_equal(actual, args["expected"], normalization=args["normalization"],
                       tolerance=args["tolerance"])
    return CheckerOutcome(
        MetricStatus.scored, passed=ok,
        reason=None if ok else "state_mismatch",
        details={"path": args["path"], "actual": actual, "expected": args["expected"]},
    )


def _checker_state_delta(data: Mapping[str, Any], args: Mapping[str, Any]) -> CheckerOutcome:
    before_found, before = _resolve_path(data.get("before"), args["path"])
    after_found, after = _resolve_path(data.get("after"), args["path"])
    if not before_found or not after_found:
        if not data.get("complete", True):
            return CheckerOutcome(
                MetricStatus.insufficient_evidence, reason="snapshot_incomplete",
                details={"path": args["path"], "before_present": before_found,
                         "after_present": after_found},
            )
        return CheckerOutcome(
            MetricStatus.scored, passed=False, reason="state_path_missing",
            details={"path": args["path"], "before_present": before_found,
                     "after_present": after_found},
        )
    mode = args["mode"]
    tolerance = args["tolerance"]
    if mode == "delta":
        if not _is_number(before) or not _is_number(after):
            return CheckerOutcome(
                MetricStatus.scored, passed=False, reason="state_not_numeric",
                details={"path": args["path"], "before": before, "after": after},
            )
        effective = 0.0 if tolerance is None else float(tolerance)
        difference = float(after) - float(before)
        ok = abs(difference - float(args["delta"])) <= effective
        return CheckerOutcome(
            MetricStatus.scored, passed=ok,
            reason=None if ok else "state_delta_mismatch",
            details={"path": args["path"], "before": before, "after": after,
                     "observed_delta": difference, "expected_delta": args["delta"],
                     "tolerance": effective},
        )
    unchanged = _values_equal(before, after, normalization=(), tolerance=tolerance)
    ok = unchanged if mode == "unchanged" else not unchanged
    return CheckerOutcome(
        MetricStatus.scored, passed=ok,
        reason=None if ok else ("state_changed" if mode == "unchanged" else "state_unchanged"),
        details={"path": args["path"], "before": before, "after": after, "mode": mode},
    )


def _search_pattern(pattern: str, text: str, flags: int, timeout_sec: float) -> bool:
    """Single bounded entry point for pattern matching (fault-injectable)."""
    outcome, value = bounded_regex_search(pattern, text, flags, timeout_sec=timeout_sec)
    if outcome == "timeout":
        raise CheckerTimeout(f"pattern exceeded {timeout_sec}s")
    if outcome == "error":
        raise CheckerFailure("checker_execution_error", detail=str(value))
    return bool(value)


def _effective_timeout(context: EvaluationContext, metric: MetricRequest) -> float:
    requested = metric.get("timeout_sec")
    effective = float(context.limits.regex_timeout_sec)
    if isinstance(requested, (int, float)) and not isinstance(requested, bool):
        effective = min(float(requested), effective)
    if context.deadline is not None:
        effective = min(effective, max(context.deadline - monotonic(), 0.0))
    return effective


def evaluate_state_equals(observation: FrozenObservation, metric: MetricRequest,
                           context: EvaluationContext) -> MetricResult:
    shape = _shape(observation, metric, allowed=_STATE_EQUALS_KEYS,
                   required=("checkpoint", "path"))
    if shape is not None:
        return shape
    if "expected" not in metric:
        return _base_metric(
            observation, metric, MetricStatus.not_applicable,
            reason="no_expectation", denominator=False,
        )
    problem = _normalization_problem(metric) or _tolerance_problem(metric)
    if problem:
        return _config_error(observation, metric, problem)
    if context.expired():
        return _deadline_error(observation, metric)
    try:
        resolved = read_workflow_evidence(observation, metric["checkpoint"], context,
                                          kind="state")
    except WorkflowEvidenceRefused as error:
        return _refusal_result(observation, metric, error, metric["checkpoint"])
    outcome = _run_checker(_checker_state_equals, {
        "state": resolved.payload, "complete": resolved.ref.complete,
    }, {
        "path": metric["path"],
        "expected": metric["expected"],
        "normalization": list(metric.get("normalization", [])),
        "tolerance": metric.get("tolerance"),
    })
    return _checker_metric(observation, metric, outcome, [resolved.evidence_ref])


def evaluate_state_delta(observation: FrozenObservation, metric: MetricRequest,
                          context: EvaluationContext) -> MetricResult:
    shape = _shape(observation, metric, allowed=_STATE_DELTA_KEYS,
                   required=("before", "after", "path"))
    if shape is not None:
        return shape
    modes = [name for name in ("delta", "unchanged", "changed") if name in metric]
    if len(modes) != 1:
        return _config_error(
            observation, metric,
            "state-delta requires exactly one of delta / unchanged / changed",
        )
    mode = modes[0]
    if mode == "delta" and not _is_number(metric.get("delta")):
        return _config_error(observation, metric, "delta must be a number")
    if mode in ("unchanged", "changed") and metric[mode] is not True:
        return _config_error(observation, metric, f"{mode} must be true when present")
    problem = _tolerance_problem(metric)
    if problem:
        return _config_error(observation, metric, problem)
    if context.expired():
        return _deadline_error(observation, metric)
    try:
        before = read_workflow_evidence(observation, metric["before"], context, kind="state")
    except WorkflowEvidenceRefused as error:
        return _refusal_result(observation, metric, error, metric["before"])
    try:
        after = read_workflow_evidence(observation, metric["after"], context, kind="state")
    except WorkflowEvidenceRefused as error:
        return _refusal_result(observation, metric, error, metric["after"])
    outcome = _run_checker(
        _checker_state_delta,
        {"before": before.payload, "after": after.payload,
         "complete": before.ref.complete and after.ref.complete},
        {"path": metric["path"], "mode": mode, "delta": metric.get("delta"),
         "tolerance": metric.get("tolerance")},
    )
    return _checker_metric(
        observation, metric, outcome, [before.evidence_ref, after.evidence_ref],
    )


@dataclass(frozen=True)
class _PolicyStream:
    """Normalized ordered evidence stream for a response policy."""

    source: str
    items: list[dict[str, Any]]
    refs: list[EvidenceRef]
    complete: bool
    scope: tuple[str, ...]
    incomplete_reason: str | None = None


def _tool_call_text(call: Any) -> str:
    return json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)


def _policy_stream(observation: FrozenObservation, metric: MetricRequest,
                   context: EvaluationContext) -> _PolicyStream | MetricResult:
    source = metric.get("source", "action_log")
    if source not in _POLICY_SOURCES:
        return _config_error(observation, metric, f"unsupported source: {source!r}")
    if source == "final_output":
        output = observation.final_output
        if output is None:
            return _insufficient(observation, metric, "final_output_missing")
        text = output if isinstance(output, str) else json.dumps(
            output, ensure_ascii=False, sort_keys=True, default=str,
        )
        return _PolicyStream(
            source=source,
            items=[{"order": 1, "step": 1, "name": "final_output", "target": None,
                    "status": "succeeded", "text": text, "call_id": None}],
            refs=[],
            complete=True,
            scope=("final_output",),
        )
    if source == "tool_calls":
        if not observation.coverage.complete:
            return _insufficient(observation, metric, "tool_trajectory_incomplete")
        return _PolicyStream(
            source=source, items=_tool_call_items(observation), refs=[], complete=True,
            scope=("tool_calls",),
        )
    log_id = metric.get("log")
    if not isinstance(log_id, str) or not log_id:
        return _config_error(observation, metric, "source action_log requires log")
    try:
        resolved = read_workflow_evidence(observation, log_id, context, kind="action_log")
    except WorkflowEvidenceRefused as error:
        return _refusal_result(observation, metric, error, log_id)
    try:
        log = WorkflowActionLog.model_validate(resolved.payload)
    except ValueError as error:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="action_log_invalid", details={"error": str(error)[:300]},
            refs=[resolved.evidence_ref],
        )
    scope = tuple(resolved.ref.monitored_scope)
    if not resolved.ref.complete:
        reason: str | None = "action_log_incomplete"
    elif not scope:
        reason = "monitoring_scope_undeclared"
    elif not observation.coverage.complete:
        reason = "capture_incomplete"
    else:
        reason = None
    return _PolicyStream(
        source=source, items=_action_log_items([log]), refs=[resolved.evidence_ref],
        complete=reason is None, scope=scope, incomplete_reason=reason,
    )


def _validate_matcher(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return "policy matcher must be an object"
    unknown = sorted(set(raw) - _MATCHER_KEYS)
    if unknown:
        return f"unknown matcher fields: {', '.join(unknown)}"
    if not any(raw.get(key) for key in ("action", "tool", "target", "text", "pattern")):
        return "matcher requires one of action/tool/target/text/pattern"
    for key in ("action", "tool", "target", "status", "text", "pattern"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            return f"matcher {key} must be a non-empty string"
    for key in ("min_count", "max_count"):
        value = raw.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or value < 0):
            return f"matcher {key} must be a nonnegative integer"
    minimum, maximum = raw.get("min_count"), raw.get("max_count")
    if minimum is not None and maximum is not None and maximum < minimum:
        return "matcher max_count must be >= min_count"
    if raw.get("case_policy", "sensitive") not in ("sensitive", "insensitive"):
        return "matcher case_policy must be sensitive or insensitive"
    return None


def _matcher_list(metric: MetricRequest, key: str) -> tuple[list[dict[str, Any]], str | None]:
    raw = metric.get(key, [])
    if not isinstance(raw, list):
        return [], f"{key} must be a list of matchers"
    for item in raw:
        problem = _validate_matcher(item)
        if problem:
            return [], f"{key}: {problem}"
    return [dict(item) for item in raw], None


def _field_matches(value: Any, expectation: Any, policy: str) -> bool:
    if expectation is None:
        return True
    if not isinstance(value, str):
        return False
    if policy == "insensitive":
        return value.lower() == expectation.lower()
    return value == expectation


def _text_offsets(text: str, needle: str, policy: str) -> list[int]:
    haystack = text if policy == "sensitive" else text.lower()
    target = needle if policy == "sensitive" else needle.lower()
    offsets: list[int] = []
    start = haystack.find(target)
    while start >= 0 and len(offsets) < 64:
        offsets.append(start)
        start = haystack.find(target, start + max(len(target), 1))
    return offsets


def _matcher_keys(items: Sequence[Mapping[str, Any]], matcher: Mapping[str, Any], *, flags: int,
                  timeout_sec: float) -> list[int]:
    policy = matcher.get("case_policy", "sensitive")
    name = matcher.get("action") or matcher.get("tool")
    keys: list[int] = []
    for item in items:
        order = int(item.get("order") or 0)
        if not _field_matches(item.get("name"), name, policy):
            continue
        if not _field_matches(item.get("target"), matcher.get("target"), policy):
            continue
        status = matcher.get("status")
        if status is not None and item.get("status") != status:
            continue
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        offsets: list[int] = []
        if matcher.get("text") is not None:
            offsets = _text_offsets(text, matcher["text"], policy)
            if not offsets:
                continue
        if matcher.get("pattern") is not None:
            if not _search_pattern(matcher["pattern"], text, flags, timeout_sec):
                continue
        if not offsets:
            offsets = [0]
        keys.extend(order * _ORDER_SCALE + min(offset, _ORDER_SCALE - 1) for offset in offsets)
    return sorted(keys)


def _matcher_summary(matcher: Mapping[str, Any]) -> dict[str, Any]:
    return {key: matcher[key] for key in sorted(matcher)}


def _policy_flags(metric: MetricRequest) -> tuple[int, str | None]:
    names = metric.get("flags", [])
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        return 0, "flags must be a list of flag names"
    unknown = sorted(set(names) - set(_REGEX_FLAG_NAMES))
    if unknown:
        return 0, f"unsupported regex flags: {', '.join(unknown)}"
    flags = 0
    for name in names:
        flags |= _FLAG_VALUES[name]
    return flags, None


def _policy_options(metric: MetricRequest) -> tuple[dict[str, Any], str | None]:
    require, problem = _matcher_list(metric, "require")
    if problem:
        return {}, problem
    forbid, problem = _matcher_list(metric, "forbid")
    if problem:
        return {}, problem
    if not require and not forbid:
        return {}, "response-policy requires a nonempty require or forbid list"
    ordered = metric.get("ordered", True)
    if not isinstance(ordered, bool):
        return {}, "ordered must be a boolean"
    if metric.get("case_policy", "sensitive") not in ("sensitive", "insensitive"):
        return {}, "case_policy must be sensitive or insensitive"
    timeout = metric.get("timeout_sec")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or timeout <= 0):
        return {}, "timeout_sec must be a positive number"
    flags, problem = _policy_flags(metric)
    if problem:
        return {}, problem
    return {"require": require, "forbid": forbid, "ordered": ordered, "flags": flags}, None


def _confirmed_policy_violations(
    require: Sequence[Mapping[str, Any]], require_keys: Sequence[Sequence[int]],
    forbid_keys: Sequence[tuple[Mapping[str, Any], Sequence[int]]],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for matcher, keys in zip(require, require_keys):
        maximum = matcher.get("max_count")
        if maximum is not None and len(keys) > maximum:
            violations.append({
                "kind": "action_count_exceeded", "matcher": _matcher_summary(matcher),
                "matched": len(keys), "max_count": maximum,
            })
    for matcher, keys in forbid_keys:
        if keys:
            violations.append({
                "kind": "forbidden_match", "matcher": _matcher_summary(matcher),
                "matched": len(keys),
            })
    return violations


def _order_satisfied(require: Sequence[Mapping[str, Any]],
                     require_keys: Sequence[Sequence[int]]) -> bool:
    cursor: int | None = None
    for matcher, keys in zip(require, require_keys):
        if int(matcher.get("min_count", 1)) < 1:
            continue
        candidate = next((key for key in keys if cursor is None or key > cursor), None)
        if candidate is None:
            return False
        cursor = candidate
    return True


def _completeness_policy_violations(
    require: Sequence[Mapping[str, Any]], require_keys: Sequence[Sequence[int]], ordered: bool,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for matcher, keys in zip(require, require_keys):
        minimum = int(matcher.get("min_count", 1))
        if len(keys) < minimum:
            violations.append({
                "kind": "required_match_missing", "matcher": _matcher_summary(matcher),
                "matched": len(keys), "min_count": minimum,
            })
    if not violations and ordered and not _order_satisfied(require, require_keys):
        violations.append({
            "kind": "action_order_violation",
            "requirements": [_matcher_summary(matcher) for matcher in require],
        })
    return violations


def evaluate_response_policy(observation: FrozenObservation, metric: MetricRequest,
                             context: EvaluationContext) -> MetricResult:
    shape = _shape(observation, metric, allowed=_RESPONSE_POLICY_KEYS)
    if shape is not None:
        return shape
    options, problem = _policy_options(metric)
    if problem:
        return _config_error(observation, metric, problem)
    if context.expired():
        return _deadline_error(observation, metric)
    stream = _policy_stream(observation, metric, context)
    if isinstance(stream, MetricResult):
        return stream
    timeout = _effective_timeout(context, metric)
    try:
        require_keys = [
            _matcher_keys(stream.items, matcher, flags=options["flags"], timeout_sec=timeout)
            for matcher in options["require"]
        ]
        forbid_keys = [
            (matcher, _matcher_keys(stream.items, matcher, flags=options["flags"],
                                    timeout_sec=timeout))
            for matcher in options["forbid"]
        ]
    except CheckerTimeout as error:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error, reason="checker_timeout",
            details={"timeout_sec": timeout, "error": str(error)}, refs=stream.refs,
        )
    except CheckerFailure as error:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error, reason=error.reason,
            details={"error": error.detail or str(error)}, refs=stream.refs,
        )
    confirmed = _confirmed_policy_violations(options["require"], require_keys, forbid_keys)
    if confirmed:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason=confirmed[0]["kind"], details={"violations": confirmed}, refs=stream.refs,
        )
    if not stream.complete:
        return _insufficient(
            observation, metric, stream.incomplete_reason or "evidence_incomplete",
            details={"source": stream.source, "scope": list(stream.scope)}, refs=stream.refs,
        )
    dependent = _completeness_policy_violations(
        options["require"], require_keys, options["ordered"],
    )
    if dependent:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason=dependent[0]["kind"], details={"violations": dependent}, refs=stream.refs,
        )
    return _base_metric(
        observation, metric, MetricStatus.scored, passed=True,
        details={"source": stream.source, "scope": list(stream.scope),
                 "matched": len(stream.items)},
        refs=stream.refs,
    )


def _log_ids(metric: MetricRequest) -> tuple[list[str], str | None]:
    ids: list[str] = []
    single = metric.get("log")
    if single is not None:
        if not isinstance(single, str) or not single:
            return [], "log must be a non-empty evidence id"
        ids.append(single)
    many = metric.get("logs")
    if many is not None:
        if not isinstance(many, list) or any(not isinstance(item, str) or not item
                                             for item in many):
            return [], "logs must be a list of non-empty evidence ids"
        ids.extend(many)
    deduped: list[str] = []
    for item in ids:
        if item not in deduped:
            deduped.append(item)
    return deduped, None


def _action_log_items(logs: Sequence[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, log in enumerate(logs):
        for record in log.actions:
            items.append({
                "order": index * _ORDER_SCALE + record.seq, "step": record.step,
                "name": record.action, "target": record.target, "status": record.status,
                "text": record.detail, "call_id": record.call_id, "kind": "action",
            })
    return items


def _tool_call_items(observation: FrozenObservation) -> list[dict[str, Any]]:
    return [
        {"order": index + 1, "step": call.step, "name": call.tool_name,
         "target": call.arguments.get("target") or call.arguments.get("path"),
         "status": call.status, "text": _tool_call_text(call), "call_id": call.call_id,
         "kind": "tool_call"}
        for index, call in enumerate(observation.tool_calls)
    ]


def _unmonitored_tool_calls(observation: FrozenObservation,
                            logged_call_ids: set[str]) -> list[str]:
    """工具清单不是副作用证据：受控原语之外或语义不完整的调用无法证明无副作用。"""
    unmonitored: list[str] = []
    for call in observation.tool_calls:
        if call.call_id in logged_call_ids:
            continue
        if call.tool_name not in _CONTROLLED_TOOLS:
            unmonitored.append(call.tool_name)
            continue
        if call.tool_name == "write_file" and (
            call.status != "succeeded" or not isinstance(call.arguments.get("path"), str)
        ):
            unmonitored.append(call.tool_name)
    return sorted(set(unmonitored))


def _forbidden_hits(forbid: Sequence[Mapping[str, Any]], items: Sequence[Mapping[str, Any]],
                    timeout_sec: float) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for matcher in forbid:
        keys = _matcher_keys(items, matcher, flags=0, timeout_sec=timeout_sec)
        if keys:
            hits.append({"matcher": _matcher_summary(matcher), "matched": len(keys)})
    return hits


def evaluate_no_side_effect(observation: FrozenObservation, metric: MetricRequest,
                            context: EvaluationContext) -> MetricResult:
    shape = _shape(observation, metric, allowed=_NO_SIDE_EFFECT_KEYS)
    if shape is not None:
        return shape
    scope = metric.get("scope")
    if scope is None or scope == []:
        return _insufficient(observation, metric, "monitoring_scope_undeclared")
    if not isinstance(scope, list) or any(not isinstance(item, str) for item in scope):
        return _config_error(observation, metric, "scope must be a list of scope tokens")
    unknown = sorted(set(scope) - set(_SIDE_EFFECT_SCOPES))
    if unknown:
        return _config_error(observation, metric, f"unknown scope tokens: {', '.join(unknown)}")
    forbid, problem = _matcher_list(metric, "forbid")
    if problem:
        return _config_error(observation, metric, problem)
    log_ids, problem = _log_ids(metric)
    if problem:
        return _config_error(observation, metric, problem)
    if "actions" in scope and not log_ids:
        return _insufficient(
            observation, metric, "action_log_missing", details={"scope": list(scope)},
        )
    if context.expired():
        return _deadline_error(observation, metric)
    logs = []
    refs: list[EvidenceRef] = []
    logged_call_ids: set[str] = set()
    incomplete_reason: str | None = None
    for log_id in log_ids:
        try:
            resolved = read_workflow_evidence(observation, log_id, context,
                                              kind="action_log")
        except WorkflowEvidenceRefused as error:
            return _refusal_result(observation, metric, error, log_id)
        try:
            log = WorkflowActionLog.model_validate(resolved.payload)
        except ValueError as error:
            return _base_metric(
                observation, metric, MetricStatus.evaluator_error,
                reason="action_log_invalid", details={"error": str(error)[:300]},
                refs=[resolved.evidence_ref],
            )
        logs.append(log)
        refs.append(resolved.evidence_ref)
        for record in log.actions:
            if record.call_id:
                logged_call_ids.add(record.call_id)
        if not resolved.ref.complete:
            incomplete_reason = incomplete_reason or "action_log_incomplete"
        elif not resolved.ref.monitored_scope:
            incomplete_reason = incomplete_reason or "monitoring_scope_undeclared"
    action_items = _action_log_items(logs)
    tool_items = _tool_call_items(observation)
    timeout = _effective_timeout(context, metric)
    try:
        hits = _forbidden_hits(forbid, action_items + tool_items, timeout)
    except CheckerTimeout:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error, reason="checker_timeout",
            details={"timeout_sec": timeout}, refs=refs,
        )
    except CheckerFailure as error:
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error, reason=error.reason,
            details={"error": error.detail or str(error)}, refs=refs,
        )
    if hits:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="side_effect_detected", details={"violations": hits, "scope": list(scope)},
            refs=refs,
        )
    # 阴性结论需要声明范围与完整度；已确认的违规在上面已先行计入。
    if incomplete_reason is not None:
        return _insufficient(observation, metric, incomplete_reason,
                             details={"scope": list(scope)}, refs=refs)
    if "filesystem" in scope:
        workspace = observation.workspace
        if workspace is None or not workspace.complete:
            return _insufficient(observation, metric, "workspace_snapshot_incomplete",
                                 details={"scope": list(scope)}, refs=refs)
    if "tool_calls" in scope and not observation.coverage.complete:
        return _insufficient(observation, metric, "tool_trajectory_incomplete",
                             details={"scope": list(scope)}, refs=refs)
    unmonitored = _unmonitored_tool_calls(observation, logged_call_ids)
    if unmonitored:
        return _insufficient(
            observation, metric, "tool_side_effects_unobserved",
            details={"tools": unmonitored, "scope": list(scope)}, refs=refs,
        )
    return _base_metric(
        observation, metric, MetricStatus.scored, passed=True,
        details={"scope": list(scope), "monitored_actions": len(action_items),
                 "monitored_tool_calls": len(tool_items), "logs": log_ids},
        refs=refs,
    )


def evaluate_goal_achieved(observation: FrozenObservation, metric: MetricRequest,
                           context: EvaluationContext) -> MetricResult:
    shape = _shape(observation, metric, allowed=_GOAL_KEYS, required=("components",))
    if shape is not None:
        return shape
    components = metric["components"]
    if not isinstance(components, list) or not components:
        return _config_error(observation, metric, "components must be a nonempty list")
    evaluated: list[tuple[str, str, MetricResult]] = []
    refs: list[EvidenceRef] = []
    seen_refs: set[tuple[str, str, str]] = set()
    for component in components:
        if not isinstance(component, dict):
            return _config_error(observation, metric, "goal component must be an object")
        unknown = sorted(set(component) - {"role", "metric"})
        if unknown:
            return _config_error(
                observation, metric, f"unknown goal component fields: {', '.join(unknown)}",
            )
        role = component.get("role")
        if role not in _GOAL_ROLES:
            return _config_error(
                observation, metric,
                f"goal role must be one of {', '.join(_GOAL_ROLES)}",
            )
        spec = component.get("metric")
        if not isinstance(spec, dict):
            return _config_error(observation, metric, "goal component requires a metric object")
        if spec.get("kind") == "goal-achieved":
            return _config_error(observation, metric, "goal-achieved cannot nest itself")
        try:
            normalized = _validate_metric(dict(spec))
        except EvaluatorConfigError as error:
            return _config_error(observation, metric, f"goal component invalid: {error}")
        handler = _METRIC_KINDS.get(normalized["kind"])
        if handler is None:
            return _config_error(
                observation, metric,
                f"goal component kind is not registered: {normalized['kind']!r}",
            )
        try:
            result = handler(observation, normalized, context)
        except Exception as error:  # noqa: BLE001 - 组件崩溃不改写为业务失败
            result = _base_metric(
                observation, normalized, MetricStatus.evaluator_error,
                reason="evaluator_crash",
                details={"error": f"{type(error).__name__}: {error}"},
            )
        evaluated.append((str(role), normalized["metric_id"], result))
        for ref in result.evidence_refs:
            key = (ref.kind, ref.run_id, ref.locator)
            if key not in seen_refs:
                seen_refs.add(key)
                refs.append(ref)
    return _combine_goal(observation, metric, evaluated, refs)


def _combine_goal(observation: FrozenObservation, metric: MetricRequest,
                  evaluated: Sequence[tuple[str, str, MetricResult]],
                  refs: list[EvidenceRef]) -> MetricResult:
    """Conjunction where a confirmed process failure outweighs a correct final state.

    Precedence: component evaluator error > confirmed scored failure > insufficient
    or not_applicable evidence > pass. The final-state component can never mask a
    failed process component (M5-A02).
    """
    breakdown = [
        {"role": role, "metric_id": metric_id, "status": result.status.value,
         "passed": result.passed, "reason": result.reason}
        for role, metric_id, result in evaluated
    ]
    if any(result.status is MetricStatus.evaluator_error for _, _, result in evaluated):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="component_evaluator_error", details={"components": breakdown}, refs=refs,
        )
    failed = [{"role": role, "metric_id": metric_id}
              for role, metric_id, result in evaluated
              if result.status is MetricStatus.scored and result.passed is not True]
    if failed:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="goal_not_achieved",
            details={"components": breakdown, "failed": failed}, refs=refs,
        )
    if any(result.status is MetricStatus.insufficient_evidence for _, _, result in evaluated):
        return _base_metric(
            observation, metric, MetricStatus.insufficient_evidence,
            reason="component_insufficient_evidence", details={"components": breakdown},
            refs=refs,
        )
    if any(result.status is MetricStatus.not_applicable for _, _, result in evaluated):
        return _base_metric(
            observation, metric, MetricStatus.insufficient_evidence,
            reason="component_not_applicable", details={"components": breakdown}, refs=refs,
        )
    return _base_metric(
        observation, metric, MetricStatus.scored, passed=True,
        details={"components": breakdown, "roles": [role for role, _, _ in evaluated]},
        refs=refs,
    )


def register_workflow_metric_kinds() -> None:
    """Register the M5-T04 kinds into the closed observation registry (idempotent)."""
    for kind, function in (
        ("state-equals", evaluate_state_equals),
        ("state-delta", evaluate_state_delta),
        ("no-side-effect", evaluate_no_side_effect),
        ("goal-achieved", evaluate_goal_achieved),
        ("response-policy", evaluate_response_policy),
    ):
        if kind not in _METRIC_KINDS:
            register_metric_kind(kind, function)


register_workflow_metric_kinds()
