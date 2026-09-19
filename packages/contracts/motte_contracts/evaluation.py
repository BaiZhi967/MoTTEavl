"""Evaluation-layer contracts: frozen observations, evaluator specs, metric results.

This is the M1 evaluation view. Records here reference persisted frozen evidence
(events, artifacts, invocation logs) by stable ref instead of holding worker- or
framework-private objects. The legacy ``Observation`` (name/value/source) in
``evidence.py`` stays the compatible read model; new records carry an explicit
``schema_version`` and never reuse the legacy ``name`` field for case identity.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from .identity import canonical_json_bytes
from .messages import Contract

__all__ = [
    "AgentResult",
    "ArtifactEntry",
    "EvidenceCoverage",
    "EvidenceRef",
    "EvaluatorSpec",
    "FrozenObservation",
    "InvocationKind",
    "InvocationRecord",
    "MetricResult",
    "MetricStatus",
    "ObservedUsage",
    "TerminationReason",
    "TerminationRecord",
    "observation_evidence_hash",
]


# 审计字段：可以变化但不参与评分输入视图 hash
_AUDIT_FIELDS = frozenset({"evidence_hash", "recorded_at"})


def validate_safe_relative_path(path: str) -> str:
    """受控相对路径：posix、非绝对、无 ..、无反斜杠。"""
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if path.startswith(("/", "\\")) or "\\" in path:
        raise ValueError(f"path must be a relative posix path: {path!r}")
    if PurePosixPath(path).is_absolute():
        raise ValueError(f"path must be relative: {path!r}")
    if any(part == ".." for part in PurePosixPath(path).parts):
        raise ValueError(f"path escapes the workspace: {path!r}")
    return path


class EvidenceRef(Contract):
    """Stable pointer to frozen platform evidence; external URLs are not evidence."""

    kind: Literal["event", "artifact", "invocation"]
    run_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)

    @model_validator(mode="after")
    def locator_shape(self) -> EvidenceRef:
        if self.kind == "event":
            if not self.locator.isdigit() or int(self.locator) < 1:
                raise ValueError("event evidence locator must be a positive seq number")
        elif self.kind == "artifact":
            validate_safe_relative_path(self.locator)
        return self


class ArtifactEntry(Contract):
    """An artifact captured from a case workspace into the frozen view."""

    artifact_id: str  # safe relative path inside the artifact store
    path: str  # logical path inside the case workspace
    media_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0, strict=True)
    sha256: str | None = None  # plain hex digest of captured bytes
    available: bool = True
    truncated: bool = False

    @model_validator(mode="after")
    def safe_paths(self) -> ArtifactEntry:
        validate_safe_relative_path(self.artifact_id)
        validate_safe_relative_path(self.path)
        if self.sha256 is not None and (
            len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("sha256 must be a 64-char lowercase hex digest")
        return self


TerminationReason = Literal[
    "final_answer", "max_steps", "max_tool_calls", "wall_time",
    "token_limit", "cost_limit", "cancelled", "error", "invalid_state",
]


class TerminationRecord(Contract):
    reason: TerminationReason
    detail: str | None = None


class EvidenceCoverage(Contract):
    """What share of the evidence scope was actually captured."""

    complete: bool
    events_captured: int = Field(default=0, ge=0, strict=True)
    artifacts_captured: int = Field(default=0, ge=0, strict=True)
    artifacts_expected: int | None = Field(default=None, ge=0, strict=True)
    missing: list[str] = Field(default_factory=list)


class ObservedUsage(Contract):
    """Observed metering; ``reported=False`` means the provider sent no usage."""

    reported: bool = True
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    cost_total: float | None = Field(default=None, allow_inf_nan=False)


class FrozenObservation(Contract):
    """Frozen per-case scoring input produced after termination.

    Scoring reads only this record (plus artifacts it references); a changed
    workspace after the freeze cannot change evaluation. The ``evidence_hash``
    binds the scoring view: identical hash means identical scoring input.
    """

    schema_version: Literal[1] = 1
    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    attempt_id: str | None = None
    final_output: Any = None
    termination: TerminationRecord
    event_refs: list[EvidenceRef] = Field(default_factory=list)
    artifact_refs: list[ArtifactEntry] = Field(default_factory=list)
    coverage: EvidenceCoverage
    usage: ObservedUsage | None = None
    evidence_hash: str
    recorded_at: datetime | None = None

    @model_validator(mode="after")
    def hash_shape(self) -> FrozenObservation:
        if not self.evidence_hash.startswith("sha256:") or len(self.evidence_hash) != 71:
            raise ValueError("evidence_hash must be a sha256:<64 hex> value")
        return self


def observation_evidence_hash(payload: Any) -> str:
    """Canonical hash over the frozen scoring view (audit fields excluded).

    Rejects NaN/Infinity inputs before hashing so a hash can never alias
    non-finite values. ``recorded_at``/``evidence_hash`` are excluded on
    purpose: they may differ between captures of the same scoring view.
    """
    if isinstance(payload, dict):
        view = {key: value for key, value in payload.items() if key not in _AUDIT_FIELDS}
    else:
        view = payload
    return "sha256:" + hashlib.sha256(canonical_json_bytes(view)).hexdigest()


class EvaluatorSpec(Contract):
    """A frozen evaluator configuration; unknown versions are rejected upstream."""

    evaluator_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)
    required_evidence: list[str] = Field(default_factory=list)
    missing_policy: Literal["insufficient_evidence", "not_applicable", "fail"] = (
        "insufficient_evidence"
    )
    metric_ids: list[str] = Field(default_factory=list)
    config_sha256: str | None = None


class MetricStatus(str, Enum):
    scored = "scored"
    insufficient_evidence = "insufficient_evidence"
    evaluator_error = "evaluator_error"
    not_applicable = "not_applicable"


class MetricResult(Contract):
    """One metric over a frozen observation.

    Non-scored statuses never fabricate a value or a pass; they carry an
    explicit reason and stay out of (or explicitly inside, via ``denominator``)
    the metric's declared denominator.
    """

    metric_id: str = Field(min_length=1)
    status: MetricStatus
    value: float | None = Field(default=None, allow_inf_nan=False)
    passed: bool | None = None
    unit: str | None = None
    evaluator_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    reason: str | None = None
    denominator: bool = True
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def status_invariants(self) -> MetricResult:
        if self.status is MetricStatus.scored:
            if self.value is None and self.passed is None:
                raise ValueError("scored metric needs a value or a passed flag")
        else:
            if self.passed is not None:
                raise ValueError(f"{self.status.value} metric cannot report passed")
            if self.value is not None:
                raise ValueError(f"{self.status.value} metric cannot fabricate a value")
        return self


class AgentResult(Contract):
    """The agent-side outcome handed to the application layer.

    An agent claiming completion is a termination fact, never a scoring pass.
    """

    final_output: Any = None
    termination_reason: TerminationReason
    observed_usage: ObservedUsage | None = None
    artifact_refs: list[ArtifactEntry] = Field(default_factory=list)
    evidence_coverage: EvidenceCoverage


class InvocationKind(str, Enum):
    model = "model"
    tool = "tool"


class InvocationRecord(Contract):
    """Persisted per-call boundary: prepared -> dispatching -> settled.

    This is evidence and recovery bookkeeping under CaseAttempt, not a second
    run state machine. ``settled`` requires an outcome; unsettled records mean
    the side effect may have happened (indeterminate).
    """

    id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    attempt_id: str | None = None
    kind: InvocationKind
    step: int = Field(ge=1, strict=True)
    ordinal: int | None = Field(default=None, ge=1, strict=True)
    status: Literal["prepared", "dispatching", "settled"]
    outcome: Literal["succeeded", "failed", "indeterminate"] | None = None
    tool_name: str | None = None
    model: str | None = None
    request_summary: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] | None = None
    evidence_ref: EvidenceRef | None = None
    prepared_at: datetime | None = None
    dispatched_at: datetime | None = None
    settled_at: datetime | None = None

    @model_validator(mode="after")
    def call_invariants(self) -> InvocationRecord:
        if self.status == "settled":
            if self.outcome is None:
                raise ValueError("settled invocation requires an outcome")
            if self.settled_at is None:
                raise ValueError("settled invocation requires settled_at")
        elif self.outcome is not None:
            raise ValueError("only settled invocations carry an outcome")
        if self.kind is InvocationKind.tool and not self.tool_name:
            raise ValueError("tool invocation requires tool_name")
        if self.kind is InvocationKind.model and self.tool_name is not None:
            raise ValueError("model invocation cannot carry tool_name")
        return self
