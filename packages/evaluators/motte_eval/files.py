"""File-related metric evaluators over a frozen observation.

All reads go through the frozen artifact view: the evaluator never touches the
live workspace, and artifacts whose bytes no longer match their frozen hash are
insufficient evidence rather than silently-scored content.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from motte_contracts.evaluation import (
    EvidenceRef,
    FrozenObservation,
    MetricResult,
    MetricStatus,
)

from .observation import MetricRequest, _base_metric, _insufficient


def _find_artifact(observation: FrozenObservation, path: str) -> Any:
    return next(
        (entry for entry in observation.artifact_refs if entry.path == path), None
    )


def _ref(observation: FrozenObservation, entry: Any) -> list[EvidenceRef]:
    return [EvidenceRef(kind="artifact", run_id=observation.run_id, locator=entry.artifact_id)]


def evaluate_file_exists(observation: FrozenObservation, metric: MetricRequest, context: Any):
    path = metric["path"]
    entry = _find_artifact(observation, path)
    if entry is None:
        if not observation.coverage.complete:
            return _insufficient(
                observation, metric, "capture_incomplete",
                refs=_ref(observation, entry) if entry else [],
            )
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="file_absent",
        )
    if not entry.available:
        return _insufficient(observation, metric, "artifact_unavailable", refs=_ref(observation, entry))
    # 不只信冻结清单：实际读一次字节（#11：Artifact 丢失时降为证据不足）
    if context.read_artifact(entry.artifact_id) is None:
        return _insufficient(observation, metric, "artifact_unavailable", refs=_ref(observation, entry))
    return _base_metric(observation, metric, MetricStatus.scored, passed=True)


def _load_artifact_bytes(
    observation: FrozenObservation, metric: MetricRequest, context: Any, entry: Any,
):
    if entry.redacted:
        return _insufficient(observation, metric, 'artifact_redacted', refs=_ref(observation, entry))
    data = context.read_artifact(entry.artifact_id)
    if data is None:
        return _insufficient(observation, metric, "artifact_unavailable", refs=_ref(observation, entry))
    if entry.sha256 is not None and hashlib.sha256(data).hexdigest() != entry.sha256:
        return _insufficient(observation, metric, "artifact_hash_mismatch", refs=_ref(observation, entry))
    if entry.truncated:
        return _insufficient(observation, metric, "artifact_truncated", refs=_ref(observation, entry))
    if len(data) > context.limits.max_artifact_bytes:
        return _insufficient(
            observation, metric, "artifact_too_large",
            details={"size_bytes": len(data),
                     "max_artifact_bytes": context.limits.max_artifact_bytes},
            refs=_ref(observation, entry),
        )
    return data


def evaluate_file_content(observation: FrozenObservation, metric: MetricRequest, context: Any):
    path = metric["path"]
    entry = _find_artifact(observation, path)
    if entry is None:
        return _insufficient(observation, metric, "artifact_not_captured")
    if not entry.available:
        return _insufficient(observation, metric, "artifact_unavailable", refs=_ref(observation, entry))
    loaded = _load_artifact_bytes(observation, metric, context, entry)
    if isinstance(loaded, MetricResult):
        return loaded
    data: bytes = loaded
    mode = metric["mode"]
    refs = _ref(observation, entry)

    if mode == "hash":
        expected = metric["sha256"]
        ok = hashlib.sha256(data).hexdigest() == expected
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=ok,
            reason=None if ok else "hash_mismatch", refs=refs,
        )
    if mode == "exact":
        expected = metric.get("expected")
        if expected is None:
            return _base_metric(
                observation, metric, MetricStatus.not_applicable,
                reason="no_expectation", denominator=False, refs=refs,
            )
        ok = data.decode("utf-8", errors="replace") == expected
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=ok,
            reason=None if ok else "content_mismatch", refs=refs,
        )
    if mode == "contains":
        expected = metric.get("expected")
        if expected is None:
            return _base_metric(
                observation, metric, MetricStatus.not_applicable,
                reason="no_expectation", denominator=False, refs=refs,
            )
        text = data.decode("utf-8", errors="replace")
        needle = expected if metric.get("case_policy", "sensitive") == "sensitive" else expected.lower()
        haystack = text if metric.get("case_policy", "sensitive") == "sensitive" else text.lower()
        ok = needle in haystack
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=ok,
            reason=None if ok else "content_missing", refs=refs,
        )
    # mode == "schema"
    schema = metric.get("schema")
    if not isinstance(schema, dict):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="config_error", details={"error": "schema mode requires a schema object"},
            refs=refs,
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="invalid_utf8", details={"error": str(error)}, refs=refs,
        )
    try:
        payload = json.loads(text)
    except ValueError as error:
        # Invalid JSON from the subject is a subject failure, not an evaluator fault.
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="invalid_json", details={"error": str(error)}, refs=refs,
        )
    from .observation import bounded_schema_validation, schema_timeout

    kind, value = bounded_schema_validation(
        schema, payload, timeout_sec=schema_timeout(context),
    )
    if kind == "timeout":
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason="schema_timeout", details={"timeout_sec": schema_timeout(context)},
            refs=refs,
        )
    if kind in ("config_error", "unresolvable", "error"):
        return _base_metric(
            observation, metric, MetricStatus.evaluator_error,
            reason={"config_error": "config_error",
                    "unresolvable": "schema_unresolvable"}.get(kind, "schema_execution_error"),
            details={"error": str(value)}, refs=refs,
        )
    if value:
        return _base_metric(
            observation, metric, MetricStatus.scored, passed=False,
            reason="schema_violation",
            details={"violations": list(value[:5])},
            refs=refs,
        )
    return _base_metric(observation, metric, MetricStatus.scored, passed=True, refs=refs)
